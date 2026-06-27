"""
translate_srt.py - JSON-first subtitle pipeline.

Flow:
  WhisperX JSON -> beautified JSON -> glossary -> translation/split/proofread -> ASS

SRT is intentionally not part of the main pipeline anymore. WhisperX JSON is the
single source of truth; word timestamps are used only to project split whole
sentences back onto the timeline.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time

import unicodedata
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Optional

import langcodes
import language_data  # noqa: F401
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_openai import OpenAIEmbeddings
from openai import OpenAI
from tavily import TavilyClient


# --- Data model ---------------------------------------------------------------


class SplitStatus(str, Enum):
    PENDING = "pending"
    OK = "ok"
    FALLBACK = "fallback"
    UNSPLIT = "unsplit"

    @staticmethod
    def normalize(value: str) -> str:
        try:
            return SplitStatus((value or "").strip()).value
        except ValueError:
            return ""


class SplitReason(str, Enum):
    BELOW_THRESHOLDS = "below_thresholds"
    NO_USABLE_PARTS = "no_usable_parts"
    PART_COUNT_MISMATCH = "part_count_mismatch"
    TOKEN_RECONSTRUCT_FAILED = "token_reconstruct_failed"
    WORD_ALIGNMENT_FAILED = "word_alignment_failed"
    PARSE_FAILED = "parse_failed"
    EXCEPTION = "exception"
    AI_SPLIT_INVALID = "ai_split_invalid"

    @staticmethod
    def normalize(value: str) -> str:
        try:
            return SplitReason((value or "").strip()).value
        except ValueError:
            return ""


class AssOutputMode(str, Enum):
    SOURCE = "source"
    TARGET = "target"
    BILINGUAL = "bilingual"

    @staticmethod
    def normalize(value: "AssOutputMode | str") -> "AssOutputMode":
        if isinstance(value, AssOutputMode):
            return value
        try:
            return AssOutputMode(str(value).strip())
        except ValueError as e:
            raise ValueError(f"unknown ASS output mode: {value}") from e


@dataclass
class TranscriptWord:
    text: str
    start: Optional[float] = None
    end: Optional[float] = None
    score: Optional[float] = None

    @staticmethod
    def from_json(data: dict) -> "TranscriptWord":
        return TranscriptWord(
            text=str(data.get("word") or data.get("text") or "").strip(),
            start=_float_or_none(data.get("start")),
            end=_float_or_none(data.get("end")),
            score=_float_or_none(data.get("score")),
        )

    def to_json(self) -> dict:
        result = {"word": self.text}
        if self.start is not None:
            result["start"] = round(self.start, 3)
        if self.end is not None:
            result["end"] = round(self.end, 3)
        if self.score is not None:
            result["score"] = self.score
        return result


@dataclass
class SplitEvent:
    start: float
    end: float
    en: str
    zh: str

    @staticmethod
    def from_json(data: dict) -> "SplitEvent":
        return SplitEvent(
            start=float(data.get("start", 0.0)),
            end=float(data.get("end", 0.0)),
            en=str(data.get("en", "")),
            zh=str(data.get("zh", "")),
        )

    def to_json(self) -> dict:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "en": self.en,
            "zh": self.zh,
        }


@dataclass
class TranscriptSegment:
    index: int
    start: float
    end: float
    text: str
    words: list[TranscriptWord] = field(default_factory=list)
    proofread_text: str = ""
    translation: str = ""
    split_events: list[SplitEvent] = field(default_factory=list)
    split_status: str = ""
    split_reason: str = ""
    split_reason_detail: str = ""
    original_start: Optional[float] = None
    original_end: Optional[float] = None

    @staticmethod
    def from_json(index: int, data: dict) -> "TranscriptSegment":
        words = [TranscriptWord.from_json(w) for w in data.get("words", [])]
        start = _float_or_none(data.get("start"))
        end = _float_or_none(data.get("end"))
        if start is None:
            starts = [w.start for w in words if w.start is not None]
            start = min(starts) if starts else 0.0
        if end is None:
            ends = [w.end for w in words if w.end is not None]
            end = max(ends) if ends else start
        events = [SplitEvent.from_json(e) for e in data.get("split_events", [])]
        return TranscriptSegment(
            index=int(data.get("id", index)),
            start=float(start),
            end=float(end),
            text=str(data.get("text", "")).strip(),
            words=words,
            proofread_text=str(data.get("proofread_text", "")).strip(),
            translation=str(data.get("translation", "")).strip(),
            split_events=events,
            split_status=SplitStatus.normalize(str(data.get("split_status", ""))),
            split_reason=SplitReason.normalize(str(data.get("split_reason", ""))),
            split_reason_detail=str(data.get("split_reason_detail", "")).strip(),
            original_start=_float_or_none(data.get("original_start")),
            original_end=_float_or_none(data.get("original_end")),
        )

    def en_text(self) -> str:
        return self.proofread_text or self.text

    def source_text(self) -> str:
        return self.text

    def to_json(self) -> dict:
        data = {
            "id": self.index,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
            "words": [w.to_json() for w in self.words],
        }
        if self.original_start is not None:
            data["original_start"] = round(self.original_start, 3)
        if self.original_end is not None:
            data["original_end"] = round(self.original_end, 3)
        if self.proofread_text:
            data["proofread_text"] = self.proofread_text
        if self.translation:
            data["translation"] = self.translation
        if self.split_events:
            data["split_events"] = [e.to_json() for e in self.split_events]
        if self.split_status:
            data["split_status"] = self.split_status
        if self.split_reason:
            data["split_reason"] = self.split_reason
        if self.split_reason_detail:
            data["split_reason_detail"] = self.split_reason_detail
        return data


@dataclass
class Transcript:
    path: str
    language: str
    segments: list[TranscriptSegment]
    raw: dict = field(default_factory=dict)

    @property
    def dir(self) -> str:
        return os.path.dirname(os.path.abspath(self.path))

    @property
    def base(self) -> str:
        name = os.path.splitext(os.path.basename(self.path))[0]
        return name[:-11] if name.endswith(".beautified") else name

    def text_lines(self) -> list[str]:
        return [s.en_text() for s in self.segments]

    def to_json(self) -> dict:
        data = dict(self.raw)
        data["language"] = self.language
        data["segments"] = [s.to_json() for s in self.segments]
        data["pipeline"] = {
            "source": "translate_srt.py",
            "format": "json-first-transcript",
        }
        return data


@dataclass
class TranscriptContext:
    input_json: str
    dir: str
    base: str
    source_lang: str
    target_lang: str
    beautified_json: str
    split_source_srt: str
    split_target_srt: str
    proofread_ass: str
    target_ass: str
    bilingual_ass: str
    desc: str
    target_desc: str
    source_lang_code: str
    target_lang_code: str
    info_json: str
    tags: str
    glossary: str
    scenes_json: str
    scenechange_txt: str

    @staticmethod
    def from_json(
        json_path: str,
        output_ass: str = "",
        source_lang: str = "source",
        target_lang: str = "zh",
    ) -> "TranscriptContext":
        abs_path = os.path.abspath(json_path)
        directory = os.path.dirname(abs_path)
        name = os.path.splitext(os.path.basename(abs_path))[0]
        base = name[:-11] if name.endswith(".beautified") else name
        source_suffix = iso_639_suffix(source_lang, "source")
        target_suffix = iso_639_suffix(target_lang, "target")
        return TranscriptContext(
            input_json=abs_path,
            dir=directory,
            base=base,
            source_lang=source_lang,
            target_lang=target_lang,
            beautified_json=os.path.join(directory, f"{base}.beautified.json"),
            split_source_srt=os.path.join(directory, f"{base}.split.{source_suffix}.srt"),
            split_target_srt=os.path.join(directory, f"{base}.split.{target_suffix}.srt"),
            proofread_ass=os.path.join(directory, f"{base}.{source_suffix}.proofread.ass"),
            target_ass=os.path.join(directory, f"{base}.{target_suffix}.ass"),
            bilingual_ass=output_ass or os.path.join(directory, f"{base}.{source_suffix}-{target_suffix}.ass"),
            desc=os.path.join(directory, f"{base}.description"),
            target_desc=os.path.join(directory, f"{base}.{target_suffix}.description"),
            source_lang_code=source_suffix,
            target_lang_code=target_suffix,
            info_json=os.path.join(directory, f"{base}.info.json"),
            tags=os.path.join(directory, f"{base}.tags.txt"),
            glossary=os.path.join(directory, "glossary.md"),
            scenes_json=os.path.join(directory, f"{base}.scenes.json"),
            scenechange_txt=os.path.join(directory, f"{base}.scenechange.txt"),
        )


@dataclass
class LLMConfig:
    provider: str
    model: str = ""
    proofread_provider: str = ""
    proofread_model: str = ""
    api_key: Optional[str] = None
    batch_size: int = 50
    proofread_batch_size: int = 0
    proofread_retrieval_top_k: int = 1
    proofread_max_tokens: int = 8192

    def pr_provider(self) -> str:
        return self.proofread_provider or self.provider

    def pr_model(self) -> str:
        return self.proofread_model or self.model

    def resolve_key(self) -> str:
        if self.api_key is None:
            self.api_key = get_api_key(
                self.provider, load_env(os.path.dirname(os.path.abspath(__file__)))
            )
        return self.api_key

    def cfg(self) -> dict:
        providers = load_providers()
        if self.provider not in providers:
            print(f"Error: unknown provider: {self.provider}", file=sys.stderr)
            print(f"Available: {', '.join(providers)}", file=sys.stderr)
            sys.exit(1)
        return providers[self.provider]

    def model_name(self) -> str:
        return self.model or self.cfg().get("default_model", "")

    def _client(self):
        provider_cfg = self.cfg()
        return OpenAI(
            base_url=provider_cfg["url"],
            api_key=self.resolve_key(),
            default_headers=provider_cfg.get("extra_headers", {}),
        )


@dataclass
class EmbeddingConfig:
    enabled: bool = False
    provider: str = "openai"
    model: str = "text-embedding-3-small"
    store: str = "chroma"
    chroma_dir: str = ""
    top_k: int = 6
    chunk_chars: int = 800
    batch_size: int = 64

    @staticmethod
    def from_env(env: dict[str, str], ctx: TranscriptContext) -> "EmbeddingConfig":
        enabled = env_flag(env.get("EMBEDDING_ENABLED", "0"))
        provider = env.get("EMBEDDING_PROVIDER", "openai") or "openai"
        model = env.get("EMBEDDING_MODEL", "text-embedding-3-small") or "text-embedding-3-small"
        store = (env.get("EMBEDDING_STORE", "chroma") or "chroma").lower()
        chroma_dir = env.get("EMBEDDING_CHROMA_DIR", "") or os.path.join(ctx.dir, "chroma_db")
        return EmbeddingConfig(
            enabled=enabled,
            provider=provider,
            model=model,
            store=store,
            chroma_dir=chroma_dir,
            top_k=env_int(env.get("EMBEDDING_TOP_K", ""), 6),
            chunk_chars=env_int(env.get("EMBEDDING_CHUNK_CHARS", ""), 800),
            batch_size=env_int(env.get("EMBEDDING_BATCH_SIZE", ""), 64),
        )


@dataclass
class EmbeddingChunk:
    chunk_id: str
    source: str
    text: str
    start: Optional[float] = None
    end: Optional[float] = None
    metadata: dict = field(default_factory=dict)

    def to_document(self) -> Document:
        metadata = {
            "id": self.chunk_id,
            "source": self.source,
        }
        if self.start is not None:
            metadata["start"] = self.start
        if self.end is not None:
            metadata["end"] = self.end
        metadata.update(self.metadata)
        return Document(page_content=self.text, metadata=metadata)


class EmbeddingRetriever:
    def __init__(self, config: EmbeddingConfig, env: dict[str, str]):
        self.config = config
        self.vector_store = open_chroma_store(config, env)

    def retrieve_texts(self, texts: list[str], top_k: Optional[int] = None) -> list[list[dict]]:
        clean_texts = [text.strip() for text in texts]
        if not clean_texts:
            return []
        limit = top_k or self.config.top_k
        return [
            documents_to_retrieved_context(self.vector_store.similarity_search(text, k=limit))
            for text in clean_texts
        ]


def documents_to_retrieved_context(documents: list[Document]) -> list[dict]:
    contexts: list[dict] = []
    for doc in documents:
        metadata = dict(doc.metadata or {})
        data = {
            "id": str(metadata.pop("id", "")),
            "source": str(metadata.pop("source", "")),
            "text": doc.page_content,
        }
        for key in ("start", "end"):
            if key in metadata:
                data[key] = metadata.pop(key)
        if metadata:
            data["metadata"] = metadata
        contexts.append(data)
    return contexts


def embedding_function(config: EmbeddingConfig, env: dict[str, str]) -> OpenAIEmbeddings:
    providers = load_providers()
    if config.provider not in providers:
        raise ValueError(f"unknown embedding provider: {config.provider}")
    provider_cfg = providers[config.provider]
    key_name = provider_cfg["env_key"]
    api_key = env.get(key_name, "")
    if not api_key:
        raise ValueError(f"{key_name} not found in environment or .env file")
    return OpenAIEmbeddings(
        base_url=provider_cfg["url"],
        api_key=api_key,
        model=config.model,
        default_headers=provider_cfg.get("extra_headers", {}),
        check_embedding_ctx_length=False,
    )


def open_chroma_store(config: EmbeddingConfig, env: dict[str, str]) -> Chroma:
    if config.store != "chroma":
        raise ValueError(f"unsupported EMBEDDING_STORE={config.store}; only chroma is available")
    os.makedirs(config.chroma_dir, exist_ok=True)
    return Chroma(
        persist_directory=config.chroma_dir,
        embedding_function=embedding_function(config, env),
    )


def build_embedding_chunks(transcript: Transcript, chunk_chars: int) -> list[EmbeddingChunk]:
    chunks: list[EmbeddingChunk] = []
    current_segments: list[TranscriptSegment] = []
    current_lines: list[str] = []
    current_len = 0
    max_chars = max(1, chunk_chars)

    def flush() -> None:
        nonlocal current_segments, current_lines, current_len
        if not current_segments:
            return
        first = current_segments[0]
        last = current_segments[-1]
        if first.index == last.index:
            chunk_id = f"transcript:{first.index}"
        else:
            chunk_id = f"transcript:{first.index}-{last.index}"
        chunks.append(
            EmbeddingChunk(
                chunk_id=chunk_id,
                source="transcript",
                text="\n".join(current_lines),
                start=first.start,
                end=last.end,
                metadata={
                    "language": transcript.language,
                    "segment_ids": [seg.index for seg in current_segments],
                },
            )
        )
        current_segments = []
        current_lines = []
        current_len = 0

    for seg in transcript.segments:
        text = seg.source_text().strip()
        if not text:
            continue
        line = f"[{seg.index}] {text}"
        extra_len = len(line) + (1 if current_lines else 0)
        if current_lines and current_len + extra_len > max_chars:
            flush()
        current_segments.append(seg)
        current_lines.append(line)
        current_len += extra_len

    flush()
    return chunks


def build_translation_memory_chunks(transcript: Transcript, ctx: TranscriptContext) -> list[EmbeddingChunk]:
    chunks: list[EmbeddingChunk] = []
    for seg in transcript.segments:
        events = seg.split_events or []
        if not events and seg.translation.strip():
            events = [SplitEvent(seg.start, seg.end, seg.source_text(), seg.translation)]
        for event_index, event in enumerate(events, 1):
            source_text = event.en.strip()
            target_text = event.zh.strip()
            if not source_text or not target_text:
                continue
            chunks.append(
                EmbeddingChunk(
                    chunk_id=f"translation_memory:{seg.index}:{event_index}",
                    source="translation_memory",
                    text=(
                        f"[{seg.index}.{event_index}]\n"
                        f"SOURCE({ctx.source_lang_code}): {source_text}\n"
                        f"TARGET({ctx.target_lang_code}): {target_text}"
                    ),
                    start=event.start,
                    end=event.end,
                    metadata={
                        "segment_id": seg.index,
                        "event_index": event_index,
                        "source_lang": ctx.source_lang_code,
                        "target_lang": ctx.target_lang_code,
                    },
                )
            )
    return chunks


def build_embedding_index(
    transcript: Transcript,
    config: EmbeddingConfig,
    env: dict[str, str],
    quiet: bool = False,
    ctx: Optional[TranscriptContext] = None,
) -> str:
    if not config.enabled:
        return ""
    chunks = build_embedding_chunks(transcript, config.chunk_chars)
    if ctx is not None:
        chunks.extend(build_translation_memory_chunks(transcript, ctx))
    store = open_chroma_store(config, env)
    batch_size = max(1, int(config.batch_size or 1))
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        store.add_documents(
            [chunk.to_document() for chunk in batch],
            ids=[chunk.chunk_id for chunk in batch],
        )
    if not quiet:
        print(f"Embedding index: {config.chroma_dir} ({len(chunks)} chunks)", file=sys.stderr)
    return config.chroma_dir


def env_flag(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def env_int(value: str, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default

@dataclass
class BeautifyOptions:
    scene_threshold: float = 0.15
    snap_frames: int = 7
    end_offset_frames: int = 2
    min_scene_interval_frames: int = 2
    min_duration: float = 1.0
    max_duration: float = 8.0
    min_gap: float = 0.083
    max_gap_merge: float = 0.5
    no_scene_snap: bool = False
    aggressive: bool = False
    fps: float = 24.0


@dataclass
class SplitConfig:
    enabled: bool = True
    max_chars: int = 60
    max_duration: float = 3.0
    context_window: int = 1


# --- Providers/env/prompts ----------------------------------------------------


def safe_lang_suffix(value: str, fallback: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9_-]+", "-", (value or "").strip().lower()).strip("-")
    return suffix or fallback


def iso_639_suffix(value: str, fallback: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return fallback

    for candidate in (raw, raw.replace("_", "-")):
        try:
            standardized = langcodes.standardize_tag(candidate)
            language = langcodes.Language.get(standardized)
            if language.is_valid() and language.language and language.language != "und":
                return safe_lang_suffix(language.language, fallback)
        except Exception:
            pass
    try:
        language = langcodes.find(raw)
        if language.language and language.language != "und":
            return safe_lang_suffix(language.language, fallback)
    except Exception:
        pass

    aliases = {
        "english": "en",
        "eng": "en",
        "japanese": "ja",
        "jpn": "ja",
        "korean": "ko",
        "kor": "ko",
        "chinese": "zh",
        "mandarin": "zh",
        "cmn": "zh",
        "zho": "zh",
        "chi": "zh",
        "simplified chinese": "zh",
        "chinese simplified": "zh",
        "traditional chinese": "zh",
        "chinese traditional": "zh",
        "french": "fr",
        "fra": "fr",
        "fre": "fr",
        "german": "de",
        "deu": "de",
        "ger": "de",
        "spanish": "es",
        "spa": "es",
        "italian": "it",
        "ita": "it",
        "portuguese": "pt",
        "por": "pt",
        "russian": "ru",
        "rus": "ru",
    }
    lowered = re.sub(r"\s+", " ", raw.lower())
    return aliases.get(lowered) or safe_lang_suffix(raw, fallback)


def language_prompt_name(value: str, fallback: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return fallback

    aliases = {
        "en": "English",
        "eng": "English",
        "ja": "Japanese",
        "jpn": "Japanese",
        "ko": "Korean",
        "kor": "Korean",
        "zh": "Chinese",
        "zh-hans": "Simplified Chinese",
        "zh-cn": "Simplified Chinese",
        "zh-hant": "Traditional Chinese",
        "zh-tw": "Traditional Chinese",
        "cmn": "Mandarin Chinese",
        "fr": "French",
        "fra": "French",
        "fre": "French",
        "de": "German",
        "deu": "German",
        "ger": "German",
        "es": "Spanish",
        "spa": "Spanish",
        "it": "Italian",
        "ita": "Italian",
        "pt": "Portuguese",
        "por": "Portuguese",
        "ru": "Russian",
        "rus": "Russian",
    }
    lowered = re.sub(r"\s+", " ", raw.lower().replace("_", "-"))
    if lowered in aliases:
        return aliases[lowered]

    for candidate in (raw, raw.replace("_", "-")):
        try:
            standardized = langcodes.standardize_tag(candidate)
            language = langcodes.Language.get(standardized)
            if language.is_valid() and language.language and language.language != "und":
                return language.display_name("en")
        except Exception:
            pass
    try:
        language = langcodes.find(raw)
        if language.language and language.language != "und":
            return language.display_name("en")
    except Exception:
        pass

    return raw


def render_prompt_template(text: str, ctx: TranscriptContext) -> str:
    return render_language_template(
        text,
        ctx.source_lang,
        ctx.target_lang,
        ctx.source_lang_code,
        ctx.target_lang_code,
    )


def render_language_template(
    text: str,
    source_lang: str,
    target_lang: str,
    source_lang_code: str = "",
    target_lang_code: str = "",
) -> str:
    replacements = {
        "${SOURCE_LANG}": language_prompt_name(source_lang, "source language"),
        "${TARGET_LANG}": language_prompt_name(target_lang, "target language"),
        "${SOURCE_LANG_CODE}": source_lang_code or iso_639_suffix(source_lang, "source"),
        "${TARGET_LANG_CODE}": target_lang_code or iso_639_suffix(target_lang, "target"),
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def normalized_response_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def response_key_candidates(ctx: TranscriptContext, side: str) -> set[str]:
    if side == "source":
        values = [ctx.source_lang_code]
    else:
        values = [ctx.target_lang_code]
    return {normalized_response_key(value) for value in values if value}


def get_language_keyed_value(item: dict, candidates: set[str]):
    for key, value in item.items():
        if normalized_response_key(str(key)) in candidates:
            return value
    return None


_BUILTIN_PROVIDERS = {
    "openai": {
        "url": "https://api.openai.com/v1",
        "default_model": "gpt-4.1-mini",
        "env_key": "OPENAI_API_KEY",
        "auth_header": "Bearer {api_key}",
        "extra_headers": {},
    },
    "llama": {
        "url": "http://localhost:11434/v1",
        "default_model": "llama3.1",
        "env_key": "OLLAMA_API_KEY",
        "auth_header": "Bearer {api_key}",
        "extra_headers": {},
    },
    "openrouter": {
        "url": "https://openrouter.ai/api/v1",
        "default_model": "anthropic/claude-sonnet-4-6",
        "env_key": "OPENROUTER_API_KEY",
        "auth_header": "Bearer {api_key}",
        "extra_headers": {
            "HTTP-Referer": "https://github.com/oculr/Subtitle-translation",
            "X-Title": "Subtitle Translation",
        },
    },
    "deepseek": {
        "url": "https://api.deepseek.com",
        "default_model": "deepseek-v4-pro",
        "env_key": "DEEPSEEK_API_KEY",
        "auth_header": "Bearer {api_key}",
        "extra_headers": {},
        "response_format": {"type": "json_object"},
    },
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-2.5-pro",
        "env_key": "GEMINI_API_KEY",
        "auth_header": "Bearer {api_key}",
        "extra_headers": {},
    },
}

_providers_cache = None

_TRANSLATE_PROMPT_FALLBACK = """You are a professional subtitle translator. Translate from ${SOURCE_LANG} to ${TARGET_LANG}.

Rules:
- The user message is a JSON object with an "items" array of transcript segment objects.
- Translate each item object to natural, fluent ${TARGET_LANG} subtitles.
- The input items are complete transcript sentences/segments, not subtitle fragments.
- Preserve meaning and tone. Do not omit, merge, split, or add items.
- Keep proper nouns, brand names, and technical terms in original form unless a standard ${TARGET_LANG} translation exists.
- Follow natural subtitle formatting for ${TARGET_LANG}."""

_PROOFREAD_PROMPT_FALLBACK = """You are a bilingual subtitle proofreader. Review each already-split ${SOURCE_LANG}/${TARGET_LANG} subtitle event and fix both languages.

Step 1 - Check the source-language text for ASR errors:
- Homophone confusion, garbled terms, missing negation, obvious grammar breaks.
- Fix only errors that affect correctness or readability.
- Keep the original source-language sentence structure and word order. Do not rewrite, paraphrase, merge, split, or reorder source text.
- Source-language edits should normally be single-word or short-phrase ASR corrections so word-level timing remains traceable.

Step 2 - Check the ${TARGET_LANG} translation against the corrected source:
- Fix mistranslations, omissions, added content, awkward phrasing, and tone mismatches.
- Follow the glossary if provided.
- Follow natural subtitle formatting for ${TARGET_LANG}.

Do not merge, split, reorder, add, or remove events. Timing has already been aligned and must not be changed."""

_SPLIT_PROMPT_FALLBACK = r"""Style preference:
- Split only at natural pause points such as commas, clause boundaries, conjunctions, and breath groups.
- Use as many split parts as the sentence naturally needs. There is no two-part limit; long multi-clause segments may become 3, 4, 5, or more subtitle events.
- Prefer coherent subtitle events over tiny fragments, but do not keep a long multi-clause segment under-split just to avoid more than two parts.
- Keep each split event readable as a complete thought.
- For the source-language array, split by copying exact contiguous spans from the input source text. Do not correct, remove, add, or paraphrase source-language words."""

_JSON_FORMAT = """MANDATORY JSON PROTOCOL:
The user message is JSON. Your response must be machine-parseable JSON only.
No Markdown. No explanation. No prose before or after the JSON.
These rules apply to this entire LLM stage and override any conflicting style preference.

ABSOLUTE JSON RULES:
1. Every key and every string value must use double quotes `"`.
2. Never use single quotes `'` for keys, strings, arrays, or objects.
3. Apostrophes inside natural-language words are ordinary characters: write `"don't"` and `"I've"`, not `'don't'`.
4. Escape literal double quotes as `\"` and literal backslashes as `\\`.
5. No trailing commas.
6. Process only the JSON in the user message.

Never imitate Python dict syntax. Single-quoted pseudo-JSON will be rejected."""

_JSON_BATCH_FORMAT = """Return a JSON object.
The first response character must be `{` and the last response character must be `}`.
The object must have exactly one top-level key: "items".
"items" must be a JSON array with one object per input item.
Preserve each input item's exact `id`. Do not renumber."""

_JSON_OBJECT_FORMAT = """Return a JSON object.
The first response character must be `{` and the last response character must be `}`."""

_RETRIEVED_CONTEXT_RULES = """RETRIEVED CONTEXT:
Some input items may include a "retrieved_context" array from the same transcript.
Use it only for terminology, names, recurring concepts, tone, and local consistency.
Do not translate, proofread, split, merge, or return retrieved_context items."""

_TRANSLATE_FORMAT = """
TRANSLATION RESPONSE FORMAT:
Return exactly these keys in each "items" object: "id", "${TARGET_LANG_CODE}".
"${TARGET_LANG_CODE}" is the ${TARGET_LANG} translation string.

GOOD:
{"items": [
  {"id": 1, "${TARGET_LANG_CODE}": "<target translation>"},
  {"id": 2, "${TARGET_LANG_CODE}": "<target translation>"}
]}

The placeholder values above are format markers only. In your actual response, replace them with translated text."""

_SPLIT_FORMAT = """SPLIT RESPONSE FORMAT:
Return exactly these keys in each "items" object: "id", "${SOURCE_LANG_CODE}", "${TARGET_LANG_CODE}".
"${SOURCE_LANG_CODE}" is the ${SOURCE_LANG} split text array. "${TARGET_LANG_CODE}" is the ${TARGET_LANG} split text array.
Split by adding multiple strings inside "${SOURCE_LANG_CODE}" and "${TARGET_LANG_CODE}".
The arrays may contain 1, 2, 3, 4, 5, or more strings. Choose the count from natural sentence boundaries; do not cap splits at two parts.

CONTEXT RULES:
- Input items may include "context_before" and "context_after" arrays.
- Use context only to understand rhythm, references, and surrounding meaning.
- Split only the item's own "${SOURCE_LANG_CODE}" and "${TARGET_LANG_CODE}" fields.
- Do not return context items. Return exactly one output item for each input item id.

SOURCE-LANGUAGE HARD RULES:
- "${SOURCE_LANG_CODE}" must be made only by inserting split boundaries into the exact input "${SOURCE_LANG_CODE}" string.
- Preserve every source-language word, repeated word, filler, typo, and ASR artifact in order.
- Do not correct grammar, deduplicate repeated words, remove fillers, normalize wording, paraphrase, or improve readability in "${SOURCE_LANG_CODE}".
- If the input says "to to", output "to to"; if it says "how you how you", output "how you how you".
- When all "${SOURCE_LANG_CODE}" strings are joined with one space, the result must match the input source text token-for-token.

TARGET-LANGUAGE RULES:
- "${TARGET_LANG_CODE}" must have the same number of strings as "${SOURCE_LANG_CODE}".
- Each "${TARGET_LANG_CODE}" string translates the matching source split at the same array index.
- You may make the target-language text natural, but do not merge, omit, or move content across split indexes.

GOOD:
{"items": [
  {"id": 1, "${SOURCE_LANG_CODE}": ["you don't know if you can get to it", "you're learning something about discipline and how you how you push yourself", "and what does motivate you", "and what do you really want out of this life"], "${TARGET_LANG_CODE}": ["<target part 1>", "<target part 2>", "<target part 3>", "<target part 4>"]},
  {"id": 2, "${SOURCE_LANG_CODE}": ["you're actually being honest and earnest", "in your attempt to to pull something from who you are", "and what you understand"], "${TARGET_LANG_CODE}": ["<target part 1>", "<target part 2>", "<target part 3>"]},
  {"id": 3, "${SOURCE_LANG_CODE}": ["<source full sentence>"], "${TARGET_LANG_CODE}": ["<target full sentence>"]}
]}

The placeholder values above are format markers only. In your actual response, replace them with split text from the provided segment."""

_GLOSSARY_PROMPT_FALLBACK = """You are a terminology expert. Analyze the ${SOURCE_LANG} transcript and metadata to prepare glossary content for ${TARGET_LANG} subtitle translation.

Content requirements:
- Title the glossary for the current video.
- Write a short background summary in ${TARGET_LANG}.
- Include a terminology table with source terms, recommended ${TARGET_LANG} translations, and brief rationale.
- Include tone guidance in ${TARGET_LANG}.
- Include key arguments in ${TARGET_LANG}.

Rules:
- Only include terms that actually appear in the transcript.
- Search results can verify standard ${TARGET_LANG} translations.
- If uncertain, mark with (?).
- Keep under 100 lines.
- Do not include greetings, meta commentary, implementation notes, or tool/runtime details.
- Do not mention whether web search was used."""

_GLOSSARY_FORMAT = """MANDATORY GLOSSARY JSON PROTOCOL:
The user message is JSON. Your response must be one machine-parseable JSON object only.
The first response character must be `{` and the last response character must be `}`.
Do not wrap the response in a code fence. Do not add prose before or after the JSON object.

Return exactly one top-level key: "markdown".
The "markdown" value must be a JSON string containing the complete glossary document in Markdown.

Markdown syntax is allowed only inside the JSON string value named "markdown".
Never output raw Markdown outside the JSON object.

Required shape:
{"markdown": "# 术语知识库 - <title>\\n\\n## 背景\\n<content>\\n\\n## 核心术语\\n| 原文术语 | ${TARGET_LANG} 推荐译法 | 说明 |\\n|---|---|---|\\n| source term | recommended translation | reason |\\n\\n## 态度基调\\n- <content>\\n\\n## 关键论点\\n- <content>"}

JSON string rules:
1. Every key and every string value must use double quotes `"`.
2. Escape literal double quotes inside markdown text as `\"`.
3. Escape literal backslashes as `\\`.
4. Encode line breaks inside the markdown string as `\\n`.
5. No trailing commas."""

STRUCTURED_MAX_TOKENS = 32768
_PROOFREAD_FORMAT = """PROOFREAD RESPONSE FORMAT:
Return exactly these keys in each "items" object: "id", "${SOURCE_LANG_CODE}", "${TARGET_LANG_CODE}".
{"items": [
  {"id": 1, "${SOURCE_LANG_CODE}": "<corrected source text>", "${TARGET_LANG_CODE}": "<corrected target translation>"},
  {"id": 2, "${SOURCE_LANG_CODE}": "<corrected source text>", "${TARGET_LANG_CODE}": "<corrected target translation>"}
]}

The placeholder values above are format markers only. In your actual response, replace them with corrected text from the provided subtitle events.
Do not output Source: or Target: labels inside values. Do not use separators like |||.
Do not merge, split, or reorder items."""


def load_providers() -> dict:
    global _providers_cache
    if _providers_cache is not None:
        return _providers_cache
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "providers.json")
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if loaded:
                providers = dict(_BUILTIN_PROVIDERS)
                providers.update(loaded)
                _providers_cache = providers
                return _providers_cache
        except (json.JSONDecodeError, OSError):
            pass
    _providers_cache = dict(_BUILTIN_PROVIDERS)
    return _providers_cache


def load_env(script_dir: str) -> dict[str, str]:
    env = dict(os.environ)
    env_path = os.path.join(script_dir, ".env")
    if os.path.isfile(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip()
                if key and key not in env:
                    env[key] = val
    return env


def get_api_key(provider: str, env: dict[str, str]) -> str:
    key_name = load_providers()[provider]["env_key"]
    key = env.get(key_name, "")
    if not key:
        print(f"Error: {key_name} not found in environment or .env file.", file=sys.stderr)
        print(f"Set it in .env: {key_name}=your_key_here", file=sys.stderr)
        sys.exit(1)
    return key


def load_prompt(filename: str, fallback: str) -> str:
    base = os.path.dirname(os.path.abspath(__file__))
    for suffix in (".md", ".example.md"):
        path = os.path.join(base, filename + suffix)
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    return content
            except OSError:
                pass
    return fallback


def _read_text_file(filepath: str) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            with open(filepath, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    with open(filepath, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def subprocess_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


# --- JSON load/save -----------------------------------------------------------


def _float_or_none(value) -> Optional[float]:
    if value is None:
        return None
    try:
        if isinstance(value, float) and math.isnan(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def load_transcript(json_path: str) -> Transcript:
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    segments = [
        TranscriptSegment.from_json(i + 1, seg)
        for i, seg in enumerate(data.get("segments", []))
    ]
    if not segments:
        print(f"Error: no segments found in JSON: {json_path}", file=sys.stderr)
        sys.exit(1)
    return Transcript(
        path=os.path.abspath(json_path),
        language=str(data.get("language", "en")),
        segments=segments,
        raw={k: v for k, v in data.items() if k != "segments"},
    )


def save_transcript(transcript: Transcript, output_path: str) -> None:
    transcript.path = os.path.abspath(output_path)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(transcript.to_json(), f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_or_create_beautified(
    ctx: TranscriptContext,
    source: Transcript,
    video_path: str,
    options: BeautifyOptions,
    skip_beautify: bool,
    force: bool,
    quiet: bool,
) -> Transcript:
    if (
        os.path.abspath(source.path) != os.path.abspath(ctx.beautified_json)
        and os.path.isfile(ctx.beautified_json)
        and not force
    ):
        if not quiet:
            print(f"Beautified JSON cache: {ctx.beautified_json}")
        return load_transcript(ctx.beautified_json)

    transcript = source
    if skip_beautify:
        if not quiet:
            print("Beautify: skipped")
    else:
        scene_changes = beautify_transcript_timeline(transcript, video_path, options, quiet)
        write_scene_change_sidecars(ctx, video_path, options, scene_changes)

    save_transcript(transcript, ctx.beautified_json)
    if not quiet:
        print(f"Beautified JSON: {ctx.beautified_json}")
    return transcript


# --- Beautify timeline --------------------------------------------------------


def get_frame_rate(video_path: str) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=r_frame_rate",
        "-of",
        "csv=p=0",
        video_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        fps_str = subprocess_text(result.stdout).strip()
        if "/" in fps_str:
            num, den = fps_str.split("/")
            return float(num) / float(den) if den != "0" else 24.0
        if fps_str:
            return float(fps_str)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass
    return 24.0


def get_scene_changes(
    video_path: str,
    threshold: float,
    min_interval_sec: float,
    quiet: bool,
) -> list[float]:
    if not video_path or not os.path.isfile(video_path):
        return []
    if not quiet:
        print(f"Scene detection: threshold={threshold:.2f}", file=sys.stderr)
    cmd = [
        "ffmpeg",
        "-i",
        video_path,
        "-filter:v",
        f"select='gt(scene,{threshold})',showinfo",
        "-f",
        "null",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=900)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    times = []
    for line in subprocess_text(result.stderr).splitlines():
        m = re.search(r"pts_time:([0-9.]+)", line)
        if not m:
            continue
        try:
            t = float(m.group(1))
        except ValueError:
            continue
        if not times or t - times[-1] >= min_interval_sec:
            times.append(t)
    return times


def scene_timecode(seconds: float) -> str:
    return srt_time(seconds).replace(",", ".")


def write_scene_change_sidecars(
    ctx: TranscriptContext,
    video_path: str,
    options: BeautifyOptions,
    scene_changes: list[float],
) -> None:
    payload = {
        "video": os.path.abspath(video_path) if video_path else "",
        "fps": options.fps,
        "threshold": options.scene_threshold,
        "min_interval_sec": options.min_scene_interval_frames * (1.0 / options.fps),
        "scene_changes": [
            {
                "index": idx,
                "time": round(float(time), 6),
                "frame": int(round(float(time) * options.fps)),
                "timecode": scene_timecode(float(time)),
            }
            for idx, time in enumerate(scene_changes, 1)
        ],
    }
    with open(ctx.scenes_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(ctx.scenechange_txt, "w", encoding="utf-8") as f:
        for time in scene_changes:
            f.write(f"{float(time):.6f}\n")


def snap_to_previous(value: float, targets: list[float], max_distance: float) -> float:
    for target in reversed(targets):
        if target <= value and value - target <= max_distance:
            return target
        if target < value - max_distance:
            break
    return value


def snap_end_to_scene_before(
    value: float,
    targets: list[float],
    max_distance: float,
    offset: float,
) -> float:
    for target in targets:
        snapped = target - offset
        if snapped >= value and snapped - value <= max_distance:
            return max(0.0, snapped)
        if target > value + max_distance + offset:
            break
    return value


def beautify_transcript_timeline(
    transcript: Transcript,
    video_path: str,
    options: BeautifyOptions,
    quiet: bool = False,
) -> list[float]:
    if options.aggressive:
        options.scene_threshold = 0.08
        options.snap_frames = 12
        options.end_offset_frames = 0
        options.min_scene_interval_frames = 1

    if video_path and os.path.isfile(video_path):
        options.fps = get_frame_rate(video_path)
    frame = 1.0 / options.fps
    snap_window = options.snap_frames * frame
    end_offset = options.end_offset_frames * frame
    min_scene_interval = options.min_scene_interval_frames * frame

    scene_changes = []
    if not options.no_scene_snap and video_path:
        scene_changes = get_scene_changes(
            video_path, options.scene_threshold, min_scene_interval, quiet
        )
    if not quiet:
        print(
            f"Beautify: {len(transcript.segments)} segments, fps={options.fps:.3f}, scenes={len(scene_changes)}",
            file=sys.stderr,
        )

    for seg in transcript.segments:
        if seg.original_start is None:
            seg.original_start = seg.start
        if seg.original_end is None:
            seg.original_end = seg.end

        beautify_segment_words(seg, scene_changes, snap_window, end_offset)

    for prev, cur in zip(transcript.segments, transcript.segments[1:]):
        gap = cur.start - prev.end
        if gap < 0:
            midpoint = (prev.end + cur.start) / 2.0
            prev.end = max(prev.start + frame, midpoint - frame / 2)
            cur.start = max(prev.end + frame, midpoint + frame / 2)
        elif 0 < gap < options.max_gap_merge:
            prev.end = cur.start
        elif gap < options.min_gap:
            cur.start = prev.end + options.min_gap
            if cur.end <= cur.start:
                cur.end = cur.start + options.min_duration
        shift_words_to_segment_bounds(prev)
        shift_words_to_segment_bounds(cur)

    for seg in transcript.segments:
        segment_bounds_from_words(seg)
        seg.split_events = []

    return scene_changes


def beautify_segment_words(
    seg: TranscriptSegment,
    scene_changes: list[float],
    snap_window: float,
    end_offset: float,
) -> None:
    timed_words = [w for w in seg.words if w.start is not None and w.end is not None]
    if not timed_words:
        return

    min_word_duration = 0.01
    for word in timed_words:
        word.start = float(word.start)
        word.end = max(float(word.end), word.start + min_word_duration)

    old_start = float(timed_words[0].start)
    old_end = float(timed_words[-1].end)
    new_start = old_start
    new_end = old_end
    if scene_changes:
        new_start = snap_to_previous(old_start, scene_changes, snap_window)
        new_end = snap_end_to_scene_before(old_end, scene_changes, snap_window, end_offset)

    start_delta = new_start - old_start
    if start_delta:
        for word in timed_words:
            distance = float(word.start) - old_start
            if distance < 0 or distance > snap_window:
                continue
            weight = 1.0 - (distance / snap_window if snap_window > 0 else 1.0)
            word.start += start_delta * weight
            if word.end <= word.start:
                word.end = word.start + min_word_duration

    end_window = snap_window + end_offset
    end_delta = new_end - old_end
    if end_delta:
        for word in timed_words:
            distance = old_end - float(word.end)
            if distance < 0 or distance > end_window:
                continue
            weight = 1.0 - (distance / end_window if end_window > 0 else 1.0)
            word.end += end_delta * weight
            if word.end <= word.start:
                word.start = max(new_start, word.end - min_word_duration)

    for prev, cur in zip(timed_words, timed_words[1:]):
        if cur.start < prev.end:
            midpoint = (prev.end + cur.start) / 2.0
            prev.end = max(prev.start + min_word_duration, midpoint)
            cur.start = max(prev.end, min(midpoint, cur.end - min_word_duration))
        if cur.end <= cur.start:
            cur.end = cur.start + min_word_duration

    segment_bounds_from_words(seg)


def segment_bounds_from_words(seg: TranscriptSegment) -> None:
    timed_words = [w for w in seg.words if w.start is not None and w.end is not None]
    if not timed_words:
        return
    seg.start = float(timed_words[0].start)
    seg.end = float(timed_words[-1].end)


def shift_words_to_segment_bounds(seg: TranscriptSegment) -> None:
    timed_words = [w for w in seg.words if w.start is not None and w.end is not None]
    if not timed_words:
        return
    original_start = float(timed_words[0].start)
    original_end = float(timed_words[-1].end)
    original_duration = max(0.01, original_end - original_start)
    target_duration = max(0.01, seg.end - seg.start)
    scale = target_duration / original_duration
    for word in timed_words:
        word.start = seg.start + (float(word.start) - original_start) * scale
        word.end = seg.start + (float(word.end) - original_start) * scale
        if word.end <= word.start:
            word.end = word.start + 0.01
    timed_words[0].start = seg.start
    timed_words[-1].end = seg.end


# --- Glossary -----------------------------------------------------------------


def load_description(desc_path: str) -> str:
    if not desc_path or not os.path.isfile(desc_path):
        return ""
    try:
        content = _read_text_file(desc_path).strip()
        if len(content) > 2000:
            content = content[:2000].rsplit("\n", 1)[0]
        return (
            "\n\nThe following is the video description. Use it for domain terms, "
            "proper names, and context:\n\n"
            + content
            if content
            else ""
        )
    except OSError:
        return ""


def load_glossary(glossary_path: str) -> str:
    if not glossary_path or not os.path.isfile(glossary_path):
        return ""
    try:
        content = _read_text_file(glossary_path).strip()
    except OSError:
        return ""
    if not content:
        return ""
    return (
        "\n\n以下是本视频的术语知识库, 请在翻译和校对时严格遵循其中的术语理解、"
        "推荐译法、语气判断和一致性要求:\n\n"
        + content
    )


def read_metadata(ctx: TranscriptContext) -> tuple[str, str, list[str]]:
    title = ctx.base
    webpage_url = ""
    tags: list[str] = []
    if os.path.isfile(ctx.info_json):
        try:
            with open(ctx.info_json, "r", encoding="utf-8") as f:
                info = json.load(f)
            title = info.get("title") or title
            webpage_url = info.get("webpage_url") or ""
        except Exception:
            pass
    if os.path.isfile(ctx.tags):
        try:
            raw = _read_text_file(ctx.tags)
            for line in raw.strip().splitlines():
                line = line.strip()
                if line.startswith("[") and line.endswith("]"):
                    try:
                        parsed = __import__("ast").literal_eval(line)
                        if isinstance(parsed, list):
                            tags.extend(str(t) for t in parsed)
                    except (ValueError, SyntaxError):
                        pass
        except Exception:
            pass
    tags = list(dict.fromkeys(tags))
    return title, webpage_url, tags


def read_metadata_header(ctx: TranscriptContext) -> str:
    if not os.path.isfile(ctx.info_json):
        return ""
    try:
        with open(ctx.info_json, "r", encoding="utf-8") as f:
            info = json.load(f)
    except Exception:
        return ""

    title = str(info.get("title") or "")
    webpage_url = str(info.get("webpage_url") or "")
    uploader = str(info.get("uploader") or info.get("channel") or "")
    upload_time = ""
    timestamp = info.get("timestamp")
    if timestamp:
        try:
            from datetime import datetime, timezone

            upload_time = datetime.fromtimestamp(float(timestamp), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
            upload_time = upload_time[:-2] + ":" + upload_time[-2:]
        except (TypeError, ValueError, OSError, OverflowError):
            upload_time = ""
    if not upload_time:
        upload_date = str(info.get("upload_date") or "")
        if len(upload_date) == 8 and upload_date.isdigit():
            upload_time = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"

    return (
        f"原视频：{webpage_url}\n"
        f"原标题：{title}\n"
        f"原作者：{uploader}\n"
        f"上传时间：{upload_time}\n"
        f"\n=====\n\n"
    )


def tavily_search(query: str, api_key: str, max_results: int = 5) -> list[dict]:
    try:
        client = TavilyClient(api_key=api_key)
        data = client.search(query=query, max_results=max_results, search_depth="basic")
        return data.get("results", [])
    except Exception as e:
        print(f"  Warning: Tavily search failed: {e}", file=sys.stderr)
        return []


def build_glossary(
    transcript: Transcript,
    ctx: TranscriptContext,
    llm: LLMConfig,
    tavily_key: str = "",
    tavily_max_results: int = 10,
    quiet: bool = False,
    retriever=None,
) -> str:
    if os.path.isfile(ctx.glossary) and os.path.getsize(ctx.glossary) > 0:
        if not quiet:
            print(f"Glossary cache: {ctx.glossary}", file=sys.stderr)
        return _read_text_file(ctx.glossary)

    title, _, tags = read_metadata(ctx)
    desc_text = _read_text_file(ctx.desc) if os.path.isfile(ctx.desc) else ""
    transcript_text = "\n".join(transcript.text_lines())

    search_text = ""
    if tavily_key:
        all_results = []
        for q in [title] + tags[:5]:
            if not quiet:
                print(f"  Searching: {q[:60]}", file=sys.stderr)
            all_results.extend(tavily_search(q, tavily_key, tavily_max_results))
        seen = set()
        unique = []
        for r in all_results:
            url = r.get("url", "")
            if url and url not in seen:
                seen.add(url)
                unique.append(r)
        search_text = "\n\n".join(
            f"Source: {r.get('url', '')}\n{r.get('content', '')[:500]}"
            for r in unique[:10]
        )

    request_fields = {
        "title": title,
        "transcript_excerpt": transcript_text[:8000],
        "description": desc_text[:1000],
        "tags": tags[:20],
        "search_results": search_text[:4000] if search_text else "",
    }
    if retriever:
        query = "\n".join([title, desc_text[:2000], " ".join(tags[:20]), transcript_text[:4000]]).strip()
        retrieved = retriever.retrieve_texts([query], top_k=12)
        if retrieved:
            request_fields["retrieved_context"] = retrieved[0]

    request = LLMObjectRequest(request_fields)

    if not quiet:
        print(f"Glossary: generating with {llm.provider}", file=sys.stderr)
    raw_response = ""
    try:
        raw_response = llm_text_once(
            llm,
            render_prompt_template(load_prompt("glossary_prompt", _GLOSSARY_PROMPT_FALLBACK), ctx)
            + ("\n\n" + _RETRIEVED_CONTEXT_RULES if retriever else "")
            + "\n\n"
            + render_prompt_template(_GLOSSARY_FORMAT, ctx),
            request,
            max_tokens=4096,
            temperature=0.3,
        )
        glossary_output = GlossaryOutput.from_json_content(raw_response)
        glossary = glossary_output.markdown
    except Exception as e:
        print(f"Warning: glossary generation failed: {e}", file=sys.stderr)
        if raw_response and not quiet:
            preview = raw_response[:2000]
            if len(raw_response) > len(preview):
                preview += "\n... <truncated>"
            print("Glossary raw response:", file=sys.stderr)
            print(preview, file=sys.stderr)
        return ""

    with open(ctx.glossary, "w", encoding="utf-8") as f:
        f.write(glossary)
        f.write("\n")
    if not quiet:
        print(f"Glossary: {ctx.glossary}", file=sys.stderr)
    return glossary


# --- LLM stages ---------------------------------------------------------------


@dataclass
class LLMBatchItem:
    id: int
    fields: dict

    def to_json_value(self) -> dict:
        return {"id": self.id, **self.fields}


@dataclass
class TranslateInputItem:
    id: int
    source_text: str
    ctx: TranscriptContext
    retrieved_context: list[dict] = field(default_factory=list)

    def to_batch_item(self) -> LLMBatchItem:
        fields = {self.ctx.source_lang_code: self.source_text}
        if self.retrieved_context:
            fields["retrieved_context"] = self.retrieved_context
        return LLMBatchItem(self.id, fields)

    def to_json_value(self) -> dict:
        return self.to_batch_item().to_json_value()


@dataclass
class TranslateOutputItem:
    id: int
    target_text: str

    @staticmethod
    def from_json_value(data: dict, ctx: TranscriptContext) -> "TranslateOutputItem":
        target_value = get_language_keyed_value(data, response_key_candidates(ctx, "target"))
        return TranslateOutputItem(int(data.get("id")), str(target_value or "").strip())


@dataclass
class ProofreadInputItem:
    id: int
    source_text: str
    target_text: str
    ctx: TranscriptContext
    retrieved_context: list[dict] = field(default_factory=list)

    def to_batch_item(self) -> LLMBatchItem:
        fields = {
            self.ctx.source_lang_code: self.source_text,
            self.ctx.target_lang_code: self.target_text,
        }
        if self.retrieved_context:
            fields["retrieved_context"] = self.retrieved_context
        return LLMBatchItem(self.id, fields)

    def to_json_value(self) -> dict:
        return self.to_batch_item().to_json_value()


@dataclass
class ProofreadOutputItem:
    id: int
    source_text: str
    target_text: str

    @staticmethod
    def from_json_value(data: dict, ctx: TranscriptContext) -> "ProofreadOutputItem":
        source_value = get_language_keyed_value(data, response_key_candidates(ctx, "source"))
        target_value = get_language_keyed_value(data, response_key_candidates(ctx, "target"))
        return ProofreadOutputItem(
            int(data.get("id")),
            _strip_speaker_labels(str(source_value or "")),
            _strip_speaker_labels(str(target_value or "")),
        )


@dataclass
class SplitContextItem:
    id: int
    source_text: str
    target_text: str
    ctx: TranscriptContext

    def to_json_value(self) -> dict:
        return {
            "id": self.id,
            self.ctx.source_lang_code: self.source_text,
            self.ctx.target_lang_code: self.target_text,
        }


@dataclass
class SplitInputItem:
    id: int
    source_text: str
    target_text: str
    ctx: TranscriptContext
    context_before: list[SplitContextItem] = field(default_factory=list)
    context_after: list[SplitContextItem] = field(default_factory=list)

    def to_batch_item(self) -> LLMBatchItem:
        fields = {
            self.ctx.source_lang_code: self.source_text,
            self.ctx.target_lang_code: self.target_text,
        }
        if self.context_before:
            fields["context_before"] = [item.to_json_value() for item in self.context_before]
        if self.context_after:
            fields["context_after"] = [item.to_json_value() for item in self.context_after]
        return LLMBatchItem(self.id, fields)

    def to_json_value(self) -> dict:
        return self.to_batch_item().to_json_value()


@dataclass
class SplitOutputItem:
    id: int
    source_parts: list[str]
    target_parts: list[str]

    @staticmethod
    def from_json_value(data: dict, ctx: TranscriptContext) -> "SplitOutputItem":
        source_items = get_language_keyed_value(data, response_key_candidates(ctx, "source"))
        target_items = get_language_keyed_value(data, response_key_candidates(ctx, "target"))
        if not isinstance(source_items, list) or not isinstance(target_items, list):
            raise ValueError("language-code values must both be arrays")
        return SplitOutputItem(
            int(data.get("id")),
            [str(p).replace("\\N", " ").strip() for p in source_items if str(p).strip()],
            [str(p).replace("\\N", " ").strip() for p in target_items if str(p).strip()],
        )


@dataclass
class LLMBatchRequest:
    items: list[LLMBatchItem]

    def to_json_value(self) -> dict:
        return {"items": [item.to_json_value() for item in self.items]}

    def to_json_text(self) -> str:
        return json.dumps(self.to_json_value(), ensure_ascii=False, indent=2)


@dataclass
class LLMBatchResponse:
    items: list[dict]

    @staticmethod
    def from_json_value(data) -> "LLMBatchResponse":
        if isinstance(data, dict):
            items = data.get("items")
        elif isinstance(data, list):
            items = data
        else:
            items = None
        if not isinstance(items, list):
            raise ValueError('response is not a JSON object with an "items" array')
        clean_items = [item for item in items if isinstance(item, dict)]
        if len(clean_items) != len(items):
            raise ValueError('response "items" must contain only objects')
        return LLMBatchResponse(clean_items)

    def to_items(self) -> list[dict]:
        return self.items

    def to_translate_outputs(self, ctx: TranscriptContext) -> list[TranslateOutputItem]:
        result: list[TranslateOutputItem] = []
        for item in self.items:
            try:
                result.append(TranslateOutputItem.from_json_value(item, ctx))
            except (TypeError, ValueError):
                continue
        return result

    def to_proofread_outputs(self, ctx: TranscriptContext) -> list[ProofreadOutputItem]:
        result: list[ProofreadOutputItem] = []
        for item in self.items:
            try:
                result.append(ProofreadOutputItem.from_json_value(item, ctx))
            except (TypeError, ValueError):
                continue
        return result

    def to_split_outputs(self, ctx: TranscriptContext) -> list[SplitOutputItem]:
        result: list[SplitOutputItem] = []
        for item in self.items:
            try:
                result.append(SplitOutputItem.from_json_value(item, ctx))
            except (TypeError, ValueError):
                continue
        return result


@dataclass
class LLMObjectRequest:
    fields: dict

    def to_json_value(self) -> dict:
        return dict(self.fields)

    def to_json_text(self) -> str:
        return json.dumps(self.to_json_value(), ensure_ascii=False, indent=2)


@dataclass
class LLMObjectResponse:
    fields: dict

    @staticmethod
    def from_json_value(data) -> "LLMObjectResponse":
        if not isinstance(data, dict):
            raise ValueError("response is not a JSON object")
        return LLMObjectResponse(data)


@dataclass
class GlossaryOutput:
    markdown: str

    @staticmethod
    def from_json_content(content: str) -> "GlossaryOutput":
        parsed = _extract_json_value(content)
        if isinstance(parsed, dict):
            markdown = str(parsed.get("markdown", "")).strip()
            if markdown:
                return GlossaryOutput(markdown)
            raise ValueError('glossary JSON object missing non-empty "markdown"')
        raise ValueError('glossary response is not a JSON object with non-empty "markdown"')


@dataclass
class ChatSession:
    llm: LLMConfig
    system_prompt: str
    temperature: float = 0.3
    messages: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.messages.append({"role": "system", "content": self.system_prompt})

    def ask(self, content: str, max_tokens: int) -> str:
        self.messages.append({"role": "user", "content": content})
        kwargs = {
            "model": self.llm.model_name(),
            "messages": self.messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
        }
        response_format = self.llm.cfg().get("response_format")
        if response_format:
            kwargs["response_format"] = response_format
        resp = self.llm._client().chat.completions.create(**kwargs)
        choice = resp.choices[0]
        message = choice.message
        answer = message.content or ""
        if not answer.strip():
            reasoning = getattr(message, "reasoning_content", None)
            usage = getattr(resp, "usage", None)
            details = [f"finish_reason={getattr(choice, 'finish_reason', 'unknown')}"]
            if reasoning:
                details.append(f"reasoning_chars={len(reasoning)}")
            if usage and getattr(usage, "completion_tokens_details", None):
                reasoning_tokens = getattr(usage.completion_tokens_details, "reasoning_tokens", None)
                if reasoning_tokens is not None:
                    details.append(f"reasoning_tokens={reasoning_tokens}")
            raise RuntimeError(f"LLM returned empty message.content ({', '.join(details)})")
        self.messages.append({"role": "assistant", "content": answer})
        return answer


def llm_json_once(
    llm: LLMConfig,
    system_prompt: str,
    request: LLMObjectRequest,
    max_tokens: int,
    temperature: float = 0.3,
) -> dict:
    session = ChatSession(llm, system_prompt, temperature=temperature)
    content = session.ask(request.to_json_text(), max_tokens=max_tokens)
    return LLMObjectResponse.from_json_value(_extract_json_value(content)).fields


def llm_text_once(
    llm: LLMConfig,
    system_prompt: str,
    request: LLMObjectRequest,
    max_tokens: int,
    temperature: float = 0.3,
) -> str:
    session = ChatSession(llm, system_prompt, temperature=temperature)
    return session.ask(request.to_json_text(), max_tokens=max_tokens)


def _strip_json_fence(content: str) -> str:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json_value(content: str):
    text = _strip_json_fence(content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    starts = [idx for idx in (text.find("["), text.find("{")) if idx != -1]
    if not starts:
        return None
    start = min(starts)
    open_char = text[start]
    close_char = "]" if open_char == "[" else "}"
    end = text.rfind(close_char)
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _extract_json_batch(content: str) -> Optional[LLMBatchResponse]:
    data = _extract_json_value(content)
    try:
        return LLMBatchResponse.from_json_value(data)
    except ValueError:
        return None


def _strip_speaker_labels(text: str) -> str:
    value = text.strip()
    for _ in range(3):
        new_value = re.sub(r"^(?:EN|ZH|SRC|TGT)\s*:\s*", "", value, flags=re.IGNORECASE).strip()
        if new_value == value:
            break
        value = new_value
    return value


def parse_proofread_response(
    data: list,
    expected_ids: list[int],
    fallback_pairs: list[tuple[str, str]],
    ctx: TranscriptContext,
) -> list[tuple[str, str]]:
    if data is not None:
        by_id: dict[int, tuple[str, str]] = {}
        for parsed in LLMBatchResponse([item for item in data if isinstance(item, dict)]).to_proofread_outputs(ctx):
            if parsed.source_text or parsed.target_text:
                by_id[parsed.id] = (parsed.source_text, parsed.target_text)
        return [by_id.get(item_id, fallback_pairs[idx]) for idx, item_id in enumerate(expected_ids)]
    return fallback_pairs


def llm_numbered_batch(
    request: LLMBatchRequest,
    session: ChatSession,
    quiet: bool,
    retries: int = 3,
    max_tokens: Optional[int] = None,
    raise_on_failure: bool = False,
) -> list:
    prompt = request.to_json_text()
    item_count = len(request.items)
    last_error = ""
    for attempt in range(retries):
        try:
            content = session.ask(
                prompt,
                max_tokens=max_tokens or max(4096, item_count * 220),
            )
            data = _extract_json_batch(content)
            if data is None:
                raise ValueError('response is not a JSON object with an "items" array')
            return data.to_items()
        except Exception as e:
            last_error = str(e)
            if raise_on_failure and is_context_length_error(e):
                raise
            if attempt < retries - 1:
                wait = (attempt + 1) * 3
                if not quiet:
                    print(f"  Retry {attempt + 1}/{retries} in {wait}s...", file=sys.stderr)
                time.sleep(wait)
    message = f"LLM batch failed after {retries} attempts: {last_error}"
    if raise_on_failure:
        raise RuntimeError(message)
    print(f"Error: {message}", file=sys.stderr)
    return []


def resolve_proofread_batch_size(llm: LLMConfig) -> int:
    configured = int(getattr(llm, "proofread_batch_size", 0) or 0)
    if configured > 0:
        return configured
    return max(1, int(getattr(llm, "batch_size", 1) or 1) // 2)


def proofread_max_tokens_for_batch(item_count: int, cap: int = 8192) -> int:
    safe_cap = max(1024, int(cap or 8192))
    return min(safe_cap, max(1024, max(1, item_count) * 320))


def is_context_length_error(error: Exception | str) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "maximum context length",
            "context length",
            "too many tokens",
            "reduce the length",
        )
    )


def translate_segments(
    transcript: Transcript,
    ctx: TranscriptContext,
    llm: LLMConfig,
    system_prompt: str,
    quiet: bool,
    retriever=None,
) -> bool:
    pending = [s for s in transcript.segments if not s.translation]
    if not pending:
        if not quiet:
            print("Translate: cached", file=sys.stderr)
        return False

    if not quiet:
        print(f"Translator: {llm.provider} / {llm.model_name()}", file=sys.stderr)
        print(f"Total segments: {len(pending)}", file=sys.stderr)

    session = ChatSession(
        llm,
        system_prompt
        + ("\n\n" + _RETRIEVED_CONTEXT_RULES if retriever else "")
        + "\n\n"
        + _JSON_FORMAT
        + "\n\n"
        + _JSON_BATCH_FORMAT
        + "\n\n"
        + render_prompt_template(_TRANSLATE_FORMAT, ctx),
        temperature=0.3,
    )
    for start in range(0, len(pending), llm.batch_size):
        batch = pending[start : start + llm.batch_size]
        if not quiet:
            print(
                f"  Batch {start // llm.batch_size + 1}/{math.ceil(len(pending) / llm.batch_size)}: "
                f"translating {start + 1}-{start + len(batch)}",
                file=sys.stderr,
            )
        contexts = retriever.retrieve_texts([seg.en_text() for seg in batch]) if retriever else [[] for _ in batch]
        request = LLMBatchRequest(
            [
                TranslateInputItem(s.index, s.en_text(), ctx, retrieved_context=contexts[idx]).to_batch_item()
                for idx, s in enumerate(batch)
            ]
        )
        response_items = llm_numbered_batch(
            request,
            session,
            quiet,
        )
        by_id = {
            parsed.id: parsed.target_text
            for parsed in LLMBatchResponse(response_items).to_translate_outputs(ctx)
        }
        translations = [
            by_id.get(seg.index, "")
            for seg in batch
        ]
        for seg, zh in zip(batch, translations):
            seg.translation = zh.strip()
            seg.split_events = []
    return True


def proofread_split_events(
    transcript: Transcript,
    ctx: TranscriptContext,
    llm: LLMConfig,
    system_prompt: str,
    quiet: bool,
    retriever=None,
) -> bool:
    events: list[SplitEvent] = []
    for seg in transcript.segments:
        events.extend(seg.split_events or [whole_segment_split_event(seg)])
    if not events:
        return False

    pr_llm = LLMConfig(
        provider=llm.pr_provider(),
        model=llm.pr_model(),
        api_key=llm.api_key if llm.pr_provider() == llm.provider else None,
        batch_size=resolve_proofread_batch_size(llm),
        proofread_retrieval_top_k=max(1, int(getattr(llm, "proofread_retrieval_top_k", 1) or 1)),
        proofread_max_tokens=max(1024, int(getattr(llm, "proofread_max_tokens", 8192) or 8192)),
    )
    if not quiet:
        print(f"Proofreader: {pr_llm.provider} / {pr_llm.model_name()}", file=sys.stderr)
        print(f"Total split events: {len(events)}", file=sys.stderr)

    proofread_format = render_prompt_template(_PROOFREAD_FORMAT, ctx)
    session = ChatSession(
        pr_llm,
        system_prompt
        + ("\n\n" + _RETRIEVED_CONTEXT_RULES if retriever else "")
        + "\n\n"
        + _JSON_FORMAT
        + "\n\n"
        + _JSON_BATCH_FORMAT
        + "\n\n"
        + proofread_format,
        temperature=0.2,
    )
    changed = False

    def apply_proofread_batch(
        batch_events: list[SplitEvent],
        item_offset: int,
        batch_contexts: list[list[dict]],
    ) -> bool:
        request = LLMBatchRequest(
            [
                ProofreadInputItem(
                    item_offset + idx + 1,
                    event.en,
                    event.zh,
                    ctx,
                    retrieved_context=batch_contexts[idx],
                ).to_batch_item()
                for idx, event in enumerate(batch_events)
            ]
        )
        try:
            response_items = llm_numbered_batch(
                request,
                session,
                quiet,
                max_tokens=proofread_max_tokens_for_batch(len(batch_events), pr_llm.proofread_max_tokens),
                raise_on_failure=True,
            )
        except Exception as e:
            if len(batch_events) > 1 and is_context_length_error(e):
                mid = len(batch_events) // 2
                if not quiet:
                    print(
                        f"  Proofread batch too large; splitting {item_offset + 1}-"
                        f"{item_offset + len(batch_events)}",
                        file=sys.stderr,
                    )
                left_changed = apply_proofread_batch(
                    batch_events[:mid],
                    item_offset,
                    batch_contexts[:mid],
                )
                right_changed = apply_proofread_batch(
                    batch_events[mid:],
                    item_offset + mid,
                    batch_contexts[mid:],
                )
                return left_changed or right_changed
            if is_context_length_error(e) and any(batch_contexts):
                if not quiet:
                    print(
                        f"  Proofread item {item_offset + 1} too large with retrieved context; retrying without RAG context",
                        file=sys.stderr,
                    )
                return apply_proofread_batch(batch_events, item_offset, [[] for _ in batch_events])
            print(f"Warning: proofread batch failed: {e}", file=sys.stderr)
            response_items = []

        fallback_pairs = [(event.en, event.zh) for event in batch_events]
        parsed_pairs = parse_proofread_response(
            response_items,
            [item.id for item in request.items],
            fallback_pairs,
            ctx,
        )
        batch_changed = False
        for event, (en, zh) in zip(batch_events, parsed_pairs):
            new_en = en.strip() or event.en
            new_zh = zh.strip() or event.zh
            if new_en != event.en or new_zh != event.zh:
                batch_changed = True
            event.en = new_en
            event.zh = new_zh
        return batch_changed

    for start in range(0, len(events), pr_llm.batch_size):
        batch = events[start : start + pr_llm.batch_size]
        contexts = (
            retriever.retrieve_texts(
                [f"{event.en}\n{event.zh}" for event in batch],
                top_k=pr_llm.proofread_retrieval_top_k,
            )
            if retriever
            else [[] for _ in batch]
        )
        if not quiet:
            print(
                f"  Batch {start // pr_llm.batch_size + 1}/{math.ceil(len(events) / pr_llm.batch_size)}: "
                f"proofreading split events {start + 1}-{start + len(batch)}",
                file=sys.stderr,
            )
        changed = apply_proofread_batch(batch, start, contexts) or changed
    return changed


# --- Split and word alignment -------------------------------------------------


def normalize_token_text(text: str) -> str:
    return (
        text.lower()
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201b", "'")
        .replace("\u02bc", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
    )


def is_token_char(char: str) -> bool:
    return unicodedata.category(char)[0] in ("L", "N")


def is_dash_char(char: str) -> bool:
    return unicodedata.category(char) == "Pd" or char in "\u2212"


def text_tokens(text: str) -> list[str]:
    normalized = normalize_token_text(text)
    tokens: list[str] = []
    current: list[str] = []
    for i, char in enumerate(normalized):
        if is_token_char(char):
            current.append(char)
            continue
        if (
            is_dash_char(char)
            and current
            and i + 1 < len(normalized)
            and is_token_char(normalized[i + 1])
        ):
            continue
        if (
            char == "'"
            and current
            and i + 1 < len(normalized)
            and is_token_char(normalized[i + 1])
        ):
            current.append(char)
            continue
        if current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def find_word_span(
    words: list[TranscriptWord],
    phrase: str,
    offset: int,
) -> Optional[tuple[int, int, float, float]]:
    phrase_tokens = text_tokens(phrase)
    if not phrase_tokens:
        return None
    flat: list[tuple[str, int]] = []
    for word_idx, word in enumerate(words):
        for token in text_tokens(word.text):
            flat.append((token, word_idx))
    n = len(phrase_tokens)
    start_token_offset = 0
    while start_token_offset < len(flat) and flat[start_token_offset][1] < offset:
        start_token_offset += 1
    for i in range(start_token_offset, max(start_token_offset, len(flat) - n + 1)):
        if [token for token, _ in flat[i : i + n]] != phrase_tokens:
            continue
        start_word_idx = flat[i][1]
        end_word_idx = flat[i + n - 1][1]
        timed = [w for w in words[start_word_idx : end_word_idx + 1] if w.start is not None and w.end is not None]
        if timed:
            return start_word_idx, end_word_idx + 1, float(timed[0].start), float(timed[-1].end)
    return None


def find_word_index(
    words: list[TranscriptWord],
    token: str,
    start_offset: int,
) -> Optional[int]:
    for i in range(start_offset, len(words)):
        if token in text_tokens(words[i].text):
            return i
    return None


def timed_token_words(segment: TranscriptSegment) -> list[TranscriptWord]:
    return [
        w
        for w in segment.words
        if w.start is not None and w.end is not None and text_tokens(w.text)
    ]


def align_split_events_by_edge_tokens(
    segment: TranscriptSegment,
    en_parts: list[str],
    zh_parts: list[str],
) -> Optional[list[SplitEvent]]:
    words = timed_token_words(segment)
    if not words:
        return None

    events: list[SplitEvent] = []
    offset = 0
    for idx, en in enumerate(en_parts):
        tokens = text_tokens(en)
        if not tokens:
            return None
        first_idx = find_word_index(words, tokens[0], offset)
        if first_idx is None:
            return None
        last_idx = find_word_index(words, tokens[-1], first_idx)
        if last_idx is None:
            return None
        zh = zh_parts[idx] if idx < len(zh_parts) else ""
        events.append(
            SplitEvent(
                start=float(words[first_idx].start),
                end=float(words[last_idx].end),
                en=en,
                zh=zh,
            )
        )
        offset = last_idx + 1

    return events


def clamp_split_events(
    segment: TranscriptSegment,
    events: list[SplitEvent],
) -> list[SplitEvent]:
    events[0].start = segment.start
    events[-1].end = segment.end
    for i, event in enumerate(events):
        event.start = max(segment.start, min(event.start, segment.end))
        event.end = max(event.start + 0.01, min(event.end, segment.end))
        if i > 0 and event.start < events[i - 1].end:
            event.start = events[i - 1].end
            event.end = max(event.start + 0.01, event.end)
    return events


def align_split_events(
    segment: TranscriptSegment,
    source_parts: list[str],
    target_parts: list[str],
) -> Optional[list[SplitEvent]]:
    if text_tokens(" ".join(source_parts)) != text_tokens(segment.source_text()):
        return None

    edged = align_split_events_by_edge_tokens(segment, source_parts, target_parts)
    if edged:
        return clamp_split_events(segment, edged)

    events: list[SplitEvent] = []
    timed_words = timed_token_words(segment)
    offset = 0

    for idx, source_text in enumerate(source_parts):
        target_text = target_parts[idx] if idx < len(target_parts) else ""
        span = find_word_span(timed_words, source_text, offset) if timed_words else None
        if not span:
            return None
        offset = span[1]
        start, end = span[2], span[3]
        events.append(SplitEvent(start=start, end=end, en=source_text, zh=target_text))

    if not events:
        return None

    return clamp_split_events(segment, events)


def parse_split_response(
    data: list,
    expected_ids: list[int],
    ctx: TranscriptContext,
) -> tuple[dict[int, list[str]], dict[int, list[str]], str]:
    source: dict[int, list[str]] = {}
    target: dict[int, list[str]] = {}
    expected_set = set(expected_ids)
    if data is None:
        return source, target, 'response is not a JSON object with an "items" array'

    seen_ids: set[int] = set()
    for pos, item in enumerate(data, 1):
        if not isinstance(item, dict):
            continue
        item_id = item.get("id")
        try:
            item_id_int = int(item_id)
        except (TypeError, ValueError):
            continue
        if item_id_int not in expected_set:
            continue
        if item_id_int in seen_ids:
            continue
        seen_ids.add(item_id_int)
        try:
            parsed = SplitOutputItem.from_json_value(item, ctx)
        except (TypeError, ValueError):
            continue
        if parsed.source_parts:
            source[item_id_int] = parsed.source_parts
            target[item_id_int] = parsed.target_parts
    return source, target, ""


def whole_segment_split_event(segment: TranscriptSegment) -> SplitEvent:
    return SplitEvent(segment.start, segment.end, segment.source_text(), segment.translation)


def is_whole_segment_split(segment: TranscriptSegment) -> bool:
    return (
        len(segment.split_events) == 1
        and segment.split_events[0].en == segment.source_text()
    )


def infer_split_status(segment: TranscriptSegment, split: SplitConfig) -> str:
    if segment.split_status:
        return segment.split_status
    if not segment.split_events:
        return ""
    is_long = len(segment.source_text()) > split.max_chars or segment.end - segment.start > split.max_duration
    if is_whole_segment_split(segment):
        return SplitStatus.FALLBACK.value if is_long else SplitStatus.UNSPLIT.value
    return SplitStatus.OK.value


def split_reason_message(reason: str, detail: str = "") -> str:
    messages = {
        SplitReason.BELOW_THRESHOLDS.value: "below split thresholds",
        SplitReason.NO_USABLE_PARTS.value: "no usable split parts for this id",
        SplitReason.PART_COUNT_MISMATCH.value: "source/target part count mismatch",
        SplitReason.TOKEN_RECONSTRUCT_FAILED.value: "source tokens do not reconstruct original",
        SplitReason.WORD_ALIGNMENT_FAILED.value: "split edge words could not align to WhisperX words",
        SplitReason.PARSE_FAILED.value: "AI split response parse failed",
        SplitReason.EXCEPTION.value: "split request failed",
        SplitReason.AI_SPLIT_INVALID.value: "invalid or unaligned AI split",
    }
    base = messages.get(reason, reason or SplitReason.AI_SPLIT_INVALID.value)
    return f"{base}: {detail}" if detail else base


def split_context_items(
    transcript: Transcript,
    segment: TranscriptSegment,
    ctx: TranscriptContext,
    window: int,
    before: bool,
) -> list[SplitContextItem]:
    if window <= 0:
        return []
    try:
        pos = transcript.segments.index(segment)
    except ValueError:
        return []
    if before:
        candidates = transcript.segments[max(0, pos - window) : pos]
    else:
        candidates = transcript.segments[pos + 1 : pos + 1 + window]
    return [
        SplitContextItem(seg.index, seg.source_text(), seg.translation, ctx)
        for seg in candidates
    ]


def validated_split_events(
    segment: TranscriptSegment,
    source_parts: Optional[list[str]],
    target_parts: Optional[list[str]],
) -> tuple[Optional[list[SplitEvent]], str, str]:
    if not source_parts or not target_parts:
        return None, SplitReason.NO_USABLE_PARTS.value, ""
    if len(source_parts) != len(target_parts):
        return (
            None,
            SplitReason.PART_COUNT_MISMATCH.value,
            f"source parts {len(source_parts)} != target parts {len(target_parts)}",
        )
    expected_tokens = text_tokens(segment.source_text())
    actual_tokens = text_tokens(" ".join(source_parts))
    if actual_tokens != expected_tokens:
        return (
            None,
            SplitReason.TOKEN_RECONSTRUCT_FAILED.value,
            f"{len(actual_tokens)} != {len(expected_tokens)}",
        )
    if len(source_parts) == 1:
        return [SplitEvent(segment.start, segment.end, source_parts[0], target_parts[0])], "", ""
    events = align_split_events(segment, source_parts, target_parts)
    if not events or len(events) != len(source_parts):
        return None, SplitReason.WORD_ALIGNMENT_FAILED.value, ""
    return events, "", ""


def split_segments(
    transcript: Transcript,
    ctx: TranscriptContext,
    llm: LLMConfig,
    split: SplitConfig,
    quiet: bool,
) -> bool:
    changed = False
    for seg in transcript.segments:
        if not seg.split_events:
            should_split = (
                split.enabled
                and (len(seg.source_text()) > split.max_chars or seg.end - seg.start > split.max_duration)
            )
            if not should_split:
                seg.split_events = [whole_segment_split_event(seg)]
                seg.split_status = SplitStatus.UNSPLIT.value
                seg.split_reason = SplitReason.BELOW_THRESHOLDS.value
                seg.split_reason_detail = ""
                changed = True

    if not split.enabled:
        return changed

    pending = [
        s
        for s in transcript.segments
        if infer_split_status(s, split) in ("", SplitStatus.FALLBACK.value)
    ]
    pending = [s for s in pending if len(s.source_text()) > split.max_chars or s.end - s.start > split.max_duration]
    if not pending:
        if not quiet:
            print("Split: cached/no long segments", file=sys.stderr)
        return changed

    if not quiet:
        print(f"Split: {len(pending)} long segment(s), context_window={split.context_window}", file=sys.stderr)
        print(
            "Split pending ids: " + ", ".join(str(seg.index) for seg in pending),
            file=sys.stderr,
        )
        for seg in pending:
            status = infer_split_status(seg, split) or SplitStatus.PENDING.value
            print(
                f"  #{seg.index}: chars={len(seg.source_text())} "
                f"duration={seg.end - seg.start:.2f}s split_status={status}",
                file=sys.stderr,
            )

    style_prompt = render_language_template(
        load_prompt("split_prompt", _SPLIT_PROMPT_FALLBACK),
        ctx.source_lang,
        ctx.target_lang,
        ctx.source_lang_code,
        ctx.target_lang_code,
    )
    split_format = render_prompt_template(_SPLIT_FORMAT, ctx)
    session = ChatSession(
        llm,
        f"{style_prompt}\n\n{_JSON_FORMAT}\n\n{_JSON_BATCH_FORMAT}\n\n{split_format}",
        temperature=0.1,
    )
    for start in range(0, len(pending), max(1, llm.batch_size // 2)):
        batch = pending[start : start + max(1, llm.batch_size // 2)]
        expected_ids = [seg.index for seg in batch]
        request = LLMBatchRequest(
            [
                SplitInputItem(
                    seg.index,
                    seg.source_text(),
                    seg.translation,
                    ctx,
                    context_before=split_context_items(transcript, seg, ctx, split.context_window, before=True),
                    context_after=split_context_items(transcript, seg, ctx, split.context_window, before=False),
                ).to_batch_item()
                for seg in batch
            ]
        )
        try:
            if not quiet:
                print("Split AI user prompt:", file=sys.stderr)
                print(request.to_json_text(), file=sys.stderr)
            response_items = llm_numbered_batch(
                request,
                session,
                quiet,
                max_tokens=STRUCTURED_MAX_TOKENS,
            )
            if not quiet:
                print("Split AI raw response:", file=sys.stderr)
                print(json.dumps(response_items, ensure_ascii=False, indent=2), file=sys.stderr)
            source_splits, target_splits, parse_error = parse_split_response(
                response_items,
                expected_ids,
                ctx,
            )
            if parse_error and not quiet:
                print(f"Split parse warning: {parse_error}", file=sys.stderr)
        except Exception as e:
            print(f"Warning: split failed: {e}", file=sys.stderr)
            source_splits, target_splits, parse_error = {}, {}, str(e)

        for seg in batch:
            events, reason, reason_detail = validated_split_events(
                seg,
                source_splits.get(seg.index),
                target_splits.get(seg.index),
            )
            if events is None:
                fallback_reason = reason or (SplitReason.PARSE_FAILED.value if parse_error else SplitReason.AI_SPLIT_INVALID.value)
                fallback_detail = reason_detail or parse_error
                if not quiet:
                    print(
                        f"Split: fallback to whole segment #{seg.index} "
                        f"({split_reason_message(fallback_reason, fallback_detail)})",
                        file=sys.stderr,
                    )
                    print(f"  Source text: {seg.source_text()}", file=sys.stderr)
                    print(f"  AI source parts: {source_splits.get(seg.index)}", file=sys.stderr)
                    print(f"  AI target parts: {target_splits.get(seg.index)}", file=sys.stderr)
                events = [whole_segment_split_event(seg)]
                seg.split_status = SplitStatus.FALLBACK.value
                seg.split_reason = fallback_reason
                seg.split_reason_detail = fallback_detail
            else:
                seg.split_status = (
                    SplitStatus.UNSPLIT.value
                    if len(events) == 1 and events[0].en == seg.source_text()
                    else SplitStatus.OK.value
                )
                seg.split_reason = ""
                seg.split_reason_detail = ""
            seg.split_events = events
            changed = True

    return changed


# --- Subtitle output ----------------------------------------------------------


def srt_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = min(999, int(round((seconds - int(seconds)) * 1000)))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt_events(output_path: str, events: list[SplitEvent], field_name: str) -> None:
    with open(output_path, "w", encoding="utf-8") as f:
        for i, event in enumerate(events, 1):
            text = getattr(event, field_name)
            f.write(f"{i}\n")
            f.write(f"{srt_time(event.start)} --> {srt_time(event.end)}\n")
            f.write(f"{ass_escape(text)}\n\n")


def ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    cs = int(round((seconds - int(seconds)) * 100))
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def load_template(template_path: str) -> tuple[str, str]:
    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()
    events_pos = content.find("\n[Events]\n")
    if events_pos == -1:
        print("Error: template.ass missing [Events] section.", file=sys.stderr)
        sys.exit(1)
    header = content[: events_pos + 1]
    events_section = content[events_pos + 1 :]
    match = re.search(r"Format:.*", events_section)
    if not match:
        print("Error: template.ass [Events] section missing Format line.", file=sys.stderr)
        sys.exit(1)
    return header, "\n[Events]\n" + match.group(0) + "\n"


def ass_escape(text: str) -> str:
    return " ".join(text.replace("\\N", " ").split())


def wrap_cjk(text: str, max_chars: int = 25) -> str:
    return ass_escape(text)


def all_events(transcript: Transcript) -> list[SplitEvent]:
    events: list[SplitEvent] = []
    for seg in transcript.segments:
        if seg.split_events:
            events.extend(seg.split_events)
        else:
            events.append(SplitEvent(seg.start, seg.end, seg.source_text(), seg.translation))
    return events


@dataclass(frozen=True)
class AssTrack:
    field_name: str
    style: str
    wrap_text: bool = False


ASS_OUTPUT_TRACKS: dict[AssOutputMode, tuple[AssTrack, ...]] = {
    AssOutputMode.SOURCE: (AssTrack("en", "bi-en"),),
    AssOutputMode.TARGET: (AssTrack("zh", "bi-zh", wrap_text=True),),
    AssOutputMode.BILINGUAL: (
        AssTrack("en", "bi-en"),
        AssTrack("zh", "bi-zh", wrap_text=True),
    ),
}


def write_ass(
    output_path: str,
    template_path: str,
    title: str,
    events: list[SplitEvent],
    mode: AssOutputMode | str,
) -> None:
    output_mode = AssOutputMode.normalize(mode)
    header, events_header = load_template(template_path)
    header = re.sub(r"Title:\s*.*", f"Title: {title}", header)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write(events_header)
        for track in ASS_OUTPUT_TRACKS[output_mode]:
            for event in events:
                text = ass_escape(str(getattr(event, track.field_name)))
                if track.wrap_text:
                    text = wrap_cjk(text)
                f.write(
                    f"Dialogue: 0,{ass_time(event.start)},{ass_time(event.end)},"
                    f"{track.style},,0,0,0,,{text}\n"
                )


DESCRIPTION_TRANSLATE_PROMPT = """You are a professional translator. Translate the following video title, description, and tags from ${SOURCE_LANG} to ${TARGET_LANG}.

Return a JSON object with exactly these keys:
{"title": "<translated title>", "description": "<translated description>", "tags": ["<translated tag>", "..."]}

Rules:
- Preserve URLs, email addresses, handles, and paragraph structure.
- Translate each tag naturally and keep the tag list order.
- Do not add explanations."""


def translate_description(ctx: TranscriptContext, llm: LLMConfig, quiet: bool) -> str:
    title, webpage_url, tags = read_metadata(ctx)
    metadata_header = read_metadata_header(ctx)
    desc_text = _read_text_file(ctx.desc) if os.path.isfile(ctx.desc) else ""
    if not desc_text.strip() and not title:
        if metadata_header:
            with open(ctx.target_desc, "w", encoding="utf-8") as f:
                f.write(metadata_header)
        return ctx.target_desc
    request = LLMObjectRequest(
        {
            "title": title,
            "url": webpage_url,
            "description": desc_text,
            "tags": tags,
        }
    )
    try:
        response_obj = llm_json_once(
            llm,
            render_prompt_template(DESCRIPTION_TRANSLATE_PROMPT, ctx) + "\n\n" + _JSON_FORMAT + "\n\n" + _JSON_OBJECT_FORMAT,
            request,
            max_tokens=max(2048, len(desc_text) * 2),
            temperature=0.3,
        )
        translated_title = str(response_obj.get("title", "")).strip()
        translated_desc = str(response_obj.get("description", "")).strip()
        translated_tags_raw = response_obj.get("tags", [])
        if isinstance(translated_tags_raw, list):
            translated_tags = [str(tag).strip() for tag in translated_tags_raw if str(tag).strip()]
        elif isinstance(translated_tags_raw, str):
            translated_tags = [tag.strip() for tag in re.split(r"[,，\n]+", translated_tags_raw) if tag.strip()]
        else:
            translated_tags = []
    except Exception as e:
        print(f"Warning: description translation failed: {e}", file=sys.stderr)
        return ctx.target_desc

    with open(ctx.target_desc, "w", encoding="utf-8") as f:
        if translated_title:
            f.write(f"{translated_title}\n\n")
        f.write(metadata_header)
        if translated_desc:
            f.write(translated_desc)
            f.write("\n")
        if translated_tags:
            if translated_desc:
                f.write("\n")
            f.write(f"标签：{', '.join(translated_tags)}\n")
    if not quiet:
        print(f"Description: {ctx.target_desc}", file=sys.stderr)
    return ctx.target_desc


# --- CLI ----------------------------------------------------------------------


def infer_video_path(ctx: TranscriptContext) -> str:
    for ext in (".webm", ".mkv", ".mp4", ".mov", ".m4v"):
        candidate = os.path.join(ctx.dir, ctx.base + ext)
        if os.path.isfile(candidate):
            return candidate
    return ""


def main() -> None:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env = load_env(script_dir)

    parser = argparse.ArgumentParser(
        description="Translate WhisperX JSON to proofread/target-language/bilingual ASS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  .\\.venv\\Scripts\\python.exe translate_srt.py video.json --video video.webm
  .\\.venv\\Scripts\\python.exe translate_srt.py video.json --source-lang en --target-lang zh
  .\\.venv\\Scripts\\python.exe translate_srt.py video.json -o video.en-zh.ass
  .\\.venv\\Scripts\\python.exe translate_srt.py video.json --only-beautify --video video.webm
""",
    )
    parser.add_argument("json", help="WhisperX .json transcript path")
    parser.add_argument("--video", help="Video path for scene-aware timeline beautify")
    parser.add_argument("-t", "--template", help="template.ass path")
    parser.add_argument("-o", "--output", help="Output bilingual .ass path")
    parser.add_argument("--source-lang", help="Source language name/tag for prompts and ISO 639 output suffix")
    parser.add_argument("--target-lang", help="Target language name/tag for prompts and ISO 639 output suffix (default: zh)")
    parser.add_argument("--print-output-path", action="store_true", help="Print computed bilingual ASS path and exit")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--only-beautify", action="store_true")
    parser.add_argument("--only-glossary", action="store_true")
    parser.add_argument("--skip-beautify", action="store_true")
    parser.add_argument("--skip-knowledge", action="store_true")
    parser.add_argument("--force", action="store_true", help="Rebuild beautified JSON")
    parser.add_argument("--no-split", action="store_true")
    parser.add_argument("--split-max-chars", type=int, default=60)
    parser.add_argument("--split-max-duration", type=float, default=3.0)
    parser.add_argument("--split-context-window", type=int, default=1, help="Neighbor segment count included as split-only context")
    parser.add_argument("--proofread", action="store_true", default=True)
    parser.add_argument("--no-proofread", action="store_true")
    parser.add_argument("--glossary", metavar="PATH")
    parser.add_argument("--scene-threshold", type=float, default=0.15)
    parser.add_argument("--snap-frames", type=int, default=7)
    parser.add_argument("--end-offset-frames", type=int, default=2)
    parser.add_argument("--min-scene-interval-frames", type=int, default=2)
    parser.add_argument("--min-duration", type=float, default=1.0)
    parser.add_argument("--max-duration", type=float, default=8.0)
    parser.add_argument("--min-gap", type=float, default=0.083)
    parser.add_argument("--max-gap-merge", type=float, default=0.5)
    parser.add_argument("--aggressive", action="store_true")
    parser.add_argument("--no-scene-snap", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args()

    if not os.path.isfile(args.json):
        print(f"Error: JSON file not found: {args.json}", file=sys.stderr)
        sys.exit(1)

    source = load_transcript(args.json)
    source_lang = args.source_lang or env.get("SOURCE_LANG", "") or source.language or "source"
    target_lang = args.target_lang or env.get("TARGET_LANG", "") or "zh"
    ctx = TranscriptContext.from_json(args.json, args.output or "", source_lang, target_lang)
    embedding_config = EmbeddingConfig.from_env(env, ctx)
    if args.print_output_path:
        print(os.path.abspath(ctx.bilingual_ass))
        print(f"OUTPUT_ASS={os.path.abspath(ctx.bilingual_ass)}")
        return
    video_path = args.video or infer_video_path(ctx)
    template_path = args.template or os.path.join(script_dir, "template.ass")

    if not args.quiet:
        print(f"JSON:     {os.path.abspath(args.json)}")
        print(f"Source:   {ctx.source_lang}")
        print(f"Target:   {ctx.target_lang}")
        if video_path:
            print(f"Video:    {video_path}")
        if embedding_config.enabled:
            print(f"Embedding: {embedding_config.provider} / {embedding_config.model}")
            print(f"Embedding DB: {embedding_config.chroma_dir}")

    if embedding_config.enabled:
        if embedding_config.store != "chroma":
            print(f"Error: unsupported EMBEDDING_STORE={embedding_config.store}; only chroma is available.", file=sys.stderr)
            sys.exit(1)

    beautify_options = BeautifyOptions(
        scene_threshold=args.scene_threshold,
        snap_frames=args.snap_frames,
        end_offset_frames=args.end_offset_frames,
        min_scene_interval_frames=args.min_scene_interval_frames,
        min_duration=args.min_duration,
        max_duration=args.max_duration,
        min_gap=args.min_gap,
        max_gap_merge=args.max_gap_merge,
        no_scene_snap=args.no_scene_snap,
        aggressive=args.aggressive,
    )
    transcript = load_or_create_beautified(
        ctx, source, video_path, beautify_options, args.skip_beautify, args.force, args.quiet
    )
    retriever = None
    if embedding_config.enabled:
        try:
            build_embedding_index(transcript, embedding_config, env, args.quiet, ctx)
            retriever = EmbeddingRetriever(embedding_config, env)
        except Exception as e:
            print(f"Error: embedding index failed: {e}", file=sys.stderr)
            sys.exit(1)

    if args.only_beautify:
        print(f"OUTPUT_JSON={os.path.abspath(ctx.beautified_json)}")
        return

    provider = env.get("TRANSLATE_PROVIDER", "")
    if not provider:
        print(
            f"Error: TRANSLATE_PROVIDER not set in .env. Available: {', '.join(load_providers())}",
            file=sys.stderr,
        )
        sys.exit(1)
    llm = LLMConfig(
        provider=provider,
        model=env.get("TRANSLATE_MODEL", ""),
        proofread_provider=env.get("PROOFREAD_PROVIDER", ""),
        proofread_model=env.get("PROOFREAD_MODEL", ""),
        batch_size=args.batch_size,
        proofread_batch_size=env_int(env.get("PROOFREAD_BATCH_SIZE", ""), max(1, args.batch_size // 2)),
        proofread_retrieval_top_k=env_int(env.get("PROOFREAD_RETRIEVAL_TOP_K", ""), 1),
        proofread_max_tokens=env_int(env.get("PROOFREAD_MAX_TOKENS", ""), 8192),
    )

    if not args.skip_knowledge:
        build_glossary(
            transcript,
            ctx,
            llm,
            env.get("TAVILY_API_KEY", ""),
            int(env.get("TAVILY_MAX_RESULTS", "10") or "10"),
            args.quiet,
            retriever,
        )

    if args.only_glossary:
        print(f"OUTPUT_GLOSSARY={os.path.abspath(ctx.glossary)}")
        return

    system_prompt = render_prompt_template(
        load_prompt("translate_prompt", _TRANSLATE_PROMPT_FALLBACK),
        ctx,
    )
    proofread_prompt = render_prompt_template(
        load_prompt("proofread_prompt", _PROOFREAD_PROMPT_FALLBACK),
        ctx,
    )
    desc_context = load_description(ctx.desc)
    glossary_path = args.glossary or ctx.glossary
    glossary_text = load_glossary(glossary_path)
    if desc_context:
        system_prompt += desc_context
        proofread_prompt += desc_context
    if glossary_text:
        system_prompt += glossary_text
        proofread_prompt += glossary_text

    changed = translate_segments(transcript, ctx, llm, system_prompt, args.quiet, retriever)
    changed = split_segments(
        transcript,
        ctx,
        llm,
        SplitConfig(
            enabled=not args.no_split,
            max_chars=args.split_max_chars,
            max_duration=args.split_max_duration,
            context_window=max(0, args.split_context_window),
        ),
        args.quiet,
    ) or changed
    if embedding_config.enabled:
        try:
            build_embedding_index(transcript, embedding_config, env, args.quiet, ctx)
            retriever = EmbeddingRetriever(embedding_config, env)
        except Exception as e:
            print(f"Warning: translation memory index update failed: {e}", file=sys.stderr)
    if (args.proofread and not args.no_proofread and env.get("PROOFREAD", "1") != "0"):
        changed = proofread_split_events(
            transcript,
            ctx,
            llm,
            proofread_prompt,
            args.quiet,
            retriever,
        ) or changed
    if changed:
        save_transcript(transcript, ctx.beautified_json)

    if not os.path.isfile(template_path):
        print(f"Error: template.ass not found: {template_path}", file=sys.stderr)
        sys.exit(1)

    events = all_events(transcript)
    write_srt_events(ctx.split_source_srt, events, "en")
    write_srt_events(ctx.split_target_srt, events, "zh")
    write_ass(ctx.proofread_ass, template_path, ctx.base, events, AssOutputMode.SOURCE)
    write_ass(ctx.target_ass, template_path, ctx.base, events, AssOutputMode.TARGET)
    write_ass(ctx.bilingual_ass, template_path, ctx.base, events, AssOutputMode.BILINGUAL)

    if os.path.isfile(ctx.desc):
        translate_description(ctx, llm, args.quiet)

    if not args.quiet:
        print(f"SRT:      {ctx.split_source_srt}")
        print(f"          {ctx.split_target_srt}")
        print(f"ASS:      {ctx.proofread_ass}")
        print(f"          {ctx.target_ass}")
        print(f"          {ctx.bilingual_ass}")
        print(f"Events:   {len(events)}")
    else:
        print(ctx.bilingual_ass)
    print(f"OUTPUT_ASS={os.path.abspath(ctx.bilingual_ass)}")


if __name__ == "__main__":
    main()
