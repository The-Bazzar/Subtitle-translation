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
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from urllib.parse import urlparse

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
    api_key: Optional[str] = None
    batch_size: int = 50

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


def translate_llm_from_env(env: dict[str, str], batch_size: int) -> LLMConfig:
    return LLMConfig(
        provider=env.get("TRANSLATE_PROVIDER", "").strip(),
        model=env.get("TRANSLATE_MODEL", "").strip(),
        batch_size=batch_size,
    )


def proofread_llm_from_env(env: dict[str, str], translate_llm: LLMConfig, batch_size: int) -> LLMConfig:
    configured_provider = env.get("PROOFREAD_PROVIDER", "").strip()
    provider = configured_provider or translate_llm.provider
    configured_model = env.get("PROOFREAD_MODEL", "").strip()
    if configured_model:
        model = configured_model
    elif configured_provider:
        model = ""
    else:
        model = translate_llm.model
    return LLMConfig(
        provider=provider,
        model=model,
        api_key=translate_llm.api_key if provider == translate_llm.provider else None,
        batch_size=env_int(env.get("PROOFREAD_BATCH_SIZE", ""), max(1, batch_size // 2)),
    )


def glossary_llm_from_env(
    env: dict[str, str],
    translate_llm: Optional[LLMConfig] = None,
    batch_size: int = 50,
) -> LLMConfig:
    configured_provider = env.get("GLOSSARY_PROVIDER", "").strip()
    provider = configured_provider or (translate_llm.provider if translate_llm else env.get("TRANSLATE_PROVIDER", "").strip())
    configured_model = env.get("GLOSSARY_MODEL", "").strip()
    if configured_model:
        model = configured_model
    elif configured_provider:
        model = ""
    else:
        model = translate_llm.model if translate_llm else env.get("TRANSLATE_MODEL", "").strip()
    return LLMConfig(
        provider=provider,
        model=model,
        api_key=translate_llm.api_key if translate_llm and provider == translate_llm.provider else None,
        batch_size=translate_llm.batch_size if translate_llm else batch_size,
    )


def required_glossary_provider(env: dict[str, str]) -> str:
    return env.get("GLOSSARY_PROVIDER", "").strip() or env.get("TRANSLATE_PROVIDER", "").strip()


def needs_translate_llm(args) -> bool:
    return not bool(getattr(args, "only_glossary", False))


def proofread_retrieval_top_k_from_env(env: dict[str, str]) -> int:
    return env_int(env.get("PROOFREAD_RETRIEVAL_TOP_K", ""), 1)


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
    context_text: Optional[str] = None

    def to_document(self) -> Document:
        metadata = {
            "id": self.chunk_id,
            "source": self.source,
        }
        if self.start is not None:
            metadata["start"] = self.start
        if self.end is not None:
            metadata["end"] = self.end
        if self.context_text and self.context_text != self.text:
            metadata["context_text"] = self.context_text
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
        context_text = metadata.pop("context_text", doc.page_content)
        data = {
            "id": str(metadata.pop("id", "")),
            "source": str(metadata.pop("source", "")),
            "text": str(context_text),
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


TRANSCRIPT_CHUNK_MAX_SECONDS = 60.0
TRANSCRIPT_CHUNK_MAX_SEGMENTS = 24
TRANSCRIPT_CHUNK_OVERLAP_SECONDS = 10.0
TRANSCRIPT_CHUNK_OVERLAP_MAX_SEGMENTS = 4
TRANSCRIPT_CHUNK_OVERLAP_MAX_RATIO = 0.25


def transcript_chunk_id(segments: list[TranscriptSegment]) -> str:
    first = segments[0]
    last = segments[-1]
    if first.index == last.index:
        return f"transcript:{first.index}"
    return f"transcript:{first.index}-{last.index}"


def transcript_chunk_line(seg: TranscriptSegment) -> str:
    return f"[{seg.index}] {seg.source_text().strip()}"


def embedding_timestamp(seconds: float) -> str:
    total_ms = max(0, int(round(float(seconds) * 1000)))
    ms = total_ms % 1000
    total_seconds = total_ms // 1000
    s = total_seconds % 60
    total_minutes = total_seconds // 60
    m = total_minutes % 60
    h = total_minutes // 60
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


def transcript_chunk_context_line(seg: TranscriptSegment) -> str:
    return (
        f"[{seg.index} {embedding_timestamp(seg.start)}-{embedding_timestamp(seg.end)}] "
        f"{seg.source_text().strip()}"
    )


def transcript_chunk_len(lines: list[str]) -> int:
    return sum(len(line) for line in lines) + max(0, len(lines) - 1)


def transcript_chunk_duration(segments: list[TranscriptSegment]) -> float:
    if not segments:
        return 0.0
    return max(0.0, float(segments[-1].end) - float(segments[0].start))


def transcript_overlap_segments(segments: list[TranscriptSegment], chunk_len: int) -> list[TranscriptSegment]:
    if not segments:
        return []
    window_start = float(segments[-1].end) - TRANSCRIPT_CHUNK_OVERLAP_SECONDS
    candidates = [
        seg
        for seg in segments
        if float(seg.end) > window_start
    ][-TRANSCRIPT_CHUNK_OVERLAP_MAX_SEGMENTS:]
    if not candidates:
        candidates = [segments[-1]]

    max_overlap_len = max(
        len(transcript_chunk_line(candidates[-1])),
        int(chunk_len * TRANSCRIPT_CHUNK_OVERLAP_MAX_RATIO),
    )
    selected: list[TranscriptSegment] = []
    selected_lines: list[str] = []
    for seg in reversed(candidates):
        line = transcript_chunk_line(seg)
        projected = transcript_chunk_len([line, *selected_lines])
        if selected and projected > max_overlap_len:
            break
        selected.insert(0, seg)
        selected_lines.insert(0, line)
    return selected or [segments[-1]]


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
        chunks.append(
            EmbeddingChunk(
                chunk_id=transcript_chunk_id(current_segments),
                source="transcript",
                text="\n".join(current_lines),
                start=first.start,
                end=last.end,
                metadata={
                    "language": transcript.language,
                    "segment_ids": [seg.index for seg in current_segments],
                },
                context_text="\n".join(transcript_chunk_context_line(seg) for seg in current_segments),
            )
        )
        overlap_segments = transcript_overlap_segments(current_segments, current_len)
        current_segments = list(overlap_segments)
        current_lines = [transcript_chunk_line(seg) for seg in current_segments]
        current_len = transcript_chunk_len(current_lines)

    for seg in transcript.segments:
        text = seg.source_text().strip()
        if not text:
            continue
        line = transcript_chunk_line(seg)
        extra_len = len(line) + (1 if current_lines else 0)
        next_segments = current_segments + [seg]
        if current_lines and (
            current_len + extra_len > max_chars
            or len(next_segments) > TRANSCRIPT_CHUNK_MAX_SEGMENTS
            or transcript_chunk_duration(next_segments) > TRANSCRIPT_CHUNK_MAX_SECONDS
        ):
            flush()
            if current_segments and current_segments[-1].index == seg.index:
                continue
            extra_len = len(line) + (1 if current_lines else 0)
        current_segments.append(seg)
        current_lines.append(line)
        current_len += extra_len

    flush()
    return chunks


def split_markdown_sections(text: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    current_heading = ""
    current_lines: list[str] = []
    heading_pattern = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

    def flush() -> None:
        nonlocal current_lines
        if current_lines:
            sections.append((current_heading, current_lines))
            current_lines = []

    for line in text.splitlines():
        match = heading_pattern.match(line)
        if match:
            flush()
            current_heading = match.group(2).strip()
        current_lines.append(line)
    flush()
    return sections


def build_glossary_chunks(ctx: TranscriptContext, chunk_chars: int) -> list[EmbeddingChunk]:
    if not os.path.isfile(ctx.glossary) or os.path.getsize(ctx.glossary) <= 0:
        return []
    text = _read_text_file(ctx.glossary).strip()
    if not text:
        return []

    chunks: list[EmbeddingChunk] = []
    max_chars = max(1, chunk_chars)

    def append_chunk(current_lines: list[str], heading: str) -> None:
        if not current_lines:
            return
        index = len(chunks) + 1
        chunks.append(
            EmbeddingChunk(
                chunk_id=f"glossary:{index}",
                source="glossary",
                text="\n".join(current_lines).strip(),
                metadata={
                    "kind": "project_glossary",
                    "path": ctx.glossary,
                    "heading": heading,
                },
            )
        )

    for heading, section_lines in split_markdown_sections(text):
        current_lines: list[str] = []
        current_len = 0
        for line in section_lines:
            extra_len = len(line) + (1 if current_lines else 0)
            if current_lines and current_len + extra_len > max_chars:
                append_chunk(current_lines, heading)
                current_lines = []
                current_len = 0
                extra_len = len(line)
            current_lines.append(line)
            current_len += extra_len
        append_chunk(current_lines, heading)

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


def is_embedding_chunk_id(chunk_id: str) -> bool:
    return chunk_id.startswith(("transcript:", "glossary:", "translation_memory:"))


def existing_embedding_chunk_ids(store) -> list[str]:
    if not hasattr(store, "get"):
        return []
    try:
        data = store.get(include=[])
    except TypeError:
        data = store.get()
    except Exception:
        return []
    ids = data.get("ids", []) if isinstance(data, dict) else []
    return [str(chunk_id) for chunk_id in ids if is_embedding_chunk_id(str(chunk_id))]


def clear_embedding_chunks(store, chunk_ids: Optional[list[str]] = None) -> None:
    ids = chunk_ids if chunk_ids is not None else existing_embedding_chunk_ids(store)
    ids = [str(chunk_id) for chunk_id in ids if is_embedding_chunk_id(str(chunk_id))]
    if not ids or not hasattr(store, "delete"):
        return
    store.delete(ids=ids)


def build_embedding_index(
    transcript: Transcript,
    config: EmbeddingConfig,
    env: dict[str, str],
    quiet: bool = False,
    ctx: Optional[TranscriptContext] = None,
    existing_chunk_ids: Optional[list[str]] = None,
) -> str:
    if not config.enabled:
        return ""
    chunks = build_embedding_chunks(transcript, config.chunk_chars)
    if ctx is not None:
        chunks.extend(build_glossary_chunks(ctx, config.chunk_chars))
        chunks.extend(build_translation_memory_chunks(transcript, ctx))
    store = open_chroma_store(config, env)
    clear_embedding_chunks(store, existing_chunk_ids)
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


def refresh_embedding_retriever(
    transcript: Transcript,
    config: EmbeddingConfig,
    env: dict[str, str],
    quiet: bool,
    ctx: TranscriptContext,
    fatal: bool = False,
    warning_label: str = "embedding index failed",
) -> EmbeddingRetriever | None:
    try:
        build_embedding_index(transcript, config, env, quiet, ctx)
        return EmbeddingRetriever(config, env)
    except Exception as e:
        if fatal:
            print(f"Error: {warning_label}: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Warning: {warning_label}: {e}", file=sys.stderr)
        return None


def env_flag(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def env_int(value: str, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def embedding_enabled_for_stage(only_beautify: bool, only_glossary: bool) -> bool:
    return not only_beautify


DEFAULT_SPLIT_MAX_CHARS = 72
DEFAULT_SPLIT_MAX_DURATION = 3.8


@dataclass
class BeautifyOptions:
    scene_threshold: float = 0.15
    snap_frames: int = 7
    end_offset_frames: int = 2
    min_scene_interval_frames: int = 2
    min_duration: float = 1.0
    min_gap: float = 0.083
    max_gap_merge: float = 0.5
    no_scene_snap: bool = False
    aggressive: bool = False
    fps: float = 24.0


@dataclass
class SplitConfig:
    enabled: bool = True
    max_chars: int = DEFAULT_SPLIT_MAX_CHARS
    max_duration: float = DEFAULT_SPLIT_MAX_DURATION
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


@dataclass(frozen=True)
class LanguageFields:
    source_key: str
    target_key: str

    @staticmethod
    def from_ctx(ctx: TranscriptContext) -> "LanguageFields":
        return LanguageFields(ctx.source_lang_code, ctx.target_lang_code)

    def source_candidates(self) -> set[str]:
        return {normalized_response_key(self.source_key)} if self.source_key else set()

    def target_candidates(self) -> set[str]:
        return {normalized_response_key(self.target_key)} if self.target_key else set()

    def build(self, source=None, target=None, extra: Optional[dict] = None) -> dict:
        fields: dict = {}
        if source is not None:
            fields[self.source_key] = source
        if target is not None:
            fields[self.target_key] = target
        if extra:
            fields.update(extra)
        return prune_empty_json(fields) or {}

    def get_source(self, item: dict):
        return get_language_keyed_value(item, self.source_candidates())

    def get_target(self, item: dict):
        return get_language_keyed_value(item, self.target_candidates())


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
        "request_kwargs": {
            "response_format": {"type": "json_object"},
        },
    },
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-3.5-flash",
        "env_key": "GEMINI_API_KEY",
        "auth_header": "Bearer {api_key}",
        "extra_headers": {},
        "request_kwargs": {
            "extra_body": {
                "extra_body": {
                    "google": {
                        "tools": [
                            {
                                "google_search": {}
                            }
                        ]
                    }
                }
            }
        },
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
Some input items may include a "retrieved_context" array from the same project memory.
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

_GLOSSARY_PROMPT_FALLBACK = """You are a terminology expert. Build a rigorous glossary for ${TARGET_LANG} subtitle translation from the ${SOURCE_LANG} transcript, metadata, and any provided search evidence.

Glossary core:
- Background: identify the real topic, domain, works, people, and context in ${TARGET_LANG}.
- Core terminology: source term, corrected form if ASR is likely wrong, recommended ${TARGET_LANG} translation, and concise rationale.
- Tone: practical guidance for preserving speaker attitude and register in ${TARGET_LANG}.
- Key arguments: only the claims needed to keep translation choices consistent.

Evidence rules:
- Treat web search results as the primary evidence when they are provided; use the transcript to identify what matters, then verify names, titles, concepts, and standard ${TARGET_LANG} translations against search evidence.
- If transcript text conflicts with reliable search evidence, prefer the search evidence and mark uncertainty only when the correction is not clear.
- You must actively correct likely ASR errors in names, titles, quotes, source terms, and concepts. Do not copy ASR mistakes into the glossary.
- Include only terms, concepts, tone notes, and arguments that are actually useful for translating this video.
- If a term or correction remains uncertain after checking evidence, mark it with (?)."""

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


_TAVILY_QUERY_PROMPT = """You are a search-intent agent for a subtitle translation pipeline.
Read the video metadata and transcript excerpt, then produce compact keyword queries that reveal the real topic, named entities, concepts, works, claims, and terminology discussed in the video.

Rules:
- Base queries on transcript content first. Metadata can disambiguate, but do not rely on title/tags alone.
- The transcript excerpt comes from WhisperX ASR and may contain misheard names, works, quotes, proper nouns, or technical terms.
- Before writing a query, correct likely ASR errors by using metadata, neighboring context, and domain knowledge.
- Do not preserve a suspicious ASR token in a search query when a more likely canonical name, title, quote, or term can be inferred.
- If a correction is uncertain, prefer a broader canonical concept query over the dubious ASR wording; include only one uncertain correction at most.
- Compress long spoken ideas into search keywords. Do not copy full transcript sentences, subtitle lines, filler speech, or rhetorical questions as queries.
- Prefer concrete concepts, named entities, works, quotes, technical terms, and distinctive claims.
- Each query should normally contain 2 to 6 important words or named entities, not a complete sentence.
- Each query should cover one distinct search angle: person/work, technical term, historical background, core claim, quote/source, or domain-specific concept.
- Do not create near-duplicates, paraphrases of the same query, or multiple queries that only change function words.
- Also extract compact topic hints for selecting authoritative domain groups, such as anime, philosophy, game, film, AI, history, medicine, or their source/target-language equivalents.
- Avoid generic channel promotion, merch, sponsorship, social links, and vague queries.
- Each query must be useful as a direct Tavily/web search query.
- Topic hints are not search queries; they are short domain/category keywords.
- Return 3 to 8 queries when possible.
- Keep each query concise, normally under 80 characters."""

_TAVILY_QUERY_FORMAT = """TAVILY SEARCH QUERY JSON PROTOCOL:
Return exactly two top-level keys: "queries" and "topic_hints".
"queries" must be a JSON array of non-empty strings.
"topic_hints" must be a JSON array of short topic/category keywords useful for selecting domain groups.

GOOD:
{"queries": ["named entity technical term", "work title concept", "distinctive claim keywords"], "topic_hints": ["anime", "film criticism"]}

BAD:
{"queries": ["full spoken sentence copied from transcript with filler words and no keyword compression", "same concept with slightly different wording", "suspicious ASR gibberish kept as keyword"], "topic_hints": ["topic"]}

Do not include explanations, scores, markdown, or extra keys."""


_TAVILY_QUERY_TRANSLATE_PROMPT = """You localize web search intent for a subtitle translation pipeline.
Convert each search query from ${SOURCE_LANG_CODE} into a natural ${TARGET_LANG_CODE} web/encyclopedia search query.
Do not translate as subtitle prose. Localize the search intent for how people search the target-language web.

Rules:
- Translate aggressively. The translated query should normally look like a natural ${TARGET_LANG_CODE} web/encyclopedia search, not a lightly edited copy of the source query.
- Translate concepts, claims, descriptive phrases, and genre/topic terms into natural ${TARGET_LANG_CODE} search wording; also translate explanatory wording when it helps search.
- Prefer target-language encyclopedia, wiki, fandom, database, and glossary terminology over literal wording.
- Do not return a query that is merely the source query with minor punctuation, spacing, casing, or word-order changes.
- Preserve only named entities, titles, works, brands, and proper nouns whose original form is normally the best ${TARGET_LANG_CODE} search term.
- When a preserved name would make the query too similar to the source, add target-language context around it.
- When both forms are useful, include the common ${TARGET_LANG_CODE} wording plus the original name if concise.
- If a query mixes a proper noun with a generic concept, translate the generic concept even when preserving the proper noun.
- If the input includes topic_hints, return topic_hints localized into compact ${TARGET_LANG_CODE} topic/category keywords for domain selection.
- Topic hints should be broad enough to match site groups, such as anime, animation, game, film, philosophy, AI, history, medicine, or their target-language equivalents.
- Keep each translated query concise.
- Return the same number of queries in the same order."""

_TAVILY_QUERY_TRANSLATE_FORMAT = """TAVILY QUERY TRANSLATION JSON PROTOCOL:
Return exactly one JSON object.
"queries" is required and must be a JSON array of translated non-empty strings.
"topic_hints" is required when the input contains topic_hints; otherwise omit it.
"topic_hints" must be a JSON array of localized short topic/category keywords, not full search queries.

GOOD:
{"queries": ["translated search query", "translated named entity concept"], "topic_hints": ["localized topic", "localized domain category"]}

Do not include explanations, scores, markdown, source-language notes, or extra keys."""


def tavily_query_system_prompt(ctx: TranscriptContext) -> str:
    return (
        render_prompt_template(_TAVILY_QUERY_PROMPT, ctx)
        + "\n\n"
        + _JSON_FORMAT
        + "\n\n"
        + _JSON_OBJECT_FORMAT
        + "\n\n"
        + _TAVILY_QUERY_FORMAT
    )


def tavily_query_translate_system_prompt(ctx: TranscriptContext) -> str:
    return (
        render_prompt_template(_TAVILY_QUERY_TRANSLATE_PROMPT, ctx)
        + "\n\n"
        + _JSON_FORMAT
        + "\n\n"
        + _JSON_OBJECT_FORMAT
        + "\n\n"
        + _TAVILY_QUERY_TRANSLATE_FORMAT
    )


def glossary_system_prompt(ctx: TranscriptContext, retriever: EmbeddingRetriever | None) -> str:
    return (
        render_prompt_template(load_prompt("glossary_prompt", _GLOSSARY_PROMPT_FALLBACK), ctx)
        + ("\n\n" + _RETRIEVED_CONTEXT_RULES if retriever is not None else "")
        + "\n\n"
        + _JSON_FORMAT
        + "\n\n"
        + _JSON_OBJECT_FORMAT
        + "\n\n"
        + render_prompt_template(_GLOSSARY_FORMAT, ctx)
    )


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


def load_glossary_prompt_context(glossary_path: str, retriever: EmbeddingRetriever | None) -> str:
    if retriever is not None:
        return ""
    return load_glossary(glossary_path)


def read_video_metadata_fields(ctx: TranscriptContext) -> dict:
    title = ctx.base
    webpage_url = ""
    uploader = ""
    upload_time = ""
    tags: list[str] = []
    if os.path.isfile(ctx.info_json):
        try:
            with open(ctx.info_json, "r", encoding="utf-8") as f:
                info = json.load(f)
            title = info.get("title") or title
            webpage_url = info.get("webpage_url") or ""
            uploader = str(info.get("uploader") or info.get("channel") or "")
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
    desc_text = _read_text_file(ctx.desc).strip() if os.path.isfile(ctx.desc) else ""
    return {
        "title": str(title),
        "webpage_url": str(webpage_url),
        "uploader": uploader,
        "upload_time": upload_time,
        "description": desc_text,
        "tags": tags,
    }


def read_metadata(ctx: TranscriptContext) -> tuple[str, str, list[str]]:
    fields = read_video_metadata_fields(ctx)
    return fields["title"], fields["webpage_url"], fields["tags"]


def read_metadata_header(ctx: TranscriptContext) -> str:
    fields = read_video_metadata_fields(ctx)
    if not any(fields.get(key) for key in ("title", "webpage_url", "uploader", "upload_time")):
        return ""

    return (
        f"原视频：{fields['webpage_url']}\n"
        f"原标题：{fields['title']}\n"
        f"原作者：{fields['uploader']}\n"
        f"上传时间：{fields['upload_time']}\n"
        f"\n=====\n\n"
    )


DESCRIPTION_NOISE_PATTERNS = [
    r"https?://",
    r"\bwww\.",
    r"\b(?:patreon|merch|shop|store|discount|sponsor|sponsored|affiliate)\b",
    r"\b(?:subscribe|follow|newsletter|instagram|twitter|x\.com|tiktok|discord|facebook|threads)\b",
    r"\b(?:use\s+code|promo\s+code|coupon)\b",
    r"\b(?:chapters?|timestamps?)\b",
    r"(?:©|\bcopyright\b)",
]


def filter_video_description_for_glossary(description: str, max_chars: int = 1600) -> tuple[str, bool]:
    kept_lines: list[str] = []
    filtered = False
    blank_pending = False
    for raw_line in description.splitlines():
        line = raw_line.strip()
        if not line:
            blank_pending = bool(kept_lines)
            continue
        lowered = line.lower()
        if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in DESCRIPTION_NOISE_PATTERNS):
            filtered = True
            continue
        if re.fullmatch(r"[\W_]+", line):
            filtered = True
            continue
        if blank_pending and kept_lines:
            kept_lines.append("")
        kept_lines.append(line)
        blank_pending = False

    text = "\n".join(kept_lines).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
        filtered = True
    return text, filtered


def build_local_glossary_metadata_section(ctx: TranscriptContext) -> str:
    fields = read_video_metadata_fields(ctx)
    lines = ["## 视频元信息", ""]
    if fields["webpage_url"]:
        lines.append(f"原视频：{fields['webpage_url']}")
    if fields["title"]:
        lines.append(f"原标题：{fields['title']}")
    if fields["uploader"]:
        lines.append(f"原作者：{fields['uploader']}")
    if fields["upload_time"]:
        lines.append(f"上传时间：{fields['upload_time']}")
    if fields["tags"]:
        lines.append(f"标签：{', '.join(fields['tags'])}")
    if fields["description"]:
        description, filtered = filter_video_description_for_glossary(fields["description"])
        if description:
            lines.extend(["", "原简介：", "", description])
        if filtered:
            lines.extend(["", "已过滤简介中的推广链接、社媒链接、赞助信息和纯 URL 行。"])
    section = "\n".join(lines).strip()
    return section if section != "## 视频元信息" else ""


def ensure_local_metadata_in_glossary(glossary: str, ctx: TranscriptContext) -> str:
    clean_glossary = glossary.strip()
    if "## 视频元信息" in clean_glossary:
        return clean_glossary
    metadata_section = build_local_glossary_metadata_section(ctx)
    if not metadata_section:
        return clean_glossary
    if not clean_glossary:
        return metadata_section
    return f"{metadata_section}\n\n{clean_glossary}"


def write_glossary_file(ctx: TranscriptContext, glossary: str) -> str:
    clean_glossary = glossary.strip()
    if not clean_glossary:
        return ""
    with open(ctx.glossary, "w", encoding="utf-8") as f:
        f.write(clean_glossary)
        f.write("\n")
    return clean_glossary


def normalize_tavily_domain(domain: str) -> str:
    raw = str(domain or "").strip().lower()
    if not raw:
        return ""
    raw = re.sub(r"^\*\.", "", raw)
    parse_target = raw if "://" in raw else f"//{raw}"
    parsed = urlparse(parse_target)
    host = (parsed.netloc or parsed.path).split("/")[0]
    host = host.split("@")[-1].split(":")[0].strip().strip(".")
    if host.startswith("www."):
        host = host[4:]
    return host if "." in host else ""


def json_string_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [value]
    return []


def unique_tavily_domains(domains) -> list[str]:
    if isinstance(domains, str):
        domains = [domains]
    result: list[str] = []
    seen: set[str] = set()
    for raw in domains or []:
        domain = normalize_tavily_domain(str(raw))
        if not domain or domain in seen:
            continue
        seen.add(domain)
        result.append(domain)
    return result


@dataclass(frozen=True)
class TavilyTopicDomains:
    name: str
    keywords: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()

    @staticmethod
    def from_json_value(name: str, data) -> "TavilyTopicDomains":
        topic_name = str(name or "").strip()
        keywords = []
        domains = []
        if isinstance(data, dict):
            topic_name = str(data.get("name") or topic_name).strip()
            keywords = json_string_list(data.get("keywords", []))
            domains = data.get("domains", data.get("sites", []))
        elif isinstance(data, list):
            domains = data
        if topic_name and topic_name not in keywords:
            keywords = [topic_name, *list(keywords or [])]
        return TavilyTopicDomains(
            name=topic_name,
            keywords=tuple(unique_non_empty_strings(list(keywords or []))),
            domains=tuple(unique_tavily_domains(domains)),
        )

    def merge(self, other: "TavilyTopicDomains") -> "TavilyTopicDomains":
        return TavilyTopicDomains(
            name=self.name or other.name,
            keywords=tuple(unique_non_empty_strings([*self.keywords, *other.keywords])),
            domains=tuple(unique_tavily_domains([*self.domains, *other.domains])),
        )


@dataclass(frozen=True)
class TavilyDomainPreferences:
    global_domains: tuple[str, ...] = ()
    topics: tuple[TavilyTopicDomains, ...] = ()

    @staticmethod
    def from_json_value(data) -> "TavilyDomainPreferences":
        if not isinstance(data, dict):
            return TavilyDomainPreferences()
        global_raw = data.get("global_domains", data.get("global", []))
        if isinstance(global_raw, dict):
            global_raw = global_raw.get("domains", global_raw.get("sites", []))

        topics_by_name: dict[str, TavilyTopicDomains] = {}
        raw_topics = data.get("topics", [])
        if isinstance(raw_topics, dict):
            topic_items = raw_topics.items()
        elif isinstance(raw_topics, list):
            topic_items = ((str(item.get("name", "")) if isinstance(item, dict) else "", item) for item in raw_topics)
        else:
            topic_items = []
        for raw_name, raw_topic in topic_items:
            topic = TavilyTopicDomains.from_json_value(raw_name, raw_topic)
            if not topic.name or not topic.domains:
                continue
            key = topic.name.casefold()
            topics_by_name[key] = topics_by_name[key].merge(topic) if key in topics_by_name else topic

        return TavilyDomainPreferences(
            global_domains=tuple(unique_tavily_domains(global_raw)),
            topics=tuple(topics_by_name.values()),
        )

    def merge(self, other: "TavilyDomainPreferences") -> "TavilyDomainPreferences":
        topics_by_name = {topic.name.casefold(): topic for topic in self.topics}
        for topic in other.topics:
            key = topic.name.casefold()
            topics_by_name[key] = topics_by_name[key].merge(topic) if key in topics_by_name else topic
        return TavilyDomainPreferences(
            global_domains=tuple(unique_tavily_domains([*self.global_domains, *other.global_domains])),
            topics=tuple(topics_by_name.values()),
        )


def load_tavily_domain_preferences(base_dir: str = "") -> TavilyDomainPreferences:
    root = base_dir or os.path.dirname(os.path.abspath(__file__))
    preferences = TavilyDomainPreferences()
    for filename in ("tavily_domains.example.json", "tavily_domains.json"):
        path = os.path.join(root, filename)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                preferences = preferences.merge(TavilyDomainPreferences.from_json_value(json.load(f)))
        except (json.JSONDecodeError, OSError) as e:
            print(f"  Warning: failed to load {filename}: {e}", file=sys.stderr)
    return preferences


def select_tavily_preferred_domains(
    query: str,
    fields: dict,
    preferences: TavilyDomainPreferences,
    topic_hints: Optional[list[str]] = None,
) -> list[str]:
    domains: list[str] = [*preferences.global_domains]
    tags = fields.get("tags", [])
    if not isinstance(tags, list):
        tags = []
    haystack = "\n".join(
        str(part)
        for part in [
            query,
            fields.get("title", ""),
            fields.get("uploader", ""),
            fields.get("description", ""),
            " ".join(str(tag) for tag in tags),
            " ".join(str(hint) for hint in (topic_hints or [])),
        ]
        if part
    )
    match_text = tavily_query_dedupe_key(haystack)
    for topic in preferences.topics:
        keywords = unique_non_empty_strings([topic.name, *topic.keywords])
        keyword_keys = [tavily_query_dedupe_key(keyword) for keyword in keywords]
        if any(key and key in match_text for key in keyword_keys):
            domains.extend(topic.domains)
    return unique_tavily_domains(domains)


def tavily_url_host(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    host = parsed.netloc.lower()
    if not host and parsed.path and "://" not in str(url):
        host = parsed.path.split("/")[0].lower()
    host = host.split("@")[-1].split(":")[0].strip(".")
    return host[4:] if host.startswith("www.") else host


def tavily_url_matches_domains(url: str, preferred_domains: list[str]) -> bool:
    host = tavily_url_host(url)
    if not host:
        return False
    for domain in unique_tavily_domains(preferred_domains):
        if host == domain or host.endswith(f".{domain}"):
            return True
    return False


def tavily_url_key(url: str) -> str:
    raw = str(url or "").strip()
    parsed = urlparse(raw)
    if parsed.scheme and parsed.netloc:
        path = parsed.path.rstrip("/")
        query = f"?{parsed.query}" if parsed.query else ""
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}{query}"
    return raw.rstrip("/").casefold()


def merge_tavily_results(
    preferred_results: list[dict],
    general_results: Optional[list[dict]] = None,
    preferred_domains: Optional[list[str]] = None,
    max_results: int = 0,
) -> list[dict]:
    domains = unique_tavily_domains(preferred_domains or [])
    decorated: list[tuple[int, int, dict]] = []
    order = 0
    for stage_bonus, results in ((1, preferred_results), (0, general_results or [])):
        for result in results or []:
            if not isinstance(result, dict):
                continue
            url = str(result.get("url", "")).strip()
            if not url:
                continue
            domain_score = 10 if tavily_url_matches_domains(url, domains) else 0
            decorated.append((-(domain_score + stage_bonus), order, result))
            order += 1
    decorated.sort(key=lambda item: (item[0], item[1]))

    merged: list[dict] = []
    seen_urls: set[str] = set()
    for _, _, result in decorated:
        url_key = tavily_url_key(str(result.get("url", "")))
        if not url_key or url_key in seen_urls:
            continue
        seen_urls.add(url_key)
        merged.append(result)
        if max_results and len(merged) >= max_results:
            break
    return merged


def _tavily_client_search(client, query: str, max_results: int, include_domains: Optional[list[str]] = None) -> list[dict]:
    kwargs = {
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
    }
    if include_domains:
        kwargs["include_domains"] = include_domains
    data = client.search(**kwargs)
    if not isinstance(data, dict):
        return []
    results = data.get("results", [])
    return results if isinstance(results, list) else []


def tavily_search(
    query: str,
    api_key: str,
    max_results: int = 5,
    preferred_domains: Optional[list[str]] = None,
) -> list[dict]:
    max_results = max(1, int(max_results or 1))
    domains = unique_tavily_domains(preferred_domains or [])
    try:
        client = TavilyClient(api_key=api_key)
    except Exception as e:
        print(f"  Warning: Tavily client init failed: {e}", file=sys.stderr)
        return []

    preferred_results: list[dict] = []
    if domains:
        try:
            preferred_results = _tavily_client_search(client, query, max_results, include_domains=domains)
        except Exception as e:
            print(f"  Warning: Tavily preferred-domain search failed: {e}", file=sys.stderr)

    preferred_unique = merge_tavily_results(preferred_results, preferred_domains=domains, max_results=max_results)
    if domains and len(preferred_unique) >= max_results:
        return preferred_unique

    general_results: list[dict] = []
    try:
        general_results = _tavily_client_search(client, query, max_results)
    except Exception as e:
        print(f"  Warning: Tavily search failed: {e}", file=sys.stderr)

    return merge_tavily_results(preferred_results, general_results, preferred_domains=domains, max_results=max_results)


GENERIC_TAVILY_TAGS = {
    "video",
    "youtube",
    "podcast",
    "reaction",
    "short",
    "shorts",
    "vlog",
    "interview",
    "clips",
    "clip",
    "highlights",
    "highlight",
    "trailer",
    "official",
    "channel",
    "tag",
    "tags",
    "generic tag",
}


def is_substantive_tavily_tag(tag: str) -> bool:
    clean = re.sub(r"\s+", " ", str(tag).strip())
    if len(clean) < 3:
        return False
    lowered = clean.lower().strip("#")
    if lowered in GENERIC_TAVILY_TAGS:
        return False
    if re.fullmatch(r"[\W_]+", clean):
        return False
    return True


def tavily_query_dedupe_key(query: str) -> str:
    clean = unicodedata.normalize("NFKC", str(query))
    clean = "".join(ch for ch in clean if unicodedata.category(ch) != "Cf")
    clean = clean.casefold()
    clean = re.sub(r"[`'’‘´]", "", clean)
    clean = re.sub(r"[‐‑‒–—―-]+", " ", clean)
    clean = re.sub(r"[^\w\s+#]+", " ", clean, flags=re.UNICODE)
    clean = re.sub(r"\s+", " ", clean).strip()
    return clean


def normalize_tavily_query(query: str) -> str:
    return re.sub(r"\s+", " ", str(query).strip())[:200].rstrip()


def unique_tavily_queries(
    raw_queries: list[str],
    max_queries: int,
    seen_keys: Optional[set[str]] = None,
) -> list[str]:
    query_by_key: dict[str, str] = {}
    seen = seen_keys if seen_keys is not None else set()
    for raw in raw_queries:
        if len(query_by_key) >= max_queries:
            break
        query = normalize_tavily_query(raw)
        if not query:
            continue
        key = tavily_query_dedupe_key(query)
        if not key or key in seen:
            continue
        seen.add(key)
        query_by_key[key] = query
    return list(query_by_key.values())


def merge_tavily_queries_with_fallbacks(agent_queries: list[str], fields: dict, max_queries: int = 8) -> list[str]:
    title = str(fields.get("title", "")).strip()
    uploader = str(fields.get("uploader", "")).strip()
    tags = fields.get("tags", [])
    candidates: list[str] = []
    if title:
        candidates.append(title)
        if uploader:
            candidates.append(f"{title} {uploader}")
        if isinstance(tags, list):
            for tag in tags:
                clean_tag = re.sub(r"\s+", " ", str(tag).strip())
                if is_substantive_tavily_tag(clean_tag):
                    candidates.append(f"{title} {clean_tag}")
    candidates.extend(agent_queries)

    return unique_tavily_queries(candidates, max_queries)


def merge_source_and_target_tavily_queries(
    source_queries: list[str],
    target_queries: list[str],
    max_queries_per_language: int,
) -> list[str]:
    max_queries_per_language = max(1, int(max_queries_per_language or 1))
    seen: set[str] = set()
    source_unique = unique_tavily_queries(source_queries, max_queries_per_language, seen)
    target_unique = unique_tavily_queries(target_queries, max_queries_per_language, seen)

    queries: list[str] = []
    max_len = max(len(source_unique), len(target_unique))
    for idx in range(max_len):
        if idx < len(source_unique):
            queries.append(source_unique[idx])
        if idx < len(target_unique):
            queries.append(target_unique[idx])
    return queries


def translate_tavily_query_output(
    source_queries: list[str],
    ctx: TranscriptContext,
    llm: LLMConfig,
    quiet: bool = False,
    topic_hints: Optional[list[str]] = None,
) -> TavilyQueryOutput:
    if not source_queries or ctx.source_lang_code == ctx.target_lang_code:
        return TavilyQueryOutput([])
    fields = {
        "source_language": ctx.source_lang_code,
        "target_language": ctx.target_lang_code,
        "queries": source_queries,
    }
    if topic_hints:
        fields["topic_hints"] = topic_hints
    request = LLMObjectRequest(fields)
    try:
        response_obj = llm_json_once(
            llm,
            tavily_query_translate_system_prompt(ctx),
            request,
            temperature=0.1,
            raw_label=None if quiet else "translate_tavily_query_output",
        )
        return TavilyQueryOutput.from_json_value(response_obj, max_queries=len(source_queries))
    except Exception as e:
        if not quiet:
            print(f"  Warning: Tavily query translation failed: {e}", file=sys.stderr)
        return TavilyQueryOutput([])


def build_tavily_search_plan(
    transcript: Transcript,
    ctx: TranscriptContext,
    llm: LLMConfig,
    quiet: bool = False,
    max_queries: int = 8,
    retriever: EmbeddingRetriever | None = None,
) -> TavilySearchPlan:
    fields = read_video_metadata_fields(ctx)
    description = filter_video_description_for_glossary(fields["description"], max_chars=1200)[0]
    retrieved_context: list[dict] = []
    if retriever is not None:
        semantic_query = "\n".join(
            part
            for part in [
                fields["title"],
                fields["uploader"],
                " ".join(fields["tags"][:20]),
                description,
            ]
            if part
        ).strip()
        if not semantic_query:
            semantic_query = "\n".join(transcript.text_lines()[:50]).strip()
        if semantic_query:
            retrieved = retriever.retrieve_texts([semantic_query], top_k=8)
            if retrieved:
                retrieved_context = retrieved[0]
    request_fields = {
        "title": fields["title"],
        "uploader": fields["uploader"],
        "url": fields["webpage_url"],
        "upload_time": fields["upload_time"],
        "description": description,
        "tags": fields["tags"][:20],
        "source_language": ctx.source_lang_code,
        "target_language": ctx.target_lang_code,
    }
    if retrieved_context:
        request_fields["retrieved_transcript_context"] = retrieved_context
    else:
        request_fields["transcript_excerpt"] = "\n".join(transcript.text_lines())[:3000]
    request = LLMObjectRequest(request_fields)
    try:
        response_obj = llm_json_once(
            llm,
            tavily_query_system_prompt(ctx),
            request,
            temperature=0.2,
            raw_label=None if quiet else "build_tavily_search_plan",
        )
        agent_output = TavilyQueryOutput.from_json_value(response_obj, max_queries=max_queries)
        source_queries = merge_tavily_queries_with_fallbacks(agent_output.queries, fields, max_queries=max_queries)
        target_output = translate_tavily_query_output(
            source_queries,
            ctx,
            llm,
            quiet=quiet,
            topic_hints=agent_output.topic_hints,
        )
        queries = merge_source_and_target_tavily_queries(
            source_queries,
            target_output.queries,
            max_queries_per_language=max_queries,
        )
        topic_hints = unique_non_empty_strings([*agent_output.topic_hints, *target_output.topic_hints], 32)
        return TavilySearchPlan(queries=queries, topic_hints=topic_hints)
    except Exception as e:
        if not quiet:
            print(f"  Warning: Tavily query agent failed: {e}", file=sys.stderr)
        source_queries = merge_tavily_queries_with_fallbacks([], fields, max_queries=max_queries)
        target_output = translate_tavily_query_output(source_queries, ctx, llm, quiet=quiet)
        queries = merge_source_and_target_tavily_queries(
            source_queries,
            target_output.queries,
            max_queries_per_language=max_queries,
        )
        return TavilySearchPlan(queries=queries, topic_hints=target_output.topic_hints)


def tavily_domain_preferences_to_json(preferences: TavilyDomainPreferences) -> dict:
    return {
        "global_domains": list(preferences.global_domains),
        "topics": [
            {
                "name": topic.name,
                "keywords": list(topic.keywords),
                "domains": list(topic.domains),
            }
            for topic in preferences.topics
        ],
    }


@dataclass(frozen=True)
class GlossaryBuildOptions:
    tavily_key: str = ""
    tavily_max_results: int = 20
    tavily_max_queries: int = 15
    quiet: bool = False
    retriever: EmbeddingRetriever = None
    force: bool = False

    @staticmethod
    def from_env(env: dict[str, str], quiet: bool = False, retriever=None, force: bool = False) -> "GlossaryBuildOptions":
        return GlossaryBuildOptions(
            tavily_key=env.get("TAVILY_API_KEY", ""),
            tavily_max_results=env_int(env.get("TAVILY_MAX_RESULTS", ""), 20),
            tavily_max_queries=env_int(env.get("TAVILY_MAX_QUERIES", ""), 15),
            quiet=quiet,
            retriever=retriever,
            force=force,
        )

    def use_tool_session(self) -> bool:
        return bool(self.tavily_key and int(self.tavily_max_queries or 0) > 0)


@dataclass(frozen=True)
class GlossaryRequestArgs:
    metadata_fields: dict
    retriever: EmbeddingRetriever = None
    tavily_preferences: Optional[TavilyDomainPreferences] = None


@dataclass(frozen=True)
class GlossaryToolRuntime:
    tavily_key: str
    metadata_fields: dict
    preferences: TavilyDomainPreferences
    max_results: int

    def execute_tavily_search(self, args: dict) -> dict:
        query = re.sub(r"\s+", " ", str(args.get("query", "")).strip())
        topic_hints = json_string_list(args.get("topic_hints", []))
        requested_domains = unique_tavily_domains(json_string_list(args.get("preferred_domains", [])))
        if not query:
            return {"error": "missing query", "results": []}

        preferred_domains = unique_tavily_domains(
            [
                *select_tavily_preferred_domains(
                    query,
                    self.metadata_fields,
                    self.preferences,
                    topic_hints=topic_hints,
                ),
                *requested_domains,
            ]
        )
        results = tavily_search(
            query,
            self.tavily_key,
            max_results=max(1, min(self.max_results, 3)),
            preferred_domains=preferred_domains,
        )
        return {
            "query": query,
            "preferred_domains": preferred_domains,
            "results": [
                {
                    "url": str(item.get("url", ""))[:500],
                    "title": str(item.get("title", ""))[:200],
                    "content": str(item.get("content", ""))[:1000],
                }
                for item in results
                if isinstance(item, dict)
            ],
        }


def glossary_tavily_tool_schema() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "tavily_search",
            "description": (
                "Search the web for authoritative terminology, names, works, quotes, "
                "background concepts, and ASR corrections needed to build the glossary."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Compact keyword query. Correct likely ASR errors before searching.",
                    },
                    "topic_hints": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional topic/category hints used to select preferred domains.",
                    },
                    "preferred_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional extra authoritative domains to prefer for this query.",
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


def tool_call_to_message_value(tool_call) -> dict:
    function = get_message_value(tool_call, "function")
    return {
        "id": str(get_message_value(tool_call, "id", "")),
        "type": str(get_message_value(tool_call, "type", "function") or "function"),
        "function": {
            "name": str(get_message_value(function, "name", "")),
            "arguments": str(get_message_value(function, "arguments", "") or "{}"),
        },
    }


def get_message_value(value, key: str, default=None):
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def assistant_message_to_json_value(message) -> dict:
    data = {
        "role": str(get_message_value(message, "role", "assistant") or "assistant"),
        "content": get_message_value(message, "content", None),
    }
    tool_calls = get_message_value(message, "tool_calls", None) or []
    if tool_calls:
        data["tool_calls"] = [tool_call_to_message_value(call) for call in tool_calls]
    return data


def parse_tool_arguments(tool_call) -> dict:
    function = get_message_value(tool_call, "function")
    raw_args = str(get_message_value(function, "arguments", "") or "{}")
    try:
        args = json.loads(raw_args)
    except json.JSONDecodeError:
        return {}
    return args if isinstance(args, dict) else {}


def is_glossary_tool_call_format_issue(choice, message) -> bool:
    if get_message_value(message, "tool_calls", None):
        return False
    finish_reason = str(get_message_value(choice, "finish_reason", "") or "").strip()
    if finish_reason == "tool_calls":
        return True
    content = str(get_message_value(message, "content", "") or "").strip()
    return bool(content and "tool_calls" in content and not content.startswith("{"))


def build_glossary_request_fields(
    transcript: Transcript,
    ctx: TranscriptContext,
    args: GlossaryRequestArgs,
) -> dict:
    metadata_fields = args.metadata_fields
    title = metadata_fields["title"]
    desc_text = metadata_fields["description"]
    tags = metadata_fields["tags"]
    transcript_text = "\n".join(transcript.text_lines())
    request_fields = {
        "title": title,
        "uploader": metadata_fields["uploader"],
        "url": metadata_fields["webpage_url"],
        "upload_time": metadata_fields["upload_time"],
        "source_language": ctx.source_lang_code,
        "target_language": ctx.target_lang_code,
        "description": desc_text[:1000],
        "tags": tags[:20],
    }
    if args.tavily_preferences is not None:
        request_fields["tavily_domain_preferences"] = tavily_domain_preferences_to_json(args.tavily_preferences)
        request_fields["tool_instructions"] = (
            "Use tavily_search when web evidence is needed. Prefer compact keyword queries, "
            "correct likely ASR mistakes before searching, and use the provided domain preferences."
        )
    if args.retriever is not None:
        query = "\n".join([title, desc_text[:2000], " ".join(tags[:20]), transcript_text[:4000]]).strip()
        retrieved = args.retriever.retrieve_texts([query], top_k=12)
        if retrieved:
            request_fields["retrieved_context"] = retrieved[0]
    if "retrieved_context" not in request_fields:
        request_fields["transcript_excerpt"] = transcript_text[:8000]
    return request_fields


def build_glossary_with_tools(
    transcript: Transcript,
    ctx: TranscriptContext,
    llm: LLMConfig,
    options: GlossaryBuildOptions,
) -> str:
    metadata_fields = read_video_metadata_fields(ctx)
    preferences = load_tavily_domain_preferences()
    runtime = GlossaryToolRuntime(
        tavily_key=options.tavily_key,
        metadata_fields=metadata_fields,
        preferences=preferences,
        max_results=options.tavily_max_results,
    )
    request = LLMObjectRequest(
        build_glossary_request_fields(
            transcript,
            ctx,
            GlossaryRequestArgs(
                metadata_fields=metadata_fields,
                retriever=options.retriever,
                tavily_preferences=preferences,
            ),
        )
    )
    session = ChatSession(
        llm,
        glossary_system_prompt(ctx, options.retriever)
        + "\n\n"
        + (
            "You may call tavily_search for web evidence before returning the final glossary JSON. "
            "When tool calls are no longer available, return the best final glossary JSON using the evidence already provided."
        ),
        temperature=0.3,
        disable_response_format=True,
    )
    session.messages.append({"role": "user", "content": request.to_json_text()})
    tools = [glossary_tavily_tool_schema()]
    max_tool_queries = max(0, int(options.tavily_max_queries or 0))
    used_tool_queries = 0
    max_format_retries = max(1, max_tool_queries)
    format_retries = 0

    for _ in range(max_tool_queries + max_format_retries + 2):
        allow_tools = used_tool_queries < max_tool_queries
        kwargs = {
            "tools": tools,
            "tool_choice": "auto" if allow_tools else "none",
        }
        response = session.create(
            retry_template=CompletionRetryTemplate(
                attempts=3,
                quiet=options.quiet,
                label="Glossary tool completion",
            ),
            **kwargs,
        )
        choice = response.choices[0]
        message = choice.message
        tool_calls = get_message_value(message, "tool_calls", None) or []
        if allow_tools and is_glossary_tool_call_format_issue(choice, message):
            format_retries += 1
            if format_retries > max_format_retries:
                raise RuntimeError("glossary tool call format retry limit reached")
            if not options.quiet:
                print(
                    f"Glossary: malformed tool-call response, retrying "
                    f"({format_retries}/{max_format_retries})",
                    file=sys.stderr,
                )
            continue
        if tool_calls and allow_tools:
            format_retries = 0
            session.messages.append(assistant_message_to_json_value(message))
            for tool_call in tool_calls:
                tool_name = get_message_value(get_message_value(tool_call, "function"), "name", "")
                if used_tool_queries >= max_tool_queries:
                    tool_result = {"error": "Tavily query budget exhausted", "results": []}
                elif tool_name != "tavily_search":
                    used_tool_queries += 1
                    tool_result = {"error": f"unknown tool: {tool_name}", "results": []}
                else:
                    used_tool_queries += 1
                    tool_result = runtime.execute_tavily_search(parse_tool_arguments(tool_call))
                if not options.quiet:
                    print(
                        f"Glossary tool result ({tool_name}, {used_tool_queries}/{max_tool_queries}): "
                        f"{tool_result.get('query', '')}",
                        file=sys.stderr,
                    )
                session.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(get_message_value(tool_call, "id", "")),
                        "content": json.dumps(tool_result, ensure_ascii=False),
                    }
                )
            continue
        if tool_calls:
            raise RuntimeError("glossary Tavily query budget reached before final answer")
        content = str(get_message_value(message, "content", "") or "")
        if not options.quiet:
            print("build_glossary raw response:", file=sys.stderr)
            print(content, file=sys.stderr)
        return GlossaryOutput.from_json_content(content).markdown

    raise RuntimeError("glossary tool session ended without final answer")


def write_glossary_generation_fallback(ctx: TranscriptContext, options: GlossaryBuildOptions) -> str:
    glossary = write_glossary_file(ctx, ensure_local_metadata_in_glossary("", ctx))
    if glossary and not options.quiet:
        print(f"Glossary fallback: {ctx.glossary}", file=sys.stderr)
    return glossary


def build_tavily_search_evidence(
    transcript: Transcript,
    ctx: TranscriptContext,
    llm: LLMConfig,
    metadata_fields: dict,
    options: GlossaryBuildOptions,
) -> str:
    if not options.tavily_key or int(options.tavily_max_queries or 0) <= 0:
        return ""

    all_results = []
    all_preferred_domains: list[str] = []
    domain_preferences = load_tavily_domain_preferences()
    search_plan = build_tavily_search_plan(
        transcript,
        ctx,
        llm,
        quiet=options.quiet,
        max_queries=max(1, options.tavily_max_queries),
        retriever=options.retriever,
    )
    per_query_results = max(1, min(options.tavily_max_results, 3))
    for q in search_plan.queries:
        preferred_domains = select_tavily_preferred_domains(
            q,
            metadata_fields,
            domain_preferences,
            topic_hints=search_plan.topic_hints,
        )
        all_preferred_domains.extend(preferred_domains)
        if not options.quiet:
            domain_hint = f" ({len(preferred_domains)} preferred domains)" if preferred_domains else ""
            print(f"  Searching: {q[:60]}{domain_hint}", file=sys.stderr)
        all_results.extend(tavily_search(q, options.tavily_key, per_query_results, preferred_domains=preferred_domains))

    unique = merge_tavily_results(
        all_results,
        preferred_domains=unique_tavily_domains(all_preferred_domains),
        max_results=max(1, options.tavily_max_results),
    )
    return "\n\n".join(
        f"Source: {r.get('url', '')}\n{r.get('content', '')[:500]}"
        for r in unique
    )


def build_glossary(
    transcript: Transcript,
    ctx: TranscriptContext,
    llm: LLMConfig,
    options: Optional[GlossaryBuildOptions] = None,
) -> str:
    options = options or GlossaryBuildOptions()
    if not options.force and os.path.isfile(ctx.glossary) and os.path.getsize(ctx.glossary) > 0:
        glossary = write_glossary_file(ctx, ensure_local_metadata_in_glossary(_read_text_file(ctx.glossary), ctx))
        if not options.quiet:
            print(f"Glossary cache: {ctx.glossary}", file=sys.stderr)
        return glossary

    metadata_fields = read_video_metadata_fields(ctx)
    title = metadata_fields["title"]
    tags = metadata_fields["tags"]
    desc_text = metadata_fields["description"]
    transcript_text = "\n".join(transcript.text_lines())

    if options.use_tool_session():
        if not options.quiet:
            print(
                f"Glossary: generating with {llm.provider} / {llm.model_name()} "
                f"(Tavily queries={options.tavily_max_queries})",
                file=sys.stderr,
            )
        try:
            glossary_markdown = build_glossary_with_tools(
                transcript,
                ctx,
                llm,
                options,
            )
            glossary = write_glossary_file(ctx, ensure_local_metadata_in_glossary(glossary_markdown, ctx))
            if not options.quiet:
                print(f"Glossary: {ctx.glossary}", file=sys.stderr)
            return glossary
        except Exception as e:
            print(f"Warning: glossary tool session failed: {e}", file=sys.stderr)
            if not options.quiet:
                print("Glossary: falling back to query-agent Tavily search", file=sys.stderr)

    search_text = build_tavily_search_evidence(transcript, ctx, llm, metadata_fields, options)

    request_fields = {
        "title": title,
        "transcript_excerpt": transcript_text[:8000],
        "description": desc_text[:1000],
        "tags": tags[:20],
        "search_results": search_text[:4000] if search_text else "",
    }
    if options.retriever is not None:
        query = "\n".join([title, desc_text[:2000], " ".join(tags[:20]), transcript_text[:4000]]).strip()
        retrieved = options.retriever.retrieve_texts([query], top_k=12)
        if retrieved:
            request_fields["retrieved_context"] = retrieved[0]

    request = LLMObjectRequest(request_fields)

    if not options.quiet:
        print(f"Glossary: generating with {llm.provider} / {llm.model_name()}", file=sys.stderr)
    try:
        response_obj = llm_json_once(
            llm,
            glossary_system_prompt(ctx, options.retriever),
            request,
            temperature=0.3,
            raw_label=None if options.quiet else "build_glossary",
            disable_response_format=True,
        )
        glossary_output = GlossaryOutput.from_json_value(response_obj)
        glossary = ensure_local_metadata_in_glossary(glossary_output.markdown, ctx)
    except Exception as e:
        print(f"Warning: glossary generation failed: {e}", file=sys.stderr)
        return write_glossary_generation_fallback(ctx, options)

    glossary = write_glossary_file(ctx, glossary)
    if not options.quiet:
        print(f"Glossary: {ctx.glossary}", file=sys.stderr)
    return glossary


# --- LLM stages ---------------------------------------------------------------


def prune_empty_json(value):
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text if text else None
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            clean_item = prune_empty_json(item)
            if clean_item is not None:
                result[key] = clean_item
        return result or None
    if isinstance(value, list):
        result = []
        for item in value:
            clean_item = prune_empty_json(item)
            if clean_item is not None:
                result.append(clean_item)
        return result or None
    return value


def require_json_object(data, label: str = "response") -> dict:
    if not isinstance(data, dict):
        raise ValueError(f"{label} is not a JSON object")
    return data


def require_non_empty_string(data: dict, key: str, label: str) -> str:
    value = str(data.get(key, "")).strip()
    if not value:
        raise ValueError(f'{label} JSON object missing non-empty "{key}"')
    return value


def unique_non_empty_strings(values, max_items: int = 0) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = re.sub(r"\s+", " ", str(raw).strip())
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value[:200])
        if max_items and len(result) >= max_items:
            break
    return result


@dataclass
class LLMBatchItem:
    id: int
    fields: dict

    def to_json_value(self) -> dict:
        return prune_empty_json({"id": self.id, **self.fields}) or {"id": self.id}


def make_language_item(
    item_id: int,
    ctx: TranscriptContext,
    source=None,
    target=None,
    extra: Optional[dict] = None,
) -> LLMBatchItem:
    return LLMBatchItem(item_id, LanguageFields.from_ctx(ctx).build(source=source, target=target, extra=extra))


def make_source_item(
    item_id: int,
    ctx: TranscriptContext,
    source_text: str,
    retrieved_context: Optional[list[dict]] = None,
) -> LLMBatchItem:
    return make_language_item(
        item_id,
        ctx,
        source=source_text,
        extra={"retrieved_context": retrieved_context or []},
    )


def make_pair_item(
    item_id: int,
    ctx: TranscriptContext,
    source_text: str,
    target_text: str,
    retrieved_context: Optional[list[dict]] = None,
    context_before: Optional[list[dict]] = None,
    context_after: Optional[list[dict]] = None,
) -> LLMBatchItem:
    return make_language_item(
        item_id,
        ctx,
        source=source_text,
        target=target_text,
        extra={
            "retrieved_context": retrieved_context or [],
            "context_before": context_before or [],
            "context_after": context_after or [],
        },
    )


def make_pair_json(
    item_id: int,
    ctx: TranscriptContext,
    source_text: str,
    target_text: str,
) -> dict:
    return make_pair_item(item_id, ctx, source_text, target_text).to_json_value()


@dataclass
class LanguageTextResult:
    id: int
    source_text: str
    target_text: str

    @staticmethod
    def from_json_value(data: dict, ctx: TranscriptContext, require_source: bool = True) -> "LanguageTextResult":
        fields = LanguageFields.from_ctx(ctx)
        source_value = fields.get_source(data) if require_source else ""
        target_value = fields.get_target(data)
        return LanguageTextResult(
            int(data.get("id")),
            _strip_speaker_labels(str(source_value or "")),
            _strip_speaker_labels(str(target_value or "")),
        )


@dataclass
class SplitOutputItem:
    id: int
    source_parts: list[str]
    target_parts: list[str]

    @staticmethod
    def from_json_value(data: dict, ctx: TranscriptContext) -> "SplitOutputItem":
        fields = LanguageFields.from_ctx(ctx)
        source_items = fields.get_source(data)
        target_items = fields.get_target(data)
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

    def to_translate_outputs(self, ctx: TranscriptContext) -> list[LanguageTextResult]:
        result: list[LanguageTextResult] = []
        for item in self.items:
            try:
                result.append(LanguageTextResult.from_json_value(item, ctx, require_source=False))
            except (TypeError, ValueError):
                continue
        return result

    def to_proofread_outputs(self, ctx: TranscriptContext) -> list[LanguageTextResult]:
        result: list[LanguageTextResult] = []
        for item in self.items:
            try:
                result.append(LanguageTextResult.from_json_value(item, ctx, require_source=True))
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
        return prune_empty_json(dict(self.fields)) or {}

    def to_json_text(self) -> str:
        return json.dumps(self.to_json_value(), ensure_ascii=False, indent=2)


@dataclass
class TavilyQueryOutput:
    queries: list[str]
    topic_hints: list[str] = field(default_factory=list)

    @staticmethod
    def from_json_value(data, max_queries: int = 8) -> "TavilyQueryOutput":
        data = require_json_object(data, "Tavily query response")
        raw_queries = data.get("queries", [])
        if not isinstance(raw_queries, list):
            raise ValueError('Tavily query JSON object missing "queries" array')
        raw_topic_hints = data.get("topic_hints", data.get("topics", data.get("keywords", [])))
        return TavilyQueryOutput(
            queries=unique_non_empty_strings(raw_queries, max_queries),
            topic_hints=unique_non_empty_strings(json_string_list(raw_topic_hints) if isinstance(raw_topic_hints, str) else raw_topic_hints, 24),
        )


@dataclass
class TavilySearchPlan:
    queries: list[str]
    topic_hints: list[str] = field(default_factory=list)


@dataclass
class GlossaryOutput:
    markdown: str

    @staticmethod
    def from_json_value(data) -> "GlossaryOutput":
        data = require_json_object(data, "glossary response")
        return GlossaryOutput(require_non_empty_string(data, "markdown", "glossary"))

    @staticmethod
    def from_json_content(content: str) -> "GlossaryOutput":
        parsed = _extract_json_value(content)
        return GlossaryOutput.from_json_value(parsed)


@dataclass
class CompletionRetryTemplate:
    attempts: int = 3
    base_delay: float = 0.0
    quiet: bool = False
    label: str = "LLM completion"

    def normalized_attempts(self) -> int:
        return max(1, int(self.attempts or 1))

    def wait_seconds(self, attempt_index: int) -> float:
        return max(0.0, float(self.base_delay or 0.0) * float(attempt_index + 1))


@dataclass
class ChatSession:
    llm: LLMConfig
    system_prompt: str
    temperature: float = 0.3
    disable_response_format: bool = False
    messages: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.messages.append({"role": "system", "content": self.system_prompt})

    def create(self, retry_template: Optional[CompletionRetryTemplate] = None, **extra_kwargs):
        template = retry_template or CompletionRetryTemplate(attempts=1)
        last_error: Exception | None = None
        for attempt in range(template.normalized_attempts()):
            try:
                return self._create_once(extra_kwargs)
            except Exception as e:
                last_error = e
                if attempt >= template.normalized_attempts() - 1 or is_context_length_error(e):
                    raise
                self._wait_before_retry(template, attempt, e)
        raise RuntimeError(f"LLM completion failed: {last_error}")

    def _create_once(self, extra_kwargs: dict):
        kwargs = {
            "model": self.llm.model_name(),
            "messages": self.messages,
            "temperature": self.temperature,
        }
        provider_cfg = self.llm.cfg()
        request_kwargs = provider_cfg.get("request_kwargs")
        if request_kwargs is not None:
            if not isinstance(request_kwargs, dict):
                raise ValueError("provider request_kwargs must be a JSON object")
            kwargs.update(request_kwargs)
        response_format = provider_cfg.get("response_format")
        if response_format and "response_format" not in kwargs:
            kwargs["response_format"] = response_format
        kwargs.update(extra_kwargs)
        if self.disable_response_format:
            kwargs.pop("response_format", None)
        return self.llm._client().chat.completions.create(**kwargs)

    def ask(self, content: str, retry_template: Optional[CompletionRetryTemplate] = None) -> str:
        answer, _ = self.ask_validated(content, retry_template=retry_template)
        return answer

    def ask_validated(
        self,
        content: str,
        validator=None,
        retry_template: Optional[CompletionRetryTemplate] = None,
    ):
        template = retry_template or CompletionRetryTemplate(attempts=1)
        self.messages.append({"role": "user", "content": content})
        last_error: Exception | None = None
        for attempt in range(template.normalized_attempts()):
            try:
                resp = self._create_once({})
                answer = self._answer_from_response(resp)
                parsed = validator(answer) if validator is not None else answer
                self.messages.append({"role": "assistant", "content": answer})
                return answer, parsed
            except Exception as e:
                last_error = e
                if attempt >= template.normalized_attempts() - 1 or is_context_length_error(e):
                    raise
                self._wait_before_retry(template, attempt, e)
        raise RuntimeError(f"LLM completion failed: {last_error}")

    def _answer_from_response(self, resp) -> str:
        choice = resp.choices[0]
        message = choice.message
        answer = message.content or ""
        if not answer.strip():
            reasoning = getattr(message, "reasoning_content", None)
            refusal = getattr(message, "refusal", None)
            usage = getattr(resp, "usage", None)
            details = [
                f"provider={getattr(self.llm, 'provider', 'unknown')}",
                f"model={self.llm.model_name()}",
                f"finish_reason={getattr(choice, 'finish_reason', 'unknown')}",
            ]
            if refusal:
                details.append(f"refusal={refusal}")
            if reasoning:
                details.append(f"reasoning_chars={len(reasoning)}")
            if usage:
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    value = getattr(usage, key, None)
                    if value is not None:
                        details.append(f"{key}={value}")
                completion_details = getattr(usage, "completion_tokens_details", None)
                if completion_details:
                    reasoning_tokens = getattr(completion_details, "reasoning_tokens", None)
                    if reasoning_tokens is not None:
                        details.append(f"reasoning_tokens={reasoning_tokens}")
            raise RuntimeError(f"LLM returned empty message.content ({', '.join(details)})")
        return answer

    def _wait_before_retry(self, template: CompletionRetryTemplate, attempt_index: int, error: Exception) -> None:
        wait = template.wait_seconds(attempt_index)
        if not template.quiet:
            print(
                f"  {template.label} retry {attempt_index + 1}/{template.normalized_attempts()} "
                f"in {wait:g}s: {error}",
                file=sys.stderr,
            )
        if wait > 0:
            time.sleep(wait)


def llm_json_once(
    llm: LLMConfig,
    system_prompt: str,
    request: LLMObjectRequest,
    temperature: float = 0.3,
    raw_label: Optional[str] = None,
    disable_response_format: bool = False,
) -> dict:
    session = ChatSession(llm, system_prompt, temperature=temperature, disable_response_format=disable_response_format)
    content, response_obj = session.ask_validated(
        request.to_json_text(),
        lambda value: require_json_object(_extract_json_value(value), "response"),
        retry_template=CompletionRetryTemplate(
            attempts=3,
            quiet=raw_label is None,
            label=raw_label or "LLM JSON",
        ),
    )
    if raw_label:
        print(f"{raw_label} raw response:", file=sys.stderr)
        print(content, file=sys.stderr)
    return response_obj


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


def require_json_batch_response(content: str) -> LLMBatchResponse:
    data = _extract_json_batch(content)
    if data is None:
        raise ValueError('response is not a JSON object with an "items" array')
    return data


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
    raise_on_failure: bool = False,
) -> list:
    prompt = request.to_json_text()
    try:
        _content, data = session.ask_validated(
            prompt,
            require_json_batch_response,
            retry_template=CompletionRetryTemplate(
                attempts=retries,
                base_delay=3.0,
                quiet=quiet,
                label="LLM batch",
            ),
        )
        return data.to_items()
    except Exception as e:
        if raise_on_failure and is_context_length_error(e):
            raise
        message = f"LLM batch failed after {retries} attempts: {e}"
        if raise_on_failure:
            raise RuntimeError(message)
        print(f"Error: {message}", file=sys.stderr)
        return []


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
    retriever: EmbeddingRetriever | None = None,
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
        + ("\n\n" + _RETRIEVED_CONTEXT_RULES if retriever is not None else "")
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
        contexts = retriever.retrieve_texts([seg.en_text() for seg in batch]) if retriever is not None else [[] for _ in batch]
        request = LLMBatchRequest(
            [
                make_source_item(s.index, ctx, s.en_text(), retrieved_context=contexts[idx])
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
    retriever: EmbeddingRetriever | None = None,
    proofread_retrieval_top_k: int = 1,
) -> bool:
    events: list[SplitEvent] = []
    for seg in transcript.segments:
        events.extend(seg.split_events or [whole_segment_split_event(seg)])
    if not events:
        return False

    pr_llm = llm
    retrieval_top_k = max(1, int(proofread_retrieval_top_k or 1))
    if not quiet:
        print(f"Proofreader: {pr_llm.provider} / {pr_llm.model_name()}", file=sys.stderr)
        print(f"Total split events: {len(events)}", file=sys.stderr)

    proofread_format = render_prompt_template(_PROOFREAD_FORMAT, ctx)
    session = ChatSession(
        pr_llm,
        system_prompt
        + ("\n\n" + _RETRIEVED_CONTEXT_RULES if retriever is not None else "")
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
                make_pair_item(
                    item_offset + idx + 1,
                    ctx,
                    event.en,
                    event.zh,
                    retrieved_context=batch_contexts[idx],
                )
                for idx, event in enumerate(batch_events)
            ]
        )
        try:
            response_items = llm_numbered_batch(
                request,
                session,
                quiet,
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
                top_k=retrieval_top_k,
            )
            if retriever is not None
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
) -> list[dict]:
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
    return [make_pair_json(seg.index, ctx, seg.source_text(), seg.translation) for seg in candidates]


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
                make_pair_item(
                    seg.index,
                    ctx,
                    seg.source_text(),
                    seg.translation,
                    context_before=split_context_items(transcript, seg, ctx, split.context_window, before=True),
                    context_after=split_context_items(transcript, seg, ctx, split.context_window, before=False),
                )
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


def translate_description(
    ctx: TranscriptContext,
    llm: LLMConfig,
    quiet: bool,
    retriever: EmbeddingRetriever | None = None,
) -> str:
    title, webpage_url, tags = read_metadata(ctx)
    metadata_header = read_metadata_header(ctx)
    desc_text = _read_text_file(ctx.desc) if os.path.isfile(ctx.desc) else ""
    if not desc_text.strip() and not title:
        if metadata_header:
            with open(ctx.target_desc, "w", encoding="utf-8") as f:
                f.write(metadata_header)
        return ctx.target_desc
    request_fields = {
        "title": title,
        "url": webpage_url,
        "description": desc_text,
        "tags": tags,
    }
    if retriever is not None:
        query = "\n".join(
            part
            for part in [
                title,
                webpage_url,
                desc_text,
                " ".join(tags),
            ]
            if part
        ).strip()
        if query:
            retrieved = retriever.retrieve_texts([query], top_k=6)
            if retrieved:
                request_fields["retrieved_context"] = retrieved[0]
    request = LLMObjectRequest(
        request_fields
    )
    try:
        system_prompt = render_prompt_template(DESCRIPTION_TRANSLATE_PROMPT, ctx)
        if retriever is not None:
            system_prompt += "\n\n" + _RETRIEVED_CONTEXT_RULES
        response_obj = llm_json_once(
            llm,
            system_prompt + "\n\n" + _JSON_FORMAT + "\n\n" + _JSON_OBJECT_FORMAT,
            request,
            temperature=0.3,
            raw_label=None if quiet else "translate_description",
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
    parser.add_argument("--split-max-chars", type=int, default=DEFAULT_SPLIT_MAX_CHARS)
    parser.add_argument("--split-max-duration", type=float, default=DEFAULT_SPLIT_MAX_DURATION)
    parser.add_argument("--split-context-window", type=int, default=1, help="Neighbor segment count included as split-only context")
    parser.add_argument("--proofread", action="store_true", default=True)
    parser.add_argument("--no-proofread", action="store_true")
    parser.add_argument("--glossary", metavar="PATH")
    parser.add_argument("--scene-threshold", type=float, default=0.15)
    parser.add_argument("--snap-frames", type=int, default=7)
    parser.add_argument("--end-offset-frames", type=int, default=2)
    parser.add_argument("--min-scene-interval-frames", type=int, default=2)
    parser.add_argument("--min-duration", type=float, default=1.0)
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
    embedding_active = embedding_config.enabled and embedding_enabled_for_stage(args.only_beautify, args.only_glossary)
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
        if embedding_active:
            print(f"Embedding: {embedding_config.provider} / {embedding_config.model}")
            print(f"Embedding DB: {embedding_config.chroma_dir}")

    if embedding_active:
        if embedding_config.store != "chroma":
            print(f"Error: unsupported EMBEDDING_STORE={embedding_config.store}; only chroma is available.", file=sys.stderr)
            sys.exit(1)

    beautify_options = BeautifyOptions(
        scene_threshold=args.scene_threshold,
        snap_frames=args.snap_frames,
        end_offset_frames=args.end_offset_frames,
        min_scene_interval_frames=args.min_scene_interval_frames,
        min_duration=args.min_duration,
        min_gap=args.min_gap,
        max_gap_merge=args.max_gap_merge,
        no_scene_snap=args.no_scene_snap,
        aggressive=args.aggressive,
    )
    transcript = load_or_create_beautified(
        ctx, source, video_path, beautify_options, args.skip_beautify, args.force, args.quiet
    )
    retriever = None
    if embedding_active:
        retriever = refresh_embedding_retriever(
            transcript,
            embedding_config,
            env,
            args.quiet,
            ctx,
            fatal=True,
        )

    if args.only_beautify:
        print(f"OUTPUT_JSON={os.path.abspath(ctx.beautified_json)}")
        return

    llm = None
    if needs_translate_llm(args):
        provider = env.get("TRANSLATE_PROVIDER", "").strip()
        if not provider:
            print(
                f"Error: TRANSLATE_PROVIDER not set in .env. Available: {', '.join(load_providers())}",
                file=sys.stderr,
            )
            sys.exit(1)
        llm = translate_llm_from_env(env, args.batch_size)

    if not args.skip_knowledge and not required_glossary_provider(env):
        print(
            f"Error: GLOSSARY_PROVIDER or TRANSLATE_PROVIDER not set in .env. Available: {', '.join(load_providers())}",
            file=sys.stderr,
        )
        sys.exit(1)

    if not args.skip_knowledge:
        glossary_llm = glossary_llm_from_env(env, llm, batch_size=args.batch_size)
        build_glossary(
            transcript,
            ctx,
            glossary_llm,
            GlossaryBuildOptions.from_env(env, quiet=args.quiet, retriever=retriever, force=args.only_glossary),
        )
        if embedding_active:
            updated_retriever = refresh_embedding_retriever(
                transcript,
                embedding_config,
                env,
                args.quiet,
                ctx,
                warning_label="glossary index update failed",
            )
            retriever = updated_retriever or retriever

    if args.only_glossary:
        print(f"OUTPUT_GLOSSARY={os.path.abspath(ctx.glossary)}")
        return

    if llm is None:
        print(
            f"Error: TRANSLATE_PROVIDER not set in .env. Available: {', '.join(load_providers())}",
            file=sys.stderr,
        )
        sys.exit(1)

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
    glossary_text = load_glossary_prompt_context(glossary_path, retriever=retriever)
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
    if embedding_active:
        updated_retriever = refresh_embedding_retriever(
            transcript,
            embedding_config,
            env,
            args.quiet,
            ctx,
            warning_label="translation memory index update failed",
        )
        retriever = updated_retriever or retriever
    if args.proofread and not args.no_proofread and env.get("PROOFREAD", "1") != "0":
        proofread_llm = proofread_llm_from_env(env, llm, args.batch_size)
        changed = proofread_split_events(
            transcript,
            ctx,
            proofread_llm,
            proofread_prompt,
            args.quiet,
            retriever,
            proofread_retrieval_top_k=proofread_retrieval_top_k_from_env(env),
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
        translate_description(ctx, llm, args.quiet, retriever=retriever)

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
