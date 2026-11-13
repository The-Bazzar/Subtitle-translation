"""
translate_srt.py - JSON-first subtitle pipeline.

Flow:
  WhisperX JSON -> glossary -> joint bilingual translation -> timing -> ASS

SRT is an inspection output, not a primary input. Source text stays read-only
after glossary; ASR or optional post-translation MFA word times locate events.
"""

from __future__ import annotations

import argparse
import copy
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
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import urlparse

import langcodes
import language_data  # noqa: F401
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from openai import OpenAI
from tavily import TavilyClient

from subtitle_translation.chapters import original_chapter_media, read_original_chapters, translated_chapters
from subtitle_translation.artifacts import atomic_write_json, file_sha256, json_sha256, read_json_object
from subtitle_translation import source_review
from subtitle_translation.project_logging import log, logged_stage, project_run, validate_log_level


# --- Data model ---------------------------------------------------------------


class SplitStatus(str, Enum):
    PENDING = "pending"
    OK = "ok"
    FALLBACK = "fallback"
    UNSPLIT = "unsplit"
    SKIPPED = "skipped"

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
    WORD_TIMING_UNAVAILABLE = "word_timing_unavailable"

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
    source_range: Optional[tuple[int, int]] = None

    @staticmethod
    def from_json(data: dict) -> "TranscriptWord":
        return TranscriptWord(
            text=str(data.get("word") or data.get("text") or "").strip(),
            start=_float_or_none(data.get("start")),
            end=_float_or_none(data.get("end")),
            score=_float_or_none(data.get("score")),
            source_range=tuple(data["source_range"]) if data.get("source_range") else None,
        )

    def to_json(self) -> dict:
        result = {"word": self.text}
        if self.start is not None:
            result["start"] = round(self.start, 3)
        if self.end is not None:
            result["end"] = round(self.end, 3)
        if self.score is not None:
            result["score"] = self.score
        if self.source_range is not None:
            result["source_range"] = list(self.source_range)
        return result


@dataclass
class SplitEvent:
    start: Optional[float]
    end: Optional[float]
    en: str
    zh: str
    id: str = ""
    source_range: Optional[tuple[int, int]] = None

    @staticmethod
    def from_json(data: dict) -> "SplitEvent":
        return SplitEvent(
            start=None if data.get("timing_status") == "pending" else float(data.get("start", 0.0)),
            end=None if data.get("timing_status") == "pending" else float(data.get("end", 0.0)),
            en=str(data.get("en", "")),
            zh=str(data.get("zh", "")),
            id=str(data.get("id", "")),
            source_range=tuple(data["source_range"]) if data.get("source_range") else None,
        )

    def to_json(self) -> dict:
        data = {
            "en": self.en,
            "zh": self.zh,
        }
        if self.start is None and self.end is None:
            data["timing_status"] = "pending"
        else:
            data.update(start=round(self.start, 3), end=round(self.end, 3))
        if self.id:
            data["id"] = self.id
        if self.source_range is not None:
            data["source_range"] = list(self.source_range)
        return data


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
    event_plan_key: str = ""
    translation_key: str = ""
    proofread_key: str = ""
    word_timing: dict = field(default_factory=dict)
    source_range: Optional[tuple[int, int]] = None
    source_refs: list[str] = field(default_factory=list)
    text_origin: str = ""
    segment_timing: dict = field(default_factory=dict)
    manual_review: dict = field(default_factory=dict)

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
            event_plan_key=str(data.get("event_plan_key", "")),
            translation_key=str(data.get("translation_key", "")),
            proofread_key=str(data.get("proofread_key", "")),
            word_timing=dict(data.get("word_timing") or {}),
            source_range=tuple(data["source_range"]) if data.get("source_range") else None,
            source_refs=list(data.get("source_refs") or []),
            text_origin=str(data.get("text_origin") or ""),
            segment_timing=dict(data.get("segment_timing") or {}),
            manual_review=dict(data.get("manual_review") or {}),
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
        for name in ("event_plan_key", "translation_key", "proofread_key"):
            if getattr(self, name):
                data[name] = getattr(self, name)
        if self.word_timing:
            data["word_timing"] = self.word_timing
        if self.source_range is not None:
            data["source_range"] = list(self.source_range)
        for name in ("source_refs", "text_origin", "segment_timing", "manual_review"):
            if getattr(self, name):
                data[name] = getattr(self, name)
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
    web_evidence_json: str

    @staticmethod
    def from_json(
        json_path: str,
        output_ass: str = "",
        source_lang: str = "source",
        target_lang: str = "zh",
        beautified_json: str = "",
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
            beautified_json=(
                os.path.abspath(beautified_json)
                if beautified_json
                else os.path.join(directory, f"{base}.beautified.json")
            ),
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
            web_evidence_json=os.path.join(directory, f"{base}.web_evidence.json"),
        )


@dataclass
class WebEvidenceEntry:
    url: str
    title: str = ""
    content: str = ""
    domain: str = ""
    preferred_domain_hit: bool = False
    rank: int = 0

    @staticmethod
    def from_json_value(data) -> "WebEvidenceEntry":
        data = require_json_object(data, "web evidence entry")
        url = str(data.get("url", "")).strip()
        title = str(data.get("title", "")).strip()
        content = str(data.get("content", "")).strip()
        domain = str(data.get("domain", "")).strip() or tavily_url_host(url)
        return WebEvidenceEntry(
            url=url,
            title=title,
            content=content,
            domain=domain,
            preferred_domain_hit=bool(data.get("preferred_domain_hit", False)),
            rank=int(data.get("rank", 0) or 0),
        )

    def to_json_value(self) -> dict:
        return prune_empty_json(
            {
                "url": self.url.strip(),
                "title": self.title.strip(),
                "content": self.content.strip(),
                "domain": (self.domain.strip() or tavily_url_host(self.url)),
                "preferred_domain_hit": self.preferred_domain_hit,
                "rank": self.rank,
            }
        ) or {}


@dataclass
class WebEvidenceRecord:
    query: str
    topic_hints: list[str] = field(default_factory=list)
    preferred_domains: list[str] = field(default_factory=list)
    search_stage: str = ""
    results: list[WebEvidenceEntry] = field(default_factory=list)

    @staticmethod
    def from_json_value(data) -> "WebEvidenceRecord":
        data = require_json_object(data, "web evidence record")
        raw_results = data.get("results", [])
        results = []
        if isinstance(raw_results, list):
            for item in raw_results:
                try:
                    entry = WebEvidenceEntry.from_json_value(item)
                except Exception:
                    continue
                if entry.url and (entry.title or entry.content):
                    results.append(entry)
        return WebEvidenceRecord(
            query=str(data.get("query", "")).strip(),
            topic_hints=unique_non_empty_strings(json_string_list(data.get("topic_hints", [])), 24),
            preferred_domains=unique_tavily_domains(json_string_list(data.get("preferred_domains", []))),
            search_stage=str(data.get("search_stage", "")).strip(),
            results=results,
        )

    def to_json_value(self) -> dict:
        return prune_empty_json(
            {
                "query": self.query.strip(),
                "topic_hints": unique_non_empty_strings(self.topic_hints, 24),
                "preferred_domains": unique_tavily_domains(self.preferred_domains),
                "search_stage": self.search_stage.strip(),
                "results": [entry.to_json_value() for entry in self.results if entry.url],
            }
        ) or {}


@dataclass
class WebEvidenceSidecar:
    version: int = 1
    records: list[WebEvidenceRecord] = field(default_factory=list)

    @staticmethod
    def from_json_value(data) -> "WebEvidenceSidecar":
        if isinstance(data, list):
            raw_records = data
            version = 1
        else:
            data = require_json_object(data, "web evidence sidecar")
            raw_records = data.get("records", [])
            version = int(data.get("version", 1) or 1)
        records = []
        if isinstance(raw_records, list):
            for item in raw_records:
                try:
                    record = WebEvidenceRecord.from_json_value(item)
                except Exception:
                    continue
                if record.query and record.results:
                    records.append(record)
        return WebEvidenceSidecar(version=max(1, version), records=records)

    def to_json_value(self) -> dict:
        return {
            "version": max(1, int(self.version or 1)),
            "records": [record.to_json_value() for record in self.records if record.query and record.results],
        }

    def has_records(self) -> bool:
        return any(record.query and record.results for record in self.records)

    def unique_entries(self) -> list[tuple[WebEvidenceRecord, WebEvidenceEntry]]:
        seen_urls: set[str] = set()
        pairs: list[tuple[WebEvidenceRecord, WebEvidenceEntry]] = []
        for record in self.records:
            for entry in record.results:
                url_key = tavily_url_key(entry.url)
                if not url_key or url_key in seen_urls:
                    continue
                seen_urls.add(url_key)
                pairs.append((record, entry))
        return pairs

    def prompt_text(self, max_chars: int = 0) -> str:
        blocks = [
            f"Source: {entry.url}\n{entry.content[:500]}"
            for _, entry in self.unique_entries()
            if entry.url and entry.content
        ]
        text = "\n\n".join(blocks).strip()
        if max_chars and len(text) > max_chars:
            return text[:max_chars].rstrip()
        return text


@dataclass
class GlossaryBuildArtifact:
    markdown: str
    web_evidence: WebEvidenceSidecar = field(default_factory=WebEvidenceSidecar)


def resolve_provider_model(provider: str, configured_model: str = "") -> str:
    model = configured_model.strip()
    if model:
        return model
    provider_config = load_providers().get(provider, {})
    return str(provider_config.get("default_model", "")).strip()


@dataclass
class LLMConfig:
    provider: str
    model: str = ""
    api_key: Optional[str] = None
    batch_size: int = 50
    request_timeout: Optional[float] = None
    api_max_retries: Optional[int] = None

    def resolve_key(self) -> str:
        if self.api_key is None:
            self.api_key = get_api_key(self.provider, load_env(_config_root()))
        return self.api_key

    def cfg(self) -> dict:
        providers = load_providers()
        if self.provider not in providers:
            log("ERROR", f'Error: unknown provider: {self.provider}')
            log("INFO", f"Available: {', '.join(providers)}")
            sys.exit(1)
        return providers[self.provider]

    def model_name(self) -> str:
        model = self.model.strip()
        return model or str(self.cfg().get("default_model", "")).strip()

    def _client(self):
        provider_cfg = self.cfg()
        options = {}
        if self.request_timeout is not None:
            options["timeout"] = self.request_timeout
        if self.api_max_retries is not None:
            options["max_retries"] = self.api_max_retries
        return OpenAI(
            base_url=provider_cfg["url"],
            api_key=self.resolve_key(),
            default_headers=provider_cfg.get("extra_headers", {}),
            **options,
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
    model: str = ""
    store: str = "chroma"
    chroma_dir: str = ""
    top_k: int = 6
    chunk_chars: int = 800

    @staticmethod
    def from_env(env: dict[str, str], ctx: TranscriptContext) -> "EmbeddingConfig":
        enabled = env_flag(env.get("EMBEDDING_ENABLED", "0"))
        provider = (env.get("EMBEDDING_PROVIDER", "openai") or "openai").strip()
        model = resolve_provider_model(provider, env.get("EMBEDDING_MODEL", ""))
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


class SingleInputEmbeddings(Embeddings):
    def __init__(self, client: OpenAI, model: str):
        self.client = client
        self.model = model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        response = self.client.embeddings.create(
            model=self.model,
            input=text,
            encoding_format="float",
        )
        if len(response.data) != 1:
            raise ValueError("Embedding response must contain exactly one vector")
        return response.data[0].embedding


def embedding_function(config: EmbeddingConfig, env: dict[str, str]) -> SingleInputEmbeddings:
    providers = load_providers()
    if config.provider not in providers:
        raise ValueError(f"unknown embedding provider: {config.provider}")
    provider_cfg = providers[config.provider]
    if not config.model:
        raise ValueError(
            f"embedding model is empty for provider {config.provider}; "
            "set EMBEDDING_MODEL or provider.default_model"
        )
    key_name = provider_cfg["env_key"]
    api_key = env.get(key_name, "")
    if not api_key:
        raise ValueError(f"{key_name} not found in environment or .env file")
    client = OpenAI(
        base_url=provider_cfg["url"],
        api_key=api_key,
        default_headers=provider_cfg.get("extra_headers", {}),
    )
    return SingleInputEmbeddings(client, config.model)


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


def clip_text_for_embedding(text: str, max_chars: int) -> str:
    clean = re.sub(r"\s+", " ", str(text or "").strip())
    if len(clean) <= max_chars:
        return clean
    cut = clean[:max_chars]
    boundary = max(cut.rfind(". "), cut.rfind("; "), cut.rfind(", "), cut.rfind(" "))
    if boundary >= max_chars // 2:
        cut = cut[:boundary]
    return cut.rstrip()


def load_web_evidence_sidecar(path: str) -> WebEvidenceSidecar:
    if not path or not os.path.isfile(path):
        return WebEvidenceSidecar()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return WebEvidenceSidecar()
    try:
        return WebEvidenceSidecar.from_json_value(data)
    except Exception:
        return WebEvidenceSidecar()


def write_web_evidence_sidecar(ctx: TranscriptContext, sidecar: WebEvidenceSidecar) -> WebEvidenceSidecar:
    if not sidecar.has_records():
        if os.path.isfile(ctx.web_evidence_json):
            try:
                os.remove(ctx.web_evidence_json)
            except OSError:
                pass
        return sidecar
    with open(ctx.web_evidence_json, "w", encoding="utf-8") as f:
        json.dump(sidecar.to_json_value(), f, ensure_ascii=False, indent=2)
        f.write("\n")
    return sidecar


def build_web_evidence_chunks(ctx: TranscriptContext, chunk_chars: int) -> list[EmbeddingChunk]:
    sidecar = load_web_evidence_sidecar(ctx.web_evidence_json)
    if not sidecar.has_records():
        return []

    chunks: list[EmbeddingChunk] = []
    max_chars = max(240, int(chunk_chars or 1))
    for index, (record, entry) in enumerate(sidecar.unique_entries(), 1):
        query_text = "; ".join(unique_non_empty_strings([record.query], 3))
        topic_text = ", ".join(unique_non_empty_strings(record.topic_hints, 8))
        lines = []
        if query_text:
            lines.append(f"Query: {query_text}")
        if entry.domain:
            lines.append(f"Domain: {entry.domain}")
        if entry.title:
            lines.append(f"Title: {entry.title}")
        lines.append(f"URL: {entry.url}")
        if topic_text:
            lines.append(f"Topic hints: {topic_text}")
        if entry.content:
            lines.extend(["Evidence:", entry.content])
        context_text = "\n".join(lines).strip()
        if not context_text:
            continue
        chunks.append(
            EmbeddingChunk(
                chunk_id=f"web_evidence:{index}",
                source="web_evidence",
                text=clip_text_for_embedding(context_text, max_chars),
                metadata={
                    "kind": "web_evidence",
                    "path": ctx.web_evidence_json,
                    "url": entry.url,
                    "domain": entry.domain,
                    "query": record.query,
                    "search_stage": record.search_stage,
                },
                context_text=context_text,
            )
        )
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
    return chunk_id.startswith(("transcript:", "glossary:", "web_evidence:", "translation_memory:"))


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


CHROMA_WRITE_BATCH_SIZE = 64


def build_embedding_index(
    transcript: Transcript,
    config: EmbeddingConfig,
    env: dict[str, str],
    ctx: Optional[TranscriptContext] = None,
    existing_chunk_ids: Optional[list[str]] = None,
) -> str:
    if not config.enabled:
        return ""
    chunks = build_embedding_chunks(transcript, config.chunk_chars)
    if ctx is not None:
        chunks.extend(build_glossary_chunks(ctx, config.chunk_chars))
        chunks.extend(build_web_evidence_chunks(ctx, config.chunk_chars))
        chunks.extend(build_translation_memory_chunks(transcript, ctx))
    store = open_chroma_store(config, env)
    clear_embedding_chunks(store, existing_chunk_ids)
    for start in range(0, len(chunks), CHROMA_WRITE_BATCH_SIZE):
        batch = chunks[start : start + CHROMA_WRITE_BATCH_SIZE]
        store.add_documents(
            [chunk.to_document() for chunk in batch],
            ids=[chunk.chunk_id for chunk in batch],
        )
    log("INFO", f'Embedding index: {config.chroma_dir} ({len(chunks)} chunks)')
    return config.chroma_dir


@logged_stage("embedding")
def refresh_embedding_retriever(
    transcript: Transcript,
    config: EmbeddingConfig,
    env: dict[str, str],
    ctx: TranscriptContext,
    fatal: bool = False,
    warning_label: str = "embedding index failed",
) -> EmbeddingRetriever | None:
    try:
        build_embedding_index(transcript, config, env, ctx)
        return EmbeddingRetriever(config, env)
    except Exception as e:
        if fatal:
            log("ERROR", f'Error: {warning_label}: {e}')
            sys.exit(1)
        log("WARNING", f'Warning: {warning_label}: {e}')
        return None


def env_flag(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def env_int(value: str, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def embedding_enabled_for_stage(only_scenes: bool, only_glossary: bool) -> bool:
    return not only_scenes


@dataclass
class SceneDetectionOptions:
    scene_threshold: float = 0.15
    min_scene_interval_frames: int = 2
    fps: float = 24.0


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
_providers_cache_root = None

_TRANSLATE_PROMPT_FALLBACK = """You are a professional subtitle translator. Translate from ${SOURCE_LANG} to ${TARGET_LANG}.

Rules:
- The user message is a JSON object with an "items" array of transcript segment objects.
- Translate each item object to natural, fluent ${TARGET_LANG} subtitles.
- The input items are complete transcript sentences/segments, not subtitle fragments.
- Preserve meaning and tone. Do not omit, merge, split, or add items.
- Keep proper nouns, brand names, and technical terms in original form unless a standard ${TARGET_LANG} translation exists.
- Follow natural subtitle formatting for ${TARGET_LANG}."""

_PROOFREAD_PROMPT_FALLBACK = """Review only the ${TARGET_LANG} subtitle translation against the frozen ${SOURCE_LANG} source.
Fix clear mistranslations, omissions, unsupported additions and terminology errors.
Keep already-correct natural wording. Avoid cosmetic rewrites, unnecessary filler
or expansions that make short subtitles harder to read. Preserve jokes and tone.
Do not edit source text, event IDs, event count or timing. If uncertain, retain
the existing translation. Follow the full glossary and target subtitle style."""


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

_JSON_OBJECT_FORMAT = """Return a JSON object.
The first response character must be `{` and the last response character must be `}`."""

_RETRIEVED_CONTEXT_RULES = """RETRIEVED CONTEXT:
Some input items may include a "retrieved_context" array from the same project memory.
Use it only for terminology, names, recurring concepts, tone, and local consistency.
Do not output, translate, proofread, split, merge, or return retrieved_context items themselves."""


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

_GLOSSARY_FINALIZER_FORMAT = """GLOSSARY FINALIZER PROTOCOL:
Tool calls are not available in this stage. Do not request tools, write pseudo tool calls, or mention additional searches.
Use only the user JSON, provided web_evidence, glossary context, and transcript context.
Return the final glossary in exactly one of these machine-parseable formats:

Preferred JSON:
{"markdown": "# 术语知识库 - <title>\\n\\n## 背景\\n<content>\\n\\n## 核心术语\\n| 原文术语 | ${TARGET_LANG} 推荐译法 | 说明 |\\n|---|---|---|\\n| source term | recommended translation | reason |\\n\\n## 态度基调\\n- <content>\\n\\n## 关键论点\\n- <content>"}

Fallback tagged Markdown, only if you cannot reliably emit valid JSON:
<GLOSSARY_MARKDOWN>
# 术语知识库 - <title>

## 背景
<content>

## 核心术语
| 原文术语 | ${TARGET_LANG} 推荐译法 | 说明 |
|---|---|---|
| source term | recommended translation | reason |

## 态度基调
- <content>

## 关键论点
- <content>
</GLOSSARY_MARKDOWN>

Do not add prose before or after the JSON object or the tagged Markdown block."""


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


def glossary_base_prompt(ctx: TranscriptContext, retriever: EmbeddingRetriever | None) -> str:
    return (
        render_prompt_template(load_prompt("glossary_prompt", _GLOSSARY_PROMPT_FALLBACK), ctx)
        + ("\n\n" + _RETRIEVED_CONTEXT_RULES if retriever is not None else "")
    )


def glossary_system_prompt(ctx: TranscriptContext, retriever: EmbeddingRetriever | None) -> str:
    return (
        glossary_base_prompt(ctx, retriever)
        + "\n\n"
        + _JSON_FORMAT
        + "\n\n"
        + _JSON_OBJECT_FORMAT
        + "\n\n"
        + render_prompt_template(_GLOSSARY_FORMAT, ctx)
    )


def glossary_finalizer_system_prompt(ctx: TranscriptContext, retriever: EmbeddingRetriever | None) -> str:
    return glossary_base_prompt(ctx, retriever) + "\n\n" + render_prompt_template(_GLOSSARY_FINALIZER_FORMAT, ctx)


def _config_root() -> str:
    configured = os.environ.get("SUBTITLE_TRANSLATION_PROJECT_DIR", "").strip()
    if configured:
        return os.path.abspath(configured)
    return os.path.abspath(os.getcwd())


def load_providers() -> dict:
    global _providers_cache, _providers_cache_root
    root = _config_root()
    if _providers_cache is not None and _providers_cache_root == root:
        return _providers_cache
    config_path = os.path.join(root, "providers.json")
    if os.path.isfile(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if loaded:
                providers = dict(_BUILTIN_PROVIDERS)
                providers.update(loaded)
                _providers_cache = providers
                _providers_cache_root = root
                return _providers_cache
        except (json.JSONDecodeError, OSError):
            pass
    _providers_cache = dict(_BUILTIN_PROVIDERS)
    _providers_cache_root = root
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
        log("ERROR", f'Error: {key_name} not found in environment or .env file.')
        log("INFO", f'Set it in .env: {key_name}=your_key_here')
        sys.exit(1)
    return key


def load_prompt(filename: str, fallback: str) -> str:
    base = _config_root()
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
        log("ERROR", f'Error: no segments found in JSON: {json_path}')
        sys.exit(1)
    return Transcript(
        path=os.path.abspath(json_path),
        language=str(data.get("language", "en")),
        segments=segments,
        raw={k: v for k, v in data.items() if k != "segments"},
    )


def save_transcript(transcript: Transcript, output_path: str) -> None:
    destination = os.path.abspath(output_path)
    payload = transcript.to_json()
    payload.pop("word_segments", None)
    atomic_write_json(destination, payload)
    transcript.path = destination


def load_or_create_beautified(
    ctx: TranscriptContext,
    source: Transcript,
    video_path: str,
    options: SceneDetectionOptions,
    skip_scene_detection: bool,
    force: bool,
    source_identity: str = "",
) -> Transcript:
    if not skip_scene_detection:
        detect_project_scenes(ctx, video_path, options, )
    if (
        os.path.abspath(source.path) != os.path.abspath(ctx.beautified_json)
        and os.path.isfile(ctx.beautified_json)
        and not force
    ):
        try:
            cached = load_transcript(ctx.beautified_json)
        except (OSError, ValueError, TypeError, KeyError, SystemExit):
            cached = None
        if cached is not None and (not source_identity or cached.raw.get("_source_first_input") == source_identity):
            if "word_segments" in cached.raw:
                cached.raw.pop("word_segments")
                save_transcript(cached, ctx.beautified_json)
            log("INFO", f'Beautified JSON cache: {ctx.beautified_json}')
            return cached

    transcript = source
    if source_identity:
        transcript.raw["_source_first_input"] = source_identity
    save_transcript(transcript, ctx.beautified_json)
    log("INFO", f'Beautified JSON: {ctx.beautified_json}')
    return transcript


# --- Read-only scene detection -----------------------------------------------


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
    ) -> list[float]:
    if not video_path or not os.path.isfile(video_path):
        return []
    log("INFO", f'Scene detection: threshold={threshold:.2f}')
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
    options: SceneDetectionOptions,
    scene_changes: list[float],
    *, scene_input_key: str = "",
) -> None:
    payload = {
        "scene_input_key": scene_input_key,
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


@logged_stage("scene_detection")
def detect_project_scenes(ctx, video_path, options, ):
    """Detect and cache scene changes without reading or modifying subtitles."""
    if not video_path or not os.path.isfile(video_path):
        return []
    stat = os.stat(video_path)
    identity = json_sha256({"video": os.path.abspath(video_path), "size": stat.st_size,
                            "mtime_ns": stat.st_mtime_ns, "threshold": options.scene_threshold,
                            "min_interval_frames": options.min_scene_interval_frames})
    try:
        cached = read_json_object(ctx.scenes_json)
        if cached.get("scene_input_key") == identity and os.path.isfile(ctx.scenechange_txt):
            return [float(row["time"]) for row in cached["scene_changes"]]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    options.fps = get_frame_rate(video_path)
    changes = get_scene_changes(video_path, options.scene_threshold,
                                options.min_scene_interval_frames / options.fps, )
    write_scene_change_sidecars(ctx, video_path, options, changes, scene_input_key=identity)
    return changes


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
        "\n\n以下是本视频的术语知识库, 请在翻译、校对和简介翻译时严格遵循其中的术语理解、"
        "推荐译法、语气判断和一致性要求:\n\n"
        + content
    )


def load_glossary_prompt_context(glossary_path: str, retriever: EmbeddingRetriever | None) -> str:
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
    root = base_dir or _config_root()
    preferences = TavilyDomainPreferences()
    for filename in ("tavily_domains.example.json", "tavily_domains.json"):
        path = os.path.join(root, filename)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                preferences = preferences.merge(TavilyDomainPreferences.from_json_value(json.load(f)))
        except (json.JSONDecodeError, OSError) as e:
            log("WARNING", f'  Warning: failed to load {filename}: {e}')
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


def build_web_evidence_record(
    query: str,
    results: list[dict],
    topic_hints: Optional[list[str]] = None,
    preferred_domains: Optional[list[str]] = None,
    search_stage: str = "",
) -> WebEvidenceRecord:
    clean_query = re.sub(r"\s+", " ", str(query or "").strip())
    domains = unique_tavily_domains(preferred_domains or [])
    entries: list[WebEvidenceEntry] = []
    seen_urls: set[str] = set()
    for rank, item in enumerate(results or [], 1):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        url_key = tavily_url_key(url)
        if not url or not url_key or url_key in seen_urls:
            continue
        seen_urls.add(url_key)
        title = re.sub(r"\s+", " ", str(item.get("title", "")).strip())[:300]
        content = re.sub(r"\s+", " ", str(item.get("content", "")).strip())[:1600]
        if not title and not content:
            continue
        domain = str(item.get("domain", "")).strip() or tavily_url_host(url)
        entries.append(
            WebEvidenceEntry(
                url=url[:1000],
                title=title,
                content=content,
                domain=domain,
                preferred_domain_hit=tavily_url_matches_domains(url, domains),
                rank=rank,
            )
        )
    return WebEvidenceRecord(
        query=clean_query,
        topic_hints=unique_non_empty_strings(topic_hints or [], 24),
        preferred_domains=domains,
        search_stage=str(search_stage or "").strip(),
        results=entries,
    )


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
        log("WARNING", f'  Warning: Tavily client init failed: {e}')
        return []

    preferred_results: list[dict] = []
    if domains:
        try:
            preferred_results = _tavily_client_search(client, query, max_results, include_domains=domains)
        except Exception as e:
            log("WARNING", f'  Warning: Tavily preferred-domain search failed: {e}')

    preferred_unique = merge_tavily_results(preferred_results, preferred_domains=domains, max_results=max_results)
    if domains and len(preferred_unique) >= max_results:
        return preferred_unique

    general_results: list[dict] = []
    try:
        general_results = _tavily_client_search(client, query, max_results)
    except Exception as e:
        log("WARNING", f'  Warning: Tavily search failed: {e}')

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
            raw_label="translate_tavily_query_output",
        )
        return TavilyQueryOutput.from_json_value(response_obj, max_queries=len(source_queries))
    except Exception as e:
        log("WARNING", f'  Warning: Tavily query translation failed: {e}')
        return TavilyQueryOutput([])


def build_tavily_search_plan(
    transcript: Transcript,
    ctx: TranscriptContext,
    llm: LLMConfig,
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
            raw_label="build_tavily_search_plan",
        )
        agent_output = TavilyQueryOutput.from_json_value(response_obj, max_queries=max_queries)
        source_queries = merge_tavily_queries_with_fallbacks(agent_output.queries, fields, max_queries=max_queries)
        target_output = translate_tavily_query_output(
            source_queries,
            ctx,
            llm,
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
        log("WARNING", f'  Warning: Tavily query agent failed: {e}')
        source_queries = merge_tavily_queries_with_fallbacks([], fields, max_queries=max_queries)
        target_output = translate_tavily_query_output(source_queries, ctx, llm)
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
    retriever: EmbeddingRetriever = None
    force: bool = False

    @staticmethod
    def from_env(env: dict[str, str], retriever=None, force: bool = False) -> "GlossaryBuildOptions":
        return GlossaryBuildOptions(
            tavily_key=env.get("TAVILY_API_KEY", ""),
            tavily_max_results=env_int(env.get("TAVILY_MAX_RESULTS", ""), 20),
            tavily_max_queries=env_int(env.get("TAVILY_MAX_QUERIES", ""), 15),
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
            "topic_hints": unique_non_empty_strings(topic_hints, 24),
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


def build_glossary_finalizer_request_fields(request_fields: dict, sidecar: WebEvidenceSidecar) -> dict:
    fields = {
        key: value
        for key, value in request_fields.items()
        if key not in {"tavily_domain_preferences", "tool_instructions"}
    }
    if sidecar.has_records():
        fields["web_evidence"] = sidecar.to_json_value()
    fields["finalization_instruction"] = (
        "Search is complete or unavailable. Build the final glossary now; do not request more tools."
    )
    return fields


def local_glossary_markdown_from_evidence(request_fields: dict, sidecar: WebEvidenceSidecar) -> str:
    title = str(request_fields.get("title", "") or "").strip()
    source_language = str(request_fields.get("source_language", "") or "").strip()
    target_language = str(request_fields.get("target_language", "") or "").strip()
    heading = "# 术语知识库" + (f" - {title}" if title else "")
    lines = [
        heading,
        "",
        "## 背景",
        "- 远端模型未返回可解析的 glossary 格式；以下为本地根据已获取网页证据生成的保守草稿。",
    ]
    if source_language or target_language:
        lines.append(f"- 语言方向：{source_language or '?'} -> {target_language or '?'}")
    lines.extend(
        [
            "",
            "## 核心术语",
            "| 原文术语 | 推荐译法 | 说明 |",
            "|---|---|---|",
            "| (?) | (?) | 请根据下方网页证据人工补充和确认。 |",
            "",
            "## 态度基调",
            "- 待人工根据视频内容补充。",
            "",
            "## 关键论点",
            "- 待人工根据视频内容补充。",
        ]
    )
    if sidecar.has_records():
        lines.extend(["", "## 网页证据"])
        for record in sidecar.records:
            if not record.query or not record.results:
                continue
            lines.append(f"- Query: {record.query}")
            for entry in record.results[:3]:
                label = entry.title or entry.domain or entry.url
                summary = re.sub(r"\s+", " ", entry.content).strip()[:300]
                if entry.url:
                    lines.append(f"  - {label}: {entry.url}")
                if summary:
                    lines.append(f"    - {summary}")
    return "\n".join(lines).rstrip() + "\n"


def finalize_glossary_from_evidence(
    request_fields: dict,
    sidecar: WebEvidenceSidecar,
    ctx: TranscriptContext,
    llm: LLMConfig,
    options: GlossaryBuildOptions,
) -> GlossaryBuildArtifact:
    request = LLMObjectRequest(build_glossary_finalizer_request_fields(request_fields, sidecar))
    session = ChatSession(
        llm,
        glossary_finalizer_system_prompt(ctx, options.retriever),
        temperature=0.3,
        disable_response_format=True,
    )
    try:
        content, glossary_output = session.ask_validated(
            request.to_json_text(),
            lambda value: GlossaryOutput.from_model_content(value),
            retry_template=CompletionRetryTemplate(
                attempts=3,
                label="Glossary finalizer",
            ),
            retry_feedback=lambda _answer, error, _attempt: (
                "INVALID FORMAT. The previous response could not be parsed: "
                f"{error}\n\n"
                "Do not output <tool_call>, tool_calls, search requests, analysis prose, or markdown outside the allowed wrapper.\n"
                "Return only one of these two formats:\n"
                '1. A JSON object exactly like {"markdown": "..."}\n'
                "2. A tagged markdown block exactly wrapped by <GLOSSARY_MARKDOWN> and </GLOSSARY_MARKDOWN>\n"
                "Use the existing web_evidence in the previous user JSON. Do not ask for more search."
            ),
        )
    except Exception as e:
        log("WARNING", f'Warning: glossary finalizer failed; using local web-evidence draft: {e}')
        return GlossaryBuildArtifact(
            markdown=local_glossary_markdown_from_evidence(request_fields, sidecar),
            web_evidence=sidecar,
        )
    log("DEBUG", f"Glossary finalizer response received: chars={len(content)}")
    return GlossaryBuildArtifact(markdown=glossary_output.markdown, web_evidence=sidecar)


def build_glossary_with_tools(
    transcript: Transcript,
    ctx: TranscriptContext,
    llm: LLMConfig,
    options: GlossaryBuildOptions,
) -> GlossaryBuildArtifact:
    metadata_fields = read_video_metadata_fields(ctx)
    preferences = load_tavily_domain_preferences()
    runtime = GlossaryToolRuntime(
        tavily_key=options.tavily_key,
        metadata_fields=metadata_fields,
        preferences=preferences,
        max_results=options.tavily_max_results,
    )
    request_fields = build_glossary_request_fields(
        transcript,
        ctx,
        GlossaryRequestArgs(
            metadata_fields=metadata_fields,
            retriever=options.retriever,
            tavily_preferences=preferences,
        ),
    )
    request = LLMObjectRequest(request_fields)
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
    evidence_records: list[WebEvidenceRecord] = []

    for _ in range(max_tool_queries + max_format_retries + 2):
        allow_tools = used_tool_queries < max_tool_queries
        kwargs = {
            "tools": tools,
            "tool_choice": "auto" if allow_tools else "none",
        }
        response = session.create(
            retry_template=CompletionRetryTemplate(
                attempts=3,
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
            log("INFO", f'Glossary: malformed tool-call response, retrying ({format_retries}/{max_format_retries})')
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
                    record = build_web_evidence_record(
                        tool_result.get("query", ""),
                        tool_result.get("results", []),
                        topic_hints=json_string_list(tool_result.get("topic_hints", [])),
                        preferred_domains=json_string_list(tool_result.get("preferred_domains", [])),
                        search_stage="tool",
                    )
                    if record.query and record.results:
                        evidence_records.append(record)
                log("INFO", f"Glossary tool result ({tool_name}, {used_tool_queries}/{max_tool_queries}): {tool_result.get('query', '')}")
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
        log("DEBUG", f"Glossary response received: chars={len(content)}")
        sidecar = WebEvidenceSidecar(records=evidence_records)
        if sidecar.has_records():
            return finalize_glossary_from_evidence(request_fields, sidecar, ctx, llm, options)
        try:
            return GlossaryBuildArtifact(
                markdown=GlossaryOutput.from_model_content(content).markdown,
                web_evidence=sidecar,
            )
        except Exception as e:
            log("INFO", f'Glossary: final response was not parseable; using finalizer retry: {e}')
            return finalize_glossary_from_evidence(request_fields, sidecar, ctx, llm, options)

    raise RuntimeError("glossary tool session ended without final answer")


def write_glossary_generation_fallback(ctx: TranscriptContext, options: GlossaryBuildOptions) -> str:
    glossary = write_glossary_file(ctx, ensure_local_metadata_in_glossary("", ctx))
    if glossary:
        log("INFO", f'Glossary fallback: {ctx.glossary}')
    return glossary


def build_tavily_search_evidence(
    transcript: Transcript,
    ctx: TranscriptContext,
    llm: LLMConfig,
    metadata_fields: dict,
    options: GlossaryBuildOptions,
) -> WebEvidenceSidecar:
    if not options.tavily_key or int(options.tavily_max_queries or 0) <= 0:
        return WebEvidenceSidecar()

    records: list[WebEvidenceRecord] = []
    domain_preferences = load_tavily_domain_preferences()
    search_plan = build_tavily_search_plan(
        transcript,
        ctx,
        llm,
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
        domain_hint = f" ({len(preferred_domains)} preferred domains)" if preferred_domains else ""
        log("INFO", f'  Searching: {q[:60]}{domain_hint}')
        results = tavily_search(q, options.tavily_key, per_query_results, preferred_domains=preferred_domains)
        record = build_web_evidence_record(
            q,
            results,
            topic_hints=search_plan.topic_hints,
            preferred_domains=preferred_domains,
            search_stage="fallback",
        )
        if record.query and record.results:
            records.append(record)

    return WebEvidenceSidecar(records=records)


@logged_stage("glossary")
def build_glossary(
    transcript: Transcript,
    ctx: TranscriptContext,
    llm: LLMConfig,
    options: Optional[GlossaryBuildOptions] = None,
) -> str:
    options = options or GlossaryBuildOptions()
    metadata_fields = read_video_metadata_fields(ctx)
    if not options.force and os.path.isfile(ctx.glossary) and os.path.getsize(ctx.glossary) > 0:
        glossary = write_glossary_file(ctx, ensure_local_metadata_in_glossary(_read_text_file(ctx.glossary), ctx))
        if options.tavily_key and int(options.tavily_max_queries or 0) > 0 and not load_web_evidence_sidecar(ctx.web_evidence_json).has_records():
            sidecar = build_tavily_search_evidence(transcript, ctx, llm, metadata_fields, options)
            write_web_evidence_sidecar(ctx, sidecar)
            if sidecar.has_records():
                log("INFO", f'Web evidence: {ctx.web_evidence_json}')
        log("INFO", f'Glossary cache: {ctx.glossary}')
        return glossary

    title = metadata_fields["title"]
    tags = metadata_fields["tags"]
    desc_text = metadata_fields["description"]
    transcript_text = "\n".join(transcript.text_lines())

    if options.use_tool_session():
        log("INFO", f'Glossary: generating with {llm.provider} / {llm.model_name()} (Tavily queries={options.tavily_max_queries})')
        try:
            artifact = build_glossary_with_tools(
                transcript,
                ctx,
                llm,
                options,
            )
            write_web_evidence_sidecar(ctx, artifact.web_evidence)
            glossary = write_glossary_file(ctx, ensure_local_metadata_in_glossary(artifact.markdown, ctx))
            log("INFO", f'Glossary: {ctx.glossary}')
            if artifact.web_evidence.has_records():
                log("INFO", f'Web evidence: {ctx.web_evidence_json}')
            return glossary
        except Exception as e:
            log("WARNING", f'Warning: glossary tool session failed: {e}')
            log("INFO", 'Glossary: falling back to query-agent Tavily search')

    sidecar = build_tavily_search_evidence(transcript, ctx, llm, metadata_fields, options)
    write_web_evidence_sidecar(ctx, sidecar)
    search_text = sidecar.prompt_text(max_chars=4000)

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
    log("INFO", f'Glossary: generating with {llm.provider} / {llm.model_name()}')
    try:
        response_obj = llm_json_once(
            llm,
            glossary_system_prompt(ctx, options.retriever),
            request,
            temperature=0.3,
            raw_label="build_glossary",
            disable_response_format=True,
        )
        glossary_output = GlossaryOutput.from_json_value(response_obj)
        glossary = ensure_local_metadata_in_glossary(glossary_output.markdown, ctx)
    except Exception as e:
        log("WARNING", f'Warning: glossary generation failed: {e}')
        return write_glossary_generation_fallback(ctx, options)

    glossary = write_glossary_file(ctx, glossary)
    log("INFO", f'Glossary: {ctx.glossary}')
    if sidecar.has_records():
        log("INFO", f'Web evidence: {ctx.web_evidence_json}')
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


@dataclass
class LLMBatchRequest:
    items: list[LLMBatchItem]

    def to_json_value(self) -> dict:
        return {"items": [item.to_json_value() for item in self.items]}

    def to_json_text(self) -> str:
        return json.dumps(self.to_json_value(), ensure_ascii=False, indent=2)


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

    @staticmethod
    def from_model_content(content: str) -> "GlossaryOutput":
        try:
            return GlossaryOutput.from_json_content(content)
        except Exception as json_error:
            match = re.search(
                r"<GLOSSARY_MARKDOWN>\s*(.*?)\s*</GLOSSARY_MARKDOWN>",
                str(content or ""),
                flags=re.IGNORECASE | re.DOTALL,
            )
            if not match:
                raise json_error
            markdown = match.group(1).strip()
            if not markdown:
                raise ValueError("glossary tagged markdown is empty")
            return GlossaryOutput(markdown)


@dataclass
class CompletionRetryTemplate:
    attempts: int = 3
    base_delay: float = 0.0
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
        if getattr(self.llm, "request_timeout", None) is not None:
            kwargs["timeout"] = self.llm.request_timeout
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
        retry_feedback=None,
    ):
        template = retry_template or CompletionRetryTemplate(attempts=1)
        self.messages.append({"role": "user", "content": content})
        last_error: Exception | None = None
        for attempt in range(template.normalized_attempts()):
            answer: str | None = None
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
                if retry_feedback is not None and answer is not None:
                    self.messages.append({"role": "assistant", "content": answer})
                    self.messages.append(
                        {
                            "role": "user",
                            "content": str(retry_feedback(answer, e, attempt)),
                        }
                    )
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
        log("WARNING", f'  {template.label} retry {attempt_index + 1}/{template.normalized_attempts()} in {wait:g}s: {error}')
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
            label=raw_label or "LLM JSON",
        ),
    )
    log("DEBUG", f"{raw_label or 'LLM JSON'} response received: chars={len(content)}")
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


# --- Subtitle output ----------------------------------------------------------


SOURCE_FIRST_VERSION = "whisperx-source-first-v1"


_EVENT_PROOFREAD_PROTOCOL = """TARGET-ONLY PROOFREADING (overrides all earlier correction instructions):
Only improve the target translation. The source is READ ONLY, even if a glossary or
retrieved context suggests an ASR error. Do not correct source grammar or terminology.
Do not change event IDs, count, order or times. Preserve the whole sentence meaning.
Return {"items":[{"id":<sentence id>,"${TARGET_LANG_CODE}":<whole revised translation>,
"split_events":[{"id":<existing event id>,"${TARGET_LANG_CODE}":<revised target part>}]}]}.
Return all existing event IDs and nonempty target parts. Preserve their semantic
correspondence; there is no exact concatenation, spacing or punctuation equation
between the whole translation and its subtitle parts. Do not count characters.
Keep unresolved source doubts outside subtitle text. If uncertain, keep the translation.
"""


def source_first_hash(value) -> str:
    return json_sha256(value)


def source_first_model_identity(llm: LLMConfig) -> dict:
    return {"provider": llm.provider, "model": llm.model_name(), "config": llm.cfg()}


def source_first_context(transcript: Transcript, segment: TranscriptSegment, window: int = 1) -> dict:
    position = next(i for i, item in enumerate(transcript.segments) if item is segment)
    return {
        "context_before": [s.source_text() for s in transcript.segments[max(0, position - window):position]],
        "context_after": [s.source_text() for s in transcript.segments[position + 1:position + 1 + window]],
    }


def source_word_boundaries(segment: TranscriptSegment) -> list[dict]:
    """Expose only boundaries backed by a complete, ordered word-to-text mapping."""
    if segment.word_timing.get("status") == "unavailable":
        raise ValueError("word timing explicitly unavailable")
    words = [word for word in segment.words if text_tokens(word.text)]
    expected = text_tokens(segment.text)
    actual: list[str] = []
    word_ends: dict[int, int] = {}
    previous_end = segment.start
    for index, word in enumerate(words):
        if (
            word.start is None or word.end is None
            or not math.isfinite(word.start) or not math.isfinite(word.end)
            or word.start < previous_end - 0.001 or word.end <= word.start
            or word.end > segment.end + 0.001
        ):
            raise ValueError("incomplete, overlapping or invalid word timing")
        actual.extend(text_tokens(word.text))
        word_ends[len(actual)] = index + 1
        previous_end = word.end
    if not expected or expected != actual:
        raise ValueError("words do not cover the complete source text")
    boundaries = [{"id": "b0", "offset": 0, "word_index": 0}]
    for match in re.finditer(r"\s+", segment.text):
        offset = match.end()
        count = len(text_tokens(segment.text[:offset]))
        if (
            0 < count < len(expected) and count in word_ends
            and text_tokens(segment.text[:offset]) + text_tokens(segment.text[offset:]) == expected
            and word_ends[count] != boundaries[-1]["word_index"]
        ):
            boundaries.append({"id": f"b{len(boundaries)}", "offset": offset, "word_index": word_ends[count]})
    boundaries.append({"id": f"b{len(boundaries)}", "offset": len(segment.text), "word_index": len(words)})
    return boundaries


def source_events_from_boundaries(
    segment: TranscriptSegment, boundaries: list[dict], selected: list[str], plan_key: str,
) -> list[SplitEvent]:
    if not isinstance(selected, list) or any(not isinstance(value, str) for value in selected):
        raise ValueError("source split must return boundary IDs")
    by_id = {item["id"]: item for item in boundaries}
    if (
        len(selected) < 2 or len(set(selected)) != len(selected)
        or selected[0] != boundaries[0]["id"] or selected[-1] != boundaries[-1]["id"]
        or any(value not in by_id for value in selected)
    ):
        raise ValueError("missing, duplicate or unknown boundary ID")
    cuts = [by_id[value] for value in selected]
    if any(left["offset"] >= right["offset"] for left, right in zip(cuts, cuts[1:])):
        raise ValueError("boundary IDs are out of order")
    words = [word for word in segment.words if text_tokens(word.text)]
    events = []
    for left, right in zip(cuts, cuts[1:]):
        start, end = left["offset"], right["offset"]
        text = segment.text[start:end].strip()
        if not text or left["word_index"] >= right["word_index"]:
            raise ValueError("empty source event")
        events.append(SplitEvent(
            float(words[left["word_index"]].start), float(words[right["word_index"] - 1].end),
            text, "", source_first_hash([plan_key, start, end])[:24], (start, end),
        ))
    events[0].start = segment.start
    events[-1].end = segment.end
    return events


def source_first_rows(answer: str, expected_ids: list[int]) -> dict[int, dict]:
    payload = require_json_object(_extract_json_value(answer))
    if set(payload) != {"items"} or not isinstance(payload["items"], list):
        raise ValueError('response must contain only an "items" array')
    rows = {}
    for item in payload["items"]:
        if not isinstance(item, dict) or type(item.get("id")) is not int:
            raise ValueError("invalid sentence ID")
        if item["id"] not in expected_ids or item["id"] in rows:
            raise ValueError("unknown or duplicate sentence ID")
        rows[item["id"]] = item
    return rows


def source_first_batch(llm, prompt: str, items: list[LLMBatchItem], validate, admission_guard=None, *, round_log=None) -> tuple[dict, dict]:
    accepted, errors = {}, {}
    pending = list(items)
    for attempt in range(2):
        if not pending:
            break
        if admission_guard:
            admission_guard()
        def parse_response(answer):
            if round_log:
                round_log("response", chars=len(answer))
            return source_first_rows(answer, [item.id for item in pending])

        request_started = False
        try:
            request = LLMBatchRequest(pending).to_json_text()
            session = ChatSession(llm, prompt, temperature=0.2)
            if round_log:
                round_log("request", attempt=attempt + 1, ids=[item.id for item in pending], chars=len(request))
            request_started = True
            _, rows = session.ask_validated(
                request,
                parse_response,
                retry_template=CompletionRetryTemplate(attempts=1, ),
            )
        except Exception as error:
            rows = {}
            errors.update({item.id: str(error) for item in pending})
            if round_log and request_started:
                round_log("error", kind=type(error).__name__)
        except BaseException as error:
            if round_log and request_started:
                round_log("aborted", kind=type(error).__name__)
            raise
        remaining, valid_ids = [], []
        for item in pending:
            try:
                if item.id not in rows:
                    raise ValueError(errors.get(item.id, "missing sentence ID"))
                accepted[item.id] = validate(item.id, rows[item.id])
                valid_ids.append(item.id)
                errors.pop(item.id, None)
            except (TypeError, ValueError, KeyError) as error:
                errors[item.id] = str(error)
                remaining.append(item)
        if round_log and request_started:
            round_log("validated", accepted=valid_ids, failed=[item.id for item in remaining], retry=bool(remaining) and attempt == 0)
        pending = remaining
    if admission_guard:
        admission_guard()
    return accepted, errors


@logged_stage("source_review")
def build_source_review(transcript, ctx, llm, *, force: bool = False, admission_guard=None) -> dict:
    """Publish a text-only adopted draft without changing ASR text or its word axis."""
    glossary_path = os.path.abspath(ctx.glossary)
    glossary = _read_text_file(glossary_path)
    units = [source_review.SourceUnit(
        segment.index, segment.source_text(), segment.start, segment.end, f"whisperx:{segment.index}",
    ) for segment in transcript.segments]
    input_hash = file_sha256(ctx.input_json)
    disk_source = load_transcript(ctx.input_json)
    if [(segment.index, segment.source_text()) for segment in disk_source.segments] != [(unit.id, unit.text) for unit in units]:
        raise ValueError("source input changed before review")
    if file_sha256(ctx.input_json) != input_hash:
        raise ValueError("source input changed while taking review snapshot")
    metadata = read_video_metadata_fields(ctx)
    evidence = load_web_evidence_sidecar(ctx.web_evidence_json).prompt_text(max_chars=4000)
    prompt = (
        _JSON_FORMAT + "\n\n" + source_review.REVIEW_PROTOCOL
        + f"\nSource language key: {ctx.source_lang_code}\n\nFULL GLOSSARY:\n" + glossary
        + "\n\nPROJECT CONTEXT (not acoustic verification):\n"
        + json.dumps({"metadata": metadata, "web_evidence": evidence}, ensure_ascii=False)
    )
    context = {
        "project": ctx.dir, "input": input_hash,
        "generation": transcript.raw.get("_batch_artifact", {}),
        "model": source_first_model_identity(llm), "prompt": prompt,
    }
    identity = source_review.review_input_identity(units, ctx.source_lang_code, glossary, context)
    from pathlib import Path

    pointer = Path(ctx.dir) / f"{ctx.base}.source_review.json"
    if not force:
        cached = source_review.load_review(pointer, identity, glossary, units)
        if cached is not None:
            log("INFO", f'Source review cache: {pointer}')
            return cached
    bounded_llm = copy.copy(llm)
    bounded_llm.request_timeout = 120.0
    bounded_llm.api_max_retries = 0
    accepted = {}
    errors = {}
    by_id = {unit.id: unit for unit in units}
    source_review.validate_units(units)
    batch_size = max(1, min(8, llm.batch_size))
    for start in range(0, len(units), batch_size):
        batch = units[start:start + batch_size]
        request = [make_source_item(unit.id, ctx, unit.text) for unit in batch]
        for item in request:
            segment = next(segment for segment in transcript.segments if segment.index == item.id)
            item.fields.update(source_first_context(transcript, segment))
        valid, failed = source_first_batch(
            bounded_llm, prompt, request,
            lambda item_id, row: source_review.validate_review(row, by_id[item_id], ctx.source_lang_code), admission_guard,
        )
        accepted.update(valid)
        errors.update(failed)
    items = [accepted.get(unit.id) or source_review.fallback_review(
        unit, "source_review_request_failed",
    ) for unit in units]
    if file_sha256(ctx.input_json) != context["input"]:
        raise ValueError("source input changed during review; generation not published")
    if admission_guard:
        admission_guard()
    result = source_review.publish_review(
        pointer, identity, Path(glossary_path), glossary, units, items, ctx.source_lang_code,
        provenance={"provider": llm.provider, "model": llm.model_name(), "prompt_sha256": json_sha256(prompt),
                    "input_file_sha256": input_hash},
    )
    log("INFO", f'Source review: {pointer} (fallback={len(errors)})')
    return result


@logged_stage("source_prepare")
def prepare_mfa_review(source, ctx, env, video_path: str, args, admission_guard=None):
    from pathlib import Path

    from subtitle_translation.mfa import MfaRuntime
    from subtitle_translation.artifacts import text_sha256

    if ctx.source_lang_code != "en":
        raise ValueError("the managed MFA profile currently supports English source text only")
    runtime = MfaRuntime.for_project(Path(_config_root()))
    media = Path(video_path) if video_path else Path(ctx.dir) / f"{ctx.base}.wav"
    if not media.is_file():
        raise ValueError("MFA_ENABLED=1 requires --video or a same-name WAV alongside the input JSON")
    if not env.get("TRANSLATE_PROVIDER", "").strip():
        raise ValueError("TRANSLATE_PROVIDER is required for MFA translation")
    original = copy.deepcopy(source)
    for segment in original.segments:
        segment.proofread_text = ""
    if args.skip_knowledge:
        units = [source_review.SourceUnit(segment.index, segment.text, segment.start, segment.end,
                                         f"whisperx:{segment.index}") for segment in original.segments]
        source_review.validate_units(units)
        items = [source_review.fallback_review(unit, "knowledge_skipped") for unit in units]
        text, ranges = source_review.compose_adopted_text(items, ctx.source_lang_code)
        review = {"language": ctx.source_lang_code, "text": text, "text_sha256": text_sha256(text),
                  "items": items, "source_map": ranges, "generation": "knowledge_skipped", "skipped": True}
    else:
        if admission_guard:
            admission_guard()
        llm = glossary_llm_from_env(env, translate_llm_from_env(env, args.batch_size), batch_size=args.batch_size)
        build_glossary(original, ctx, llm, GlossaryBuildOptions.from_env(env, ))
        if admission_guard:
            admission_guard()
        review_ctx = copy.copy(ctx)
        review_ctx.glossary = os.path.abspath(args.glossary or ctx.glossary)
        review = build_source_review(original, review_ctx, llm, force=args.force, admission_guard=admission_guard)
    if admission_guard:
        admission_guard()
    return review, media, runtime


def source_events_are_valid(segment: TranscriptSegment) -> bool:
    offset = 0
    ids = set()
    previous_end = segment.start
    for event in segment.split_events:
        if not event.id or event.id in ids or event.source_range is None:
            return False
        ids.add(event.id)
        if not isinstance(event.source_range, (list, tuple)) or len(event.source_range) != 2:
            return False
        start, end = event.source_range
        if type(start) is not int or type(end) is not int or start != offset or not start < end <= len(segment.text):
            return False
        if event.en != segment.text[start:end].strip():
            return False
        if segment.segment_timing.get("quality") == "pending":
            if event.start is not None or event.end is not None:
                return False
            offset = end
            continue
        if event.start is None or event.end is None:
            return False
        if (
            not math.isfinite(event.start) or not math.isfinite(event.end)
            or event.start < previous_end - 0.001 or event.end <= event.start
            or event.end > segment.end + 0.001
        ):
            return False
        previous_end, offset = event.end, end
    return bool(segment.split_events) and offset == len(segment.text)


def validate_target_events(row: dict, segment: TranscriptSegment, ctx: TranscriptContext) -> tuple[str, list[str]]:
    if set(row) != {"id", ctx.target_lang_code, "split_events"}:
        raise ValueError("proofreading must return only sentence ID, whole target and target events")

    def target_text(item):
        value = item.get(ctx.target_lang_code)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("proofreading must provide a nonempty target string")
        return value.strip()

    whole = target_text(row)
    events = row["split_events"]
    expected = [event.id for event in segment.split_events]
    if not isinstance(events, list) or len(events) != len(expected):
        raise ValueError("proofreading event count mismatch")
    by_id = {}
    for event in events:
        if not isinstance(event, dict) or set(event) != {"id", ctx.target_lang_code}:
            raise ValueError("proofreading cannot edit source text or time")
        event_id = event["id"]
        if not isinstance(event_id, str) or event_id not in expected or event_id in by_id:
            raise ValueError("unknown or duplicate event ID")
        by_id[event_id] = target_text(event)
    return whole, [by_id[item_id] for item_id in expected]


def _proofread_source_events(segment, ctx):
    if segment.segment_timing.get("text_planned"):
        return [{"id": e.id, ctx.source_lang_code: e.en, "source_range": list(e.source_range)}
                for e in segment.split_events]
    return [{"id": e.id, ctx.source_lang_code: e.en, "start": round(e.start, 3), "end": round(e.end, 3)}
            for e in segment.split_events]


@logged_stage("proofread")
def proofread_target_events(transcript, ctx, llm, system_prompt, retriever=None, *,
                            retrieval_top_k=1, admission_guard=None):
    prompt = system_prompt + "\n\n" + _JSON_FORMAT + "\n\n" + render_prompt_template(_EVENT_PROOFREAD_PROTOCOL, ctx)
    if retriever is not None:
        prompt += "\n\n" + _RETRIEVED_CONTEXT_RULES
    plans, pending, changed = {}, [], False
    for segment in transcript.segments:
        if not source_events_are_valid(segment):
            raise ValueError(f"invalid source event plan #{segment.index}")
        current = {"id": segment.index, ctx.target_lang_code: segment.translation, "split_events": [
            {"id": e.id, ctx.target_lang_code: e.zh} for e in segment.split_events]}
        validate_target_events(current, segment, ctx)
        fields = {"split_events": _proofread_source_events(segment, ctx), **source_first_context(transcript, segment)}
        if retriever is not None:
            query = f"Target translation terminology\n{segment.text}\n{segment.translation}"
            fields["retrieved_context"] = retriever.retrieve_texts([query], top_k=retrieval_top_k)[0]
        base_key = source_first_hash({"version": SOURCE_FIRST_VERSION, "plan": segment.event_plan_key,
            "source": segment.text, "input": copy.deepcopy(fields), "prompt": prompt,
            "model": source_first_model_identity(llm), "languages": [ctx.source_lang_code, ctx.target_lang_code]})
        if segment.proofread_key == source_first_hash([base_key, current]):
            continue
        fields[ctx.target_lang_code] = segment.translation
        for event, item in zip(segment.split_events, fields["split_events"]):
            item[ctx.target_lang_code] = event.zh
        plans[segment.index] = (segment, base_key)
        pending.append(make_language_item(segment.index, ctx, source=segment.text, extra=fields))
    log("INFO", f'Target proofread: {len(pending)} pending sentence(s)')
    for start in range(0, len(pending), max(1, llm.batch_size)):
        accepted, errors = source_first_batch(llm, prompt, pending[start:start + max(1, llm.batch_size)],
            lambda item_id, row: validate_target_events(row, plans[item_id][0], ctx), admission_guard)
        for item_id, (whole, parts) in accepted.items():
            segment, base_key = plans[item_id]
            segment.translation = whole
            for event, part in zip(segment.split_events, parts):
                event.zh = part
            current = {"id": segment.index, ctx.target_lang_code: whole, "split_events": [
                {"id": e.id, ctx.target_lang_code: e.zh} for e in segment.split_events]}
            segment.proofread_key = source_first_hash([base_key, current])
            changed = True
        if errors:
            ids = ", ".join(f"#{i}" for i in errors)
            log("WARNING", f'Warning: target proofreading kept existing translation for {ids}')
    return changed


def srt_time(seconds: float) -> str:
    milliseconds = int(round(max(0.0, seconds) * 1000))
    h, remainder = divmod(milliseconds, 3600000)
    m, remainder = divmod(remainder, 60000)
    s, ms = divmod(remainder, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


@logged_stage("export")
def write_srt_events(output_path: str, events: list[SplitEvent], field_name: str) -> None:
    with open(output_path, "w", encoding="utf-8") as f:
        for i, event in enumerate(events, 1):
            text = getattr(event, field_name)
            f.write(f"{i}\n")
            f.write(f"{srt_time(event.start)} --> {srt_time(event.end)}\n")
            f.write(f"{ass_escape(text)}\n\n")


def ass_time(seconds: float) -> str:
    centiseconds = int(round(max(0.0, seconds) * 100))
    h, remainder = divmod(centiseconds, 360000)
    m, remainder = divmod(remainder, 6000)
    s, cs = divmod(remainder, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def load_template(template_path: str) -> tuple[str, str]:
    with open(template_path, "r", encoding="utf-8") as f:
        content = f.read()
    events_pos = content.find("\n[Events]\n")
    if events_pos == -1:
        log("ERROR", 'Error: template.ass missing [Events] section.')
        sys.exit(1)
    header = content[: events_pos + 1]
    events_section = content[events_pos + 1 :]
    match = re.search(r"Format:.*", events_section)
    if not match:
        log("ERROR", 'Error: template.ass [Events] section missing Format line.')
        sys.exit(1)
    return header, "\n[Events]\n" + match.group(0) + "\n"


def ass_escape(text: str) -> str:
    return " ".join(text.replace("\\N", " ").split())


def wrap_cjk(text: str, max_chars: int = 25) -> str:
    return ass_escape(text)


def bilingual_ass_text(source_text: str, target_text: str) -> str:
    return f"{ass_escape(target_text)}\\N{{\\rbi-en}}{ass_escape(source_text)}"


def all_events(transcript: Transcript) -> list[SplitEvent]:
    events: list[SplitEvent] = []
    for seg in transcript.segments:
        if seg.split_events:
            events.extend(seg.split_events)
        else:
            events.append(SplitEvent(seg.start, seg.end, seg.source_text(), seg.translation))
    if any(event.start is None or event.end is None for event in events):
        raise ValueError("cannot export text events before timing is complete")
    return events


@dataclass(frozen=True)
class AssTrack:
    field_name: str
    style: str
    wrap_text: bool = False


ASS_BILINGUAL_STYLE = "bi-zh"


ASS_OUTPUT_TRACKS: dict[AssOutputMode, tuple[AssTrack, ...]] = {
    AssOutputMode.SOURCE: (AssTrack("en", "bi-en"),),
    AssOutputMode.TARGET: (AssTrack("zh", "bi-zh", wrap_text=True),),
}


@logged_stage("export")
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
        if output_mode == AssOutputMode.BILINGUAL:
            for event in events:
                f.write(
                    f"Dialogue: 0,{ass_time(event.start)},{ass_time(event.end)},"
                    f"{ASS_BILINGUAL_STYLE},,0,0,0,,{bilingual_ass_text(event.en, event.zh)}\n"
                )
            return
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
{"title": "<translated title>", "description": "<translated description>", "tags": ["<translated tag>", "..."], "chapters": [{"id": 1, "title": "<translated chapter title>"}]}

Rules:
- Preserve URLs, email addresses, handles, and paragraph structure.
- Translate each tag naturally and keep the tag list order.
- Translate chapter titles using the same glossary and context; preserve every chapter id exactly once.
- Return only id/title for each chapter. Never generate or change chapter timestamps.
- If input chapters are absent, return an empty chapters array.
- If chapters are supplied, put their translated list only in chapters; omit a duplicated chapter list from description while preserving unrelated prose.
- Do not add explanations."""


@logged_stage("metadata")
def translate_description(
    ctx: TranscriptContext,
    llm: LLMConfig,
    retriever: EmbeddingRetriever | None = None,
    glossary_path: str = "",
) -> str:
    title, webpage_url, tags = read_metadata(ctx)
    metadata_header = read_metadata_header(ctx)
    desc_text = _read_text_file(ctx.desc) if os.path.isfile(ctx.desc) else ""
    try:
        chapters = read_original_chapters(ctx.dir, ctx.base)
    except (OSError, ValueError) as error:
        log("WARNING", f'Warning: original chapter extraction failed: {error}')
        chapters = []
    if not desc_text.strip() and not title and not chapters:
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
    if chapters:
        request_fields["chapters"] = [chapter.to_request() for chapter in chapters]
    if retriever is not None:
        query = "\n".join(
            part
            for part in [
                title,
                webpage_url,
                desc_text,
                " ".join(tags),
                "\n".join(chapter.title for chapter in chapters),
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
        glossary_context = load_glossary_prompt_context(glossary_path or ctx.glossary, retriever=retriever)
        if glossary_context:
            system_prompt += glossary_context
        if retriever is not None:
            system_prompt += "\n\n" + _RETRIEVED_CONTEXT_RULES
        response_obj = llm_json_once(
            llm,
            system_prompt + "\n\n" + _JSON_FORMAT + "\n\n" + _JSON_OBJECT_FORMAT,
            request,
            temperature=0.3,
            raw_label="translate_description",
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
        chapter_output = translated_chapters(chapters, response_obj.get("chapters"))
    except Exception as e:
        log("WARNING", f'Warning: description translation failed: {e}')
        return ctx.target_desc

    with open(ctx.target_desc, "w", encoding="utf-8") as f:
        if translated_title:
            f.write(f"{translated_title}\n\n")
        f.write(metadata_header)
        if translated_desc:
            f.write(translated_desc)
            f.write("\n")
        if chapter_output:
            if translated_desc:
                f.write("\n")
            f.write("章节：\n")
            for chapter in chapter_output:
                f.write(f"{chapter.timecode} {chapter.title}\n")
        if translated_tags:
            if translated_desc or chapter_output:
                f.write("\n")
            f.write(f"标签：{', '.join(translated_tags)}\n")
    log("INFO", f'Description: {ctx.target_desc}')
    return ctx.target_desc


# --- CLI ----------------------------------------------------------------------


def infer_video_path(ctx: TranscriptContext) -> str:
    for ext in (".webm", ".mkv", ".mp4", ".mov", ".m4v"):
        candidate = os.path.join(ctx.dir, ctx.base + ext)
        if os.path.isfile(candidate):
            return candidate
    return ""


def main(argv: Sequence[str] | None = None, *, admission_guard=None, stage_overrides=None) -> int:
    script_dir = _config_root()
    env = load_env(script_dir)
    if stage_overrides:
        env = {**env, **stage_overrides}

    parser = argparse.ArgumentParser(
        prog="subtitle-translation translate",
        description="Translate WhisperX JSON to proofread/target-language/bilingual ASS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  subtitle-translation translate video.json --video video.webm
  subtitle-translation translate video.json --source-lang en --target-lang zh
  subtitle-translation translate video.json -o video.en-zh.ass
  subtitle-translation translate video.json --only-scenes --video video.webm
""",
    )
    parser.add_argument("json", help="WhisperX .json transcript path")
    parser.add_argument("--video", help="Media path for scene detection and optional MFA audio extraction")
    parser.add_argument("-t", "--template", help="template.ass path")
    parser.add_argument("-o", "--output", help="Output bilingual .ass path")
    parser.add_argument("--source-lang", "--lang", help="Source language name/tag for this project")
    parser.add_argument("--target-lang", help="Target language name/tag for prompts and ISO 639 output suffix (default: zh)")
    parser.add_argument("--only-output-path", dest="print_output_path", action="store_true", help="Print computed bilingual ASS path and exit")
    parser.add_argument("--only-scenes", action="store_true")
    parser.add_argument("--only-glossary", action="store_true")
    parser.add_argument("--beautified-json", help=argparse.SUPPRESS)
    parser.add_argument("--glossary", metavar="PATH")
    args = parser.parse_args(argv)
    from subtitle_translation.config import ProjectConfig, resolve_mfa_enabled
    try:
        settings = ProjectConfig(Path(script_dir), env)
        validate_log_level(env.get("LOG_LEVEL", "INFO"))
        args.mfa = resolve_mfa_enabled(None, env)
        args.batch_size = int(env.get("TRANSLATE_BATCH_SIZE", "50") or "50")
        args.split_context_window = int(env.get("TRANSLATE_CONTEXT_WINDOW", "1") or "1")
        args.scene_threshold = float(env.get("SCENE_THRESHOLD", "0.15") or "0.15")
        args.min_scene_interval_frames = int(env.get("SCENE_MIN_INTERVAL_FRAMES", "2") or "2")
        if args.batch_size < 1:
            raise ValueError("TRANSLATE_BATCH_SIZE must be positive")
        if args.split_context_window < 0:
            raise ValueError("TRANSLATE_CONTEXT_WINDOW must be nonnegative")
        if not math.isfinite(args.scene_threshold) or not 0 <= args.scene_threshold <= 1:
            raise ValueError("SCENE_THRESHOLD must be finite and between 0 and 1")
        if args.min_scene_interval_frames < 0:
            raise ValueError("SCENE_MIN_INTERVAL_FRAMES must be nonnegative")
        args.skip_scene_detection = settings.flag("PIPELINE_SKIP_SCENE_DETECTION") and not args.only_scenes
        args.skip_knowledge = settings.flag("PIPELINE_SKIP_KNOWLEDGE") and not args.only_glossary
        args.no_split = not settings.flag("SPLIT_ENABLED", True)
        args.proofread = settings.flag("PROOFREAD", True)
        args.no_proofread = False
        args.reuse_glossary = settings.flag("GLOSSARY_REUSE")
        args.force = settings.flag("FORCE_REBUILD")
    except ValueError as error:
        parser.error(str(error))
    if args.print_output_path or not os.path.isfile(args.json):
        return _run_translate_command(args, env, script_dir, admission_guard)
    log_directory = Path(args.video or args.json).resolve().parent
    try:
        with project_run(log_directory, env.get("LOG_LEVEL", "INFO")):
            code = _run_translate_command(args, env, script_dir, admission_guard)
            if code:
                log("ERROR", f"Translation failed with exit code {code}")
            return code
    except OSError as error:
        log("ERROR", f"Project log or file operation failed: {type(error).__name__}")
        return 1


def _run_translate_command(args, env, script_dir, admission_guard):
    admission_kwargs = {"admission_guard": admission_guard} if admission_guard else {}

    if not os.path.isfile(args.json):
        log("ERROR", f'Error: JSON file not found: {args.json}')
        sys.exit(1)

    source = load_transcript(args.json)
    source_lang = args.source_lang or env.get("SOURCE_LANG", "") or source.language or "source"
    target_lang = args.target_lang or env.get("TARGET_LANG", "") or "zh"
    ctx = TranscriptContext.from_json(
        args.json,
        args.output or "",
        source_lang,
        target_lang,
        args.beautified_json or "",
    )
    embedding_config = EmbeddingConfig.from_env(env, ctx)
    embedding_active = embedding_config.enabled and embedding_enabled_for_stage(args.only_scenes, args.only_glossary)
    if args.print_output_path:
        print(os.path.abspath(ctx.bilingual_ass))
        print(f"OUTPUT_ASS={os.path.abspath(ctx.bilingual_ass)}")
        return
    video_path = args.video or infer_video_path(ctx)
    if args.only_scenes and (not video_path or not os.path.isfile(video_path)):
        log("ERROR", 'Error: --only-scenes requires an existing video')
        return 2
    template_path = args.template or os.path.join(script_dir, "template.ass")
    log("INFO", f'JSON:     {os.path.abspath(args.json)}')
    log("INFO", f'Source:   {ctx.source_lang}')
    log("INFO", f'Target:   {ctx.target_lang}')
    if video_path:
        log("INFO", f'Video:    {video_path}')
    if embedding_active:
        log("INFO", f'Embedding: {embedding_config.provider} / {embedding_config.model}')
        log("INFO", f'Embedding DB: {embedding_config.chroma_dir}')

    if embedding_active:
        if embedding_config.store != "chroma":
            log("ERROR", f'Error: unsupported EMBEDDING_STORE={embedding_config.store}; only chroma is available.')
            sys.exit(1)

    scene_options = SceneDetectionOptions(
        scene_threshold=args.scene_threshold,
        min_scene_interval_frames=args.min_scene_interval_frames,
    )
    knowledge_done = args.mfa and not args.only_scenes and not args.only_glossary
    text_checkpoint = ctx.beautified_json
    timing_marker = timing_key = None
    if knowledge_done:
        try:
            mfa_input_sha256 = file_sha256(args.json)
            if admission_guard:
                admission_guard()
            from subtitle_translation import windowed_mfa
            review, mfa_media, mfa_runtime = prepare_mfa_review(source, ctx, env, video_path, args, admission_guard)
            transcript, text_checkpoint = windowed_mfa.load_text_checkpoint(source, review, ctx, args.force)
        except (OSError, ValueError) as error:
            log("ERROR", f'Error: MFA source preparation failed: {error}')
            return 1
    source_identity = source_first_hash({
        "version": SOURCE_FIRST_VERSION, "source": source.to_json(),
        "timeline_policy": "asr-direct-no-beautify-v2",
    })
    if admission_guard:
        admission_guard()
    if not knowledge_done:
        transcript = load_or_create_beautified(
            ctx, source, video_path, scene_options, args.skip_scene_detection, args.force, source_identity=source_identity,
        )
    for segment in transcript.segments:
        segment.proofread_text = ""
        if args.force:
            segment.event_plan_key = ""
            segment.translation_key = segment.proofread_key = ""
    retriever = None
    if embedding_active:
        retriever = refresh_embedding_retriever(
            transcript,
            embedding_config,
            env,
            ctx,
            fatal=True,
        )

    if args.only_scenes:
        print(f"OUTPUT_SCENES={os.path.abspath(ctx.scenes_json)}")
        print(f"OUTPUT_SCENECHANGE={os.path.abspath(ctx.scenechange_txt)}")
        return

    llm = None
    if needs_translate_llm(args):
        provider = env.get("TRANSLATE_PROVIDER", "").strip()
        if not provider:
            log("ERROR", f"Error: TRANSLATE_PROVIDER not set in .env. Available: {', '.join(load_providers())}")
            sys.exit(1)
        llm = translate_llm_from_env(env, args.batch_size)

    if not knowledge_done and not args.skip_knowledge and not required_glossary_provider(env):
        log("ERROR", f"Error: GLOSSARY_PROVIDER or TRANSLATE_PROVIDER not set in .env. Available: {', '.join(load_providers())}")
        sys.exit(1)

    if not knowledge_done and not args.skip_knowledge:
        glossary_llm = glossary_llm_from_env(env, llm, batch_size=args.batch_size)
        build_glossary(
            transcript,
            ctx,
            glossary_llm,
            GlossaryBuildOptions.from_env(
                env,
                retriever=retriever,
                force=args.only_glossary and not args.reuse_glossary,
            ),
        )
        if embedding_active:
            updated_retriever = refresh_embedding_retriever(
                transcript,
                embedding_config,
                env,
                ctx,
                warning_label="glossary index update failed",
            )
            retriever = updated_retriever or retriever

    if args.only_glossary:
        if not args.skip_knowledge and not args.reuse_glossary:
            try:
                build_source_review(
                    source, ctx, glossary_llm, force=args.only_glossary and not args.reuse_glossary,
                    )
            except (OSError, ValueError) as error:
                log("ERROR", f'Error: source review publication failed: {error}')
                return 1
            print(f"OUTPUT_SOURCE_REVIEW={os.path.join(ctx.dir, ctx.base + '.source_review.json')}")
        print(f"OUTPUT_GLOSSARY={os.path.abspath(ctx.glossary)}")
        return

    if llm is None:
        log("ERROR", f"Error: TRANSLATE_PROVIDER not set in .env. Available: {', '.join(load_providers())}")
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

    try:
        if admission_guard:
            admission_guard()
        from subtitle_translation.bilingual_translation import translate_events
        changed = translate_events(
            transcript, ctx, llm, system_prompt, retriever,
            split_enabled=not args.no_split, context_window=max(0, args.split_context_window),
            checkpoint=lambda: save_transcript(transcript, str(text_checkpoint)),
            **admission_kwargs,
        )
    except ValueError as error:
        log("ERROR", f'Error: {error}')
        return 1
    if knowledge_done:
        try:
            from subtitle_translation.config import ProjectConfig
            if admission_guard:
                admission_guard()
            save_transcript(transcript, str(text_checkpoint))
            transcript, timing_marker, timing_key = windowed_mfa.finish_timing(
                transcript, review, mfa_media, mfa_runtime, ProjectConfig.load(), ctx,
                video_path, scene_options, args.skip_scene_detection, args.force, admission_guard,
            )
            changed = True
        except (OSError, ValueError) as error:
            log("ERROR", f'Error: windowed MFA failed: {error}')
            return 1
    if embedding_active:
        updated_retriever = refresh_embedding_retriever(
            transcript,
            embedding_config,
            env,
            ctx,
            warning_label="translation memory index update failed",
        )
        retriever = updated_retriever or retriever
    if args.proofread and not args.no_proofread and env.get("PROOFREAD", "1") != "0":
        proofread_llm = proofread_llm_from_env(env, llm, args.batch_size)
        changed = proofread_target_events(
            transcript,
            ctx,
            proofread_llm,
            proofread_prompt,
            retriever,
            retrieval_top_k=proofread_retrieval_top_k_from_env(env),
            **admission_kwargs,
        ) or changed
    if changed:
        if knowledge_done and file_sha256(args.json) != mfa_input_sha256:
            raise ValueError("source JSON changed during windowed translation")
        if admission_guard:
            admission_guard()
        save_transcript(transcript, ctx.beautified_json)
        if timing_marker is not None:
            if admission_guard:
                admission_guard()
            atomic_write_json(timing_marker, {"key": timing_key, "sha256": file_sha256(ctx.beautified_json)})

    if admission_guard:
        admission_guard()
    if not os.path.isfile(template_path):
        log("ERROR", f'Error: template.ass not found: {template_path}')
        sys.exit(1)

    events = all_events(transcript)
    write_srt_events(ctx.split_source_srt, events, "en")
    write_srt_events(ctx.split_target_srt, events, "zh")
    write_ass(ctx.proofread_ass, template_path, ctx.base, events, AssOutputMode.SOURCE)
    write_ass(ctx.target_ass, template_path, ctx.base, events, AssOutputMode.TARGET)
    write_ass(ctx.bilingual_ass, template_path, ctx.base, events, AssOutputMode.BILINGUAL)

    if os.path.isfile(ctx.desc) or original_chapter_media(ctx.dir, ctx.base) is not None:
        if admission_guard:
            admission_guard()
        translate_description(ctx, llm, retriever=retriever, glossary_path=glossary_path)
    log("INFO", f'SRT:      {ctx.split_source_srt}')
    log("INFO", f'          {ctx.split_target_srt}')
    log("INFO", f'ASS:      {ctx.proofread_ass}')
    log("INFO", f'          {ctx.target_ass}')
    log("INFO", f'          {ctx.bilingual_ass}')
    log("INFO", f'Events:   {len(events)}')
    print(f"OUTPUT_ASS={os.path.abspath(ctx.bilingual_ass)}")


if __name__ == "__main__":
    raise SystemExit(main())
