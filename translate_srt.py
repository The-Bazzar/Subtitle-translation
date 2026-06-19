#!/usr/bin/env python3
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
from typing import Optional


# --- Data model ---------------------------------------------------------------


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
        )


@dataclass
class LLMConfig:
    provider: str
    model: str = ""
    proofread_provider: str = ""
    proofread_model: str = ""
    api_key: Optional[str] = None
    batch_size: int = 50

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
        from openai import OpenAI

        provider_cfg = self.cfg()
        return OpenAI(
            base_url=provider_cfg["url"],
            api_key=self.resolve_key(),
            default_headers=provider_cfg.get("extra_headers", {}),
        )


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


# --- Providers/env/prompts ----------------------------------------------------


def safe_lang_suffix(value: str, fallback: str) -> str:
    suffix = re.sub(r"[^A-Za-z0-9_-]+", "-", (value or "").strip().lower()).strip("-")
    return suffix or fallback


def iso_639_suffix(value: str, fallback: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return fallback

    try:
        import langcodes

        for candidate in (raw, raw.replace("_", "-")):
            try:
                standardized = langcodes.standardize_tag(candidate)
                language = langcodes.Language.get(standardized)
                if language.is_valid() and language.language and language.language != "und":
                    return safe_lang_suffix(language.language, fallback)
            except Exception:
                pass
        try:
            import language_data  # noqa: F401

            language = langcodes.find(raw)
            if language.language and language.language != "und":
                return safe_lang_suffix(language.language, fallback)
        except Exception:
            pass
    except ImportError:
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
        "${SOURCE_LANG}": source_lang,
        "${TARGET_LANG}": target_lang,
        "${SOURCE_LANG_CODE}": source_lang_code or iso_639_suffix(source_lang, "source"),
        "${TARGET_LANG_CODE}": target_lang_code or iso_639_suffix(target_lang, "target"),
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


_BUILTIN_PROVIDERS = {
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
        "url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-v4-pro",
        "env_key": "DEEPSEEK_API_KEY",
        "auth_header": "Bearer {api_key}",
        "extra_headers": {},
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
- Translate each numbered transcript segment to natural, fluent ${TARGET_LANG} subtitles.
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

Do not merge, split, reorder, add, or remove events. Timing has already been aligned and must not be changed. Return exactly N pairs."""

_SPLIT_PROMPT_FALLBACK = r"""Style preference:
- Split only at natural pause points such as commas, clause boundaries, conjunctions, and breath groups.
- Prefer fewer, more coherent subtitle events over many tiny fragments.
- Keep each split event readable as a complete thought."""

_SPLIT_FORMAT = """MANDATORY OUTPUT FORMAT:
You must output machine-parseable JSON only. No Markdown. No explanation.

ABSOLUTE JSON RULES:
1. The first character must be `[` and the last character must be `]`.
2. Every key and every string value must use double quotes `"`.
3. Never use single quotes `'` for keys, strings, arrays, or objects.
4. Apostrophes inside source-language words are ordinary characters: write `"don't"` and `"I've"`, not `'don't'`.
5. Escape literal double quotes as `\"` and literal backslashes as `\\`.
6. No trailing commas.
7. Return one object per input id. Preserve the exact input id. Do not renumber.
8. Each object must have exactly these legacy keys: "id", "en", "zh".
9. "en" and "zh" must be arrays with the same length.
10. Split by adding multiple strings inside "en" and "zh".
11. Preserve every source-language word in order. Do not rewrite.

BAD, INVALID, WILL BE REJECTED:
[{'id': 12, 'en': ['wrong quotes'], 'zh': ['错误引号']}]

GOOD:
[
  {"id": 12, "en": ["Dystopian cyberpunk worlds filled with angels,", "biblical imagery, and esoteric ideas."], "zh": ["反乌托邦赛博朋克世界 充满天使", "圣经意象和秘传思想"]},
  {"id": 13, "en": ["Short sentence."], "zh": ["短句"]}
]"""

_KNOWLEDGE_PROMPT = """You are a terminology expert. Analyze the ${SOURCE_LANG} transcript and metadata to build a glossary for ${TARGET_LANG} subtitle translation.

Output format:
# 术语知识库 — <title>

## 背景
<2-3 sentences summarizing the video topic>

## 核心术语
| 原文术语 | ${TARGET_LANG} 推荐译法 | 说明 |
|------|---------|------|
| term | 标准译法 | why this translation fits |

## 态度基调
- <tone observation>

## 关键论点
- <core argument>

Rules:
- Only include terms that actually appear in the transcript.
- Search results can verify standard ${TARGET_LANG} translations.
- If uncertain, mark with (?).
- Keep under 100 lines."""

_TRANSLATE_FORMAT = "\n\nRespond with numbered lines only:\n[1] translation\n[2] translation"
_PROOFREAD_FORMAT = """

Return ONLY a valid JSON array. Do not wrap it in Markdown.
The array length must equal the input count. Each item must have exactly these keys:
[
  {"id": 1, "en": "corrected source text", "zh": "corrected target translation"},
  {"id": 2, "en": "corrected source text", "zh": "corrected target translation"}
]

Do not output SRC: or TGT: labels inside values. Do not use separators like |||.
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
                _providers_cache = loaded
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
        beautify_transcript_timeline(transcript, video_path, options, quiet)

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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        fps_str = result.stdout.strip()
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
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    times = []
    for line in result.stderr.splitlines():
        m = re.search(r"pts_time:([0-9.]+)", line)
        if not m:
            continue
        t = float(m.group(1))
        if not times or t - times[-1] >= min_interval_sec:
            times.append(t)
    return times


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
) -> None:
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


def tavily_search(query: str, api_key: str, max_results: int = 5) -> list[dict]:
    import urllib.request

    body = json.dumps(
        {
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "api_key": api_key,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.tavily.com/search",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
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

    prompt = f"""Title: {title}

Transcript excerpt:
{transcript_text[:8000]}

Description:
{desc_text[:1000]}

Tags: {', '.join(tags[:20])}

Search results:
{search_text[:4000] if search_text else '(no web search)'}"""

    if not quiet:
        print(f"Glossary: generating with {llm.provider}", file=sys.stderr)
    try:
        resp = llm._client().chat.completions.create(
            model=llm.model_name(),
            messages=[
                {"role": "system", "content": render_prompt_template(_KNOWLEDGE_PROMPT, ctx)},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=4096,
        )
        glossary = resp.choices[0].message.content
    except Exception as e:
        print(f"Warning: glossary generation failed: {e}", file=sys.stderr)
        return ""

    with open(ctx.glossary, "w", encoding="utf-8") as f:
        f.write(glossary)
        f.write("\n")
    if not quiet:
        print(f"Glossary: {ctx.glossary}", file=sys.stderr)
    return glossary


# --- LLM stages ---------------------------------------------------------------


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
        resp = self.llm._client().chat.completions.create(
            model=self.llm.model_name(),
            messages=self.messages,
            temperature=self.temperature,
            max_tokens=max_tokens,
        )
        answer = resp.choices[0].message.content
        self.messages.append({"role": "assistant", "content": answer})
        return answer


def _parse_numbered_response(content: str, expected_count: int) -> list[str]:
    pattern = re.compile(r"\[(\d+)\]\s*(.*?)(?=\n\s*\[\d+\]|\Z)", re.DOTALL)
    matches = pattern.findall(content)
    if matches:
        result = {}
        for num, text in matches:
            result[int(num)] = text.strip()
        return [result.get(i, "") for i in range(1, expected_count + 1)]

    lines = [l.strip() for l in content.splitlines() if l.strip()]
    parsed = []
    for line in lines:
        m = re.match(r"^(?:\[?\d+\]?[:.)]?\s*)?(.+)$", line)
        if m:
            parsed.append(m.group(1).strip())
    while len(parsed) < expected_count:
        parsed.append("")
    return parsed[:expected_count]


def _extract_json_array(content: str) -> Optional[list]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
        return data if isinstance(data, list) else None
    except json.JSONDecodeError:
        pass

    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
        return data if isinstance(data, list) else None
    except json.JSONDecodeError:
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
    content: str,
    expected_count: int,
    fallback_pairs: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    data = _extract_json_array(content)
    if data is not None:
        by_id: dict[int, tuple[str, str]] = {}
        ordered: list[tuple[str, str]] = []
        for pos, item in enumerate(data, 1):
            if not isinstance(item, dict):
                continue
            en = _strip_speaker_labels(str(item.get("en", "")))
            zh = _strip_speaker_labels(str(item.get("zh", "")))
            pair = (en, zh)
            item_id = item.get("id", pos)
            try:
                by_id[int(item_id)] = pair
            except (TypeError, ValueError):
                ordered.append(pair)
        result = []
        for i in range(1, expected_count + 1):
            if i in by_id:
                result.append(by_id[i])
            elif i - 1 < len(ordered):
                result.append(ordered[i - 1])
            else:
                result.append(fallback_pairs[i - 1])
        return result

    numbered = _parse_numbered_response(content, expected_count)
    result = []
    for i, text in enumerate(numbered):
        if "|||" in text:
            en, zh = text.split("|||", 1)
            result.append((_strip_speaker_labels(en), _strip_speaker_labels(zh)))
        else:
            result.append(fallback_pairs[i])
    return result


def llm_numbered_batch(
    texts: list[str],
    session: ChatSession,
    user_intro: str,
    quiet: bool,
    retries: int = 3,
) -> list[str]:
    prompt = "\n".join(f"[{i}] {t}" for i, t in enumerate(texts, 1))
    last_error = ""
    for attempt in range(retries):
        try:
            content = session.ask(
                f"{user_intro}\n\n{prompt}",
                max_tokens=max(4096, len(texts) * 220),
            )
            return _parse_numbered_response(content, len(texts))
        except Exception as e:
            last_error = str(e)
            if attempt < retries - 1:
                wait = (attempt + 1) * 3
                if not quiet:
                    print(f"  Retry {attempt + 1}/{retries} in {wait}s...", file=sys.stderr)
                time.sleep(wait)
    print(f"Error: LLM batch failed after {retries} attempts: {last_error}", file=sys.stderr)
    return ["" for _ in texts]


def translate_segments(
    transcript: Transcript,
    llm: LLMConfig,
    system_prompt: str,
    quiet: bool,
) -> bool:
    pending = [s for s in transcript.segments if not s.translation]
    if not pending:
        if not quiet:
            print("Translate: cached", file=sys.stderr)
        return False

    if not quiet:
        print(f"Translator: {llm.provider} / {llm.model_name()}", file=sys.stderr)
        print(f"Total segments: {len(pending)}", file=sys.stderr)

    session = ChatSession(llm, system_prompt + _TRANSLATE_FORMAT, temperature=0.3)
    for start in range(0, len(pending), llm.batch_size):
        batch = pending[start : start + llm.batch_size]
        if not quiet:
            print(
                f"  Batch {start // llm.batch_size + 1}/{math.ceil(len(pending) / llm.batch_size)}: "
                f"translating {start + 1}-{start + len(batch)}",
                file=sys.stderr,
            )
        translations = llm_numbered_batch(
            [s.en_text() for s in batch],
            session,
            f"Translate these {len(batch)} complete transcript segments to the target language. Respond with exactly {len(batch)} numbered lines.",
            quiet,
        )
        for seg, zh in zip(batch, translations):
            seg.translation = zh.strip()
            seg.split_events = []
    return True


def proofread_segments(
    transcript: Transcript,
    llm: LLMConfig,
    system_prompt: str,
    quiet: bool,
) -> bool:
    pending = [s for s in transcript.segments if s.translation and not s.proofread_text]
    if not pending:
        if not quiet:
            print("Proofread: cached", file=sys.stderr)
        return False

    pr_llm = LLMConfig(
        provider=llm.pr_provider(),
        model=llm.pr_model(),
        api_key=llm.api_key if llm.pr_provider() == llm.provider else None,
        batch_size=max(15, llm.batch_size // 2),
    )
    if not quiet:
        print(f"Proofreader: {pr_llm.provider} / {pr_llm.model_name()}", file=sys.stderr)
        print(f"Total segments: {len(pending)}", file=sys.stderr)

    session = ChatSession(pr_llm, system_prompt + _PROOFREAD_FORMAT, temperature=0.2)
    for start in range(0, len(pending), pr_llm.batch_size):
        batch = pending[start : start + pr_llm.batch_size]
        pairs = [f"SRC: {s.en_text()}\nTGT: {s.translation}" for s in batch]
        if not quiet:
            print(
                f"  Batch {start // pr_llm.batch_size + 1}/{math.ceil(len(pending) / pr_llm.batch_size)}: "
                f"proofreading {start + 1}-{start + len(batch)}",
                file=sys.stderr,
            )
        prompt = "\n\n".join(f"[{i}] {pair}" for i, pair in enumerate(pairs, 1))
        try:
            raw_result = session.ask(
                (
                    f"Proofread these {len(batch)} bilingual pairs. "
                    f"Return a JSON array with exactly {len(batch)} objects.\n\n{prompt}"
                ),
                max_tokens=max(4096, len(batch) * 260),
            )
        except Exception as e:
            print(f"Warning: proofread batch failed: {e}", file=sys.stderr)
            raw_result = ""

        fallback_pairs = [(s.en_text(), s.translation) for s in batch]
        parsed_pairs = parse_proofread_response(raw_result, len(batch), fallback_pairs)
        for seg, (en, zh) in zip(batch, parsed_pairs):
            seg.proofread_text = en.strip() or seg.text
            seg.translation = zh.strip() or seg.translation
    return True


def proofread_split_events(
    transcript: Transcript,
    llm: LLMConfig,
    system_prompt: str,
    quiet: bool,
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
        batch_size=max(15, llm.batch_size // 2),
    )
    if not quiet:
        print(f"Proofreader: {pr_llm.provider} / {pr_llm.model_name()}", file=sys.stderr)
        print(f"Total split events: {len(events)}", file=sys.stderr)

    session = ChatSession(pr_llm, system_prompt + _PROOFREAD_FORMAT, temperature=0.2)
    changed = False
    for start in range(0, len(events), pr_llm.batch_size):
        batch = events[start : start + pr_llm.batch_size]
        pairs = [f"SRC: {event.en}\nTGT: {event.zh}" for event in batch]
        if not quiet:
            print(
                f"  Batch {start // pr_llm.batch_size + 1}/{math.ceil(len(events) / pr_llm.batch_size)}: "
                f"proofreading split events {start + 1}-{start + len(batch)}",
                file=sys.stderr,
            )
        prompt = "\n\n".join(f"[{i}] {pair}" for i, pair in enumerate(pairs, 1))
        try:
            raw_result = session.ask(
                (
                    f"Proofread these {len(batch)} bilingual subtitle events. "
                    f"Return a JSON array with exactly {len(batch)} objects. "
                    "Do not merge, split, reorder, or change timing.\n\n"
                    f"{prompt}"
                ),
                max_tokens=max(4096, len(batch) * 260),
            )
        except Exception as e:
            print(f"Warning: proofread batch failed: {e}", file=sys.stderr)
            raw_result = ""

        fallback_pairs = [(event.en, event.zh) for event in batch]
        parsed_pairs = parse_proofread_response(raw_result, len(batch), fallback_pairs)
        for event, (en, zh) in zip(batch, parsed_pairs):
            new_en = en.strip() or event.en
            new_zh = zh.strip() or event.zh
            if new_en != event.en or new_zh != event.zh:
                changed = True
            event.en = new_en
            event.zh = new_zh
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


def normalize_token(text: str) -> str:
    tokens = text_tokens(text)
    return tokens[0] if tokens else ""


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


def align_split_events_by_position(
    segment: TranscriptSegment,
    en_parts: list[str],
    zh_parts: list[str],
) -> Optional[list[SplitEvent]]:
    words = timed_token_words(segment)
    if len(words) != len(text_tokens(segment.source_text())):
        return None

    events: list[SplitEvent] = []
    offset = 0
    for idx, en in enumerate(en_parts):
        part_len = len(text_tokens(en))
        if part_len <= 0:
            return None
        span_words = words[offset : offset + part_len]
        if len(span_words) != part_len:
            return None
        zh = zh_parts[idx] if idx < len(zh_parts) else ""
        events.append(
            SplitEvent(
                start=float(span_words[0].start),
                end=float(span_words[-1].end),
                en=en,
                zh=zh,
            )
        )
        offset += part_len

    if offset != len(words):
        return None
    return events


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
    en_parts: list[str],
    zh_parts: list[str],
) -> Optional[list[SplitEvent]]:
    if text_tokens(" ".join(en_parts)) != text_tokens(segment.source_text()):
        return None

    edged = align_split_events_by_edge_tokens(segment, en_parts, zh_parts)
    if edged:
        return clamp_split_events(segment, edged)

    events: list[SplitEvent] = []
    timed_words = timed_token_words(segment)
    offset = 0

    for idx, en in enumerate(en_parts):
        zh = zh_parts[idx] if idx < len(zh_parts) else ""
        span = find_word_span(timed_words, en, offset) if timed_words else None
        if not span:
            return None
        offset = span[1]
        start, end = span[2], span[3]
        events.append(SplitEvent(start=start, end=end, en=en, zh=zh))

    if not events:
        return None

    return clamp_split_events(segment, events)


def parse_split_response(
    content: str,
    expected_ids: list[int],
) -> tuple[dict[int, list[str]], dict[int, list[str]], str]:
    en: dict[int, list[str]] = {}
    zh: dict[int, list[str]] = {}
    expected_set = set(expected_ids)
    data = _extract_json_array(content)
    if data is None:
        return en, zh, "response is not a JSON array"
    if len(data) != len(expected_ids):
        return en, zh, f"JSON array length {len(data)} != expected {len(expected_ids)}"

    seen_ids: set[int] = set()
    for pos, item in enumerate(data, 1):
        if not isinstance(item, dict):
            return {}, {}, f"item {pos} is not an object"
        if set(item.keys()) != {"id", "en", "zh"}:
            return {}, {}, f"item {pos} keys {sorted(item.keys())} != ['en', 'id', 'zh']"
        item_id = item.get("id")
        try:
            item_id_int = int(item_id)
        except (TypeError, ValueError):
            return {}, {}, f"item {pos} has invalid id {item_id!r}"
        if item_id_int not in expected_set:
            return {}, {}, f"item {pos} id {item_id_int} not in expected ids {expected_ids}"
        if item_id_int in seen_ids:
            return {}, {}, f"duplicate id {item_id_int}"
        seen_ids.add(item_id_int)
        en_items = item.get("en", [])
        zh_items = item.get("zh", [])
        if not isinstance(en_items, list) or not isinstance(zh_items, list):
            return {}, {}, f"id {item_id_int} en/zh must both be arrays"
        en_parts = [str(p).replace("\\N", " ").strip() for p in en_items if str(p).strip()]
        zh_parts = [str(p).replace("\\N", " ").strip() for p in zh_items if str(p).strip()]
        if len(en_parts) != len(zh_parts):
            return {}, {}, f"id {item_id_int} EN parts {len(en_parts)} != ZH parts {len(zh_parts)}"
        if en_parts:
            en[item_id_int] = en_parts
            zh[item_id_int] = zh_parts
    if seen_ids != expected_set:
        missing = sorted(expected_set - seen_ids)
        return {}, {}, f"missing id(s): {missing}"
    return en, zh, ""


def whole_segment_split_event(segment: TranscriptSegment) -> SplitEvent:
    return SplitEvent(segment.start, segment.end, segment.source_text(), segment.translation)


def validated_split_events(
    segment: TranscriptSegment,
    en_parts: Optional[list[str]],
    zh_parts: Optional[list[str]],
) -> tuple[Optional[list[SplitEvent]], str]:
    if not en_parts or not zh_parts:
        return None, "no usable split parts for this id"
    if len(en_parts) != len(zh_parts):
        return None, f"EN parts {len(en_parts)} != ZH parts {len(zh_parts)}"
    expected_tokens = text_tokens(segment.source_text())
    actual_tokens = text_tokens(" ".join(en_parts))
    if actual_tokens != expected_tokens:
        return (
            None,
            f"EN tokens do not reconstruct source ({len(actual_tokens)} != {len(expected_tokens)})",
        )
    if len(en_parts) == 1:
        return [SplitEvent(segment.start, segment.end, en_parts[0], zh_parts[0])], ""
    events = align_split_events(segment, en_parts, zh_parts)
    if not events or len(events) != len(en_parts):
        return None, "split edge words could not align to WhisperX words"
    return events, ""


def split_segments(
    transcript: Transcript,
    llm: LLMConfig,
    split: SplitConfig,
    source_lang: str,
    target_lang: str,
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
                changed = True

    if not split.enabled:
        return changed

    pending = [
        s
        for s in transcript.segments
        if len(s.split_events) == 0
        or (
            len(s.split_events) == 1
            and s.split_events[0].en == s.source_text()
            and (len(s.source_text()) > split.max_chars or s.end - s.start > split.max_duration)
        )
    ]
    pending = [s for s in pending if len(s.source_text()) > split.max_chars or s.end - s.start > split.max_duration]
    if not pending:
        if not quiet:
            print("Split: cached/no long segments", file=sys.stderr)
        return changed

    if not quiet:
        print(f"Split: {len(pending)} long segment(s)", file=sys.stderr)

    style_prompt = render_language_template(
        load_prompt("split_prompt", _SPLIT_PROMPT_FALLBACK),
        source_lang,
        target_lang,
    )
    session = ChatSession(
        llm,
        f"{style_prompt}\n\n{_SPLIT_FORMAT}",
        temperature=0.1,
    )
    for start in range(0, len(pending), max(1, llm.batch_size // 2)):
        batch = pending[start : start + max(1, llm.batch_size // 2)]
        lines = []
        expected_ids = [seg.index for seg in batch]
        for seg in batch:
            lines.append(f"[{seg.index}] SRC: {seg.source_text()}")
            lines.append(f"[{seg.index}] TGT: {seg.translation}")
        user_prompt = (
            f"Split these {len(batch)} complete bilingual transcript segments for subtitle display.\n"
            f"Return exactly these ids, without renumbering: {expected_ids}.\n"
            f"SRC is {source_lang}. TGT is {target_lang}. "
            "These inputs were selected because they are long by duration or character count. "
            "Actively split when a segment contains a natural clause boundary. "
            "Keep it unsplit only if splitting would make the subtitle less readable.\n"
            "Important: JSON with single quotes is invalid and will be rejected; use double quotes only.\n\n"
            + "\n".join(lines)
        )
        try:
            content = session.ask(user_prompt, max_tokens=max(2048, len(batch) * 350))
            if not quiet:
                print("Split AI raw response:", file=sys.stderr)
                print(content.strip(), file=sys.stderr)
            en_splits, zh_splits, parse_error = parse_split_response(content, expected_ids)
            if parse_error and not quiet:
                print(f"Split parse warning: {parse_error}", file=sys.stderr)
        except Exception as e:
            print(f"Warning: split failed: {e}", file=sys.stderr)
            en_splits, zh_splits, parse_error = {}, {}, str(e)

        for seg in batch:
            events, reason = validated_split_events(seg, en_splits.get(seg.index), zh_splits.get(seg.index))
            if events is None:
                if not quiet:
                    print(
                        f"Split: fallback to whole segment #{seg.index} "
                        f"({reason or parse_error or 'invalid or unaligned AI split'})",
                        file=sys.stderr,
                    )
                    print(f"  Source text: {seg.source_text()}", file=sys.stderr)
                    print(f"  AI source parts: {en_splits.get(seg.index)}", file=sys.stderr)
                    print(f"  AI target parts: {zh_splits.get(seg.index)}", file=sys.stderr)
                events = [whole_segment_split_event(seg)]
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


def write_ass(
    output_path: str,
    template_path: str,
    title: str,
    events: list[SplitEvent],
    mode: str,
) -> None:
    header, events_header = load_template(template_path)
    header = re.sub(r"Title:\s*.*", f"Title: {title}", header)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write(events_header)
        if mode in ("proofread", "bilingual"):
            for event in events:
                f.write(
                    f"Dialogue: 0,{ass_time(event.start)},{ass_time(event.end)},"
                    f"bi-en,,0,0,0,,{ass_escape(event.en)}\n"
                )
        if mode in ("zh", "bilingual"):
            style = "bi-zh" if mode == "bilingual" else "zh"
            for event in events:
                text = wrap_cjk(ass_escape(event.zh))
                f.write(
                    f"Dialogue: 0,{ass_time(event.start)},{ass_time(event.end)},"
                    f"{style},,0,0,0,,{text}\n"
                )


DESCRIPTION_TRANSLATE_PROMPT = """You are a professional translator. Translate the following video title, description, and tags from ${SOURCE_LANG} to ${TARGET_LANG}.

Rules:
- First line: translated title only.
- Then a blank line, then the translated description.
- Preserve URLs, email addresses, handles, and paragraph structure.
- Do not add explanations."""


def translate_description(ctx: TranscriptContext, llm: LLMConfig, quiet: bool) -> str:
    title, webpage_url, tags = read_metadata(ctx)
    desc_text = _read_text_file(ctx.desc) if os.path.isfile(ctx.desc) else ""
    if not desc_text.strip() and not title:
        return ctx.target_desc
    prompt = f"Title: {title}\nURL: {webpage_url}\n\nDescription:\n{desc_text}"
    if tags:
        prompt += f"\n\nTags:\n{', '.join(tags)}"
    try:
        resp = llm._client().chat.completions.create(
            model=llm.model_name(),
            messages=[
                {
                    "role": "system",
                    "content": render_prompt_template(DESCRIPTION_TRANSLATE_PROMPT, ctx),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=max(2048, len(prompt) * 2),
        )
        response = resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"Warning: description translation failed: {e}", file=sys.stderr)
        return ctx.target_desc

    with open(ctx.target_desc, "w", encoding="utf-8") as f:
        f.write(response)
        f.write("\n")
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


def needs_llm(args) -> bool:
    return not args.only_beautify


def main() -> None:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env = load_env(script_dir)

    parser = argparse.ArgumentParser(
        description="Translate WhisperX JSON to proofread/target-language/bilingual ASS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  python translate_srt.py video.json --video video.webm
  python translate_srt.py video.json --source-lang en --target-lang zh
  python translate_srt.py video.json -o video.en-zh.ass
  python translate_srt.py video.json --only-beautify --video video.webm
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
    )

    if not args.skip_knowledge:
        build_glossary(
            transcript,
            ctx,
            llm,
            env.get("TAVILY_API_KEY", ""),
            int(env.get("TAVILY_MAX_RESULTS", "10") or "10"),
            args.quiet,
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

    changed = translate_segments(transcript, llm, system_prompt, args.quiet)
    changed = split_segments(
        transcript,
        llm,
        SplitConfig(
            enabled=not args.no_split,
            max_chars=args.split_max_chars,
            max_duration=args.split_max_duration,
        ),
        ctx.source_lang,
        ctx.target_lang,
        args.quiet,
    ) or changed
    if (args.proofread and not args.no_proofread and env.get("PROOFREAD", "1") != "0"):
        changed = proofread_split_events(transcript, llm, proofread_prompt, args.quiet) or changed
    if changed:
        save_transcript(transcript, ctx.beautified_json)

    if not os.path.isfile(template_path):
        print(f"Error: template.ass not found: {template_path}", file=sys.stderr)
        sys.exit(1)

    events = all_events(transcript)
    write_srt_events(ctx.split_source_srt, events, "en")
    write_srt_events(ctx.split_target_srt, events, "zh")
    write_ass(ctx.proofread_ass, template_path, ctx.base, events, "proofread")
    write_ass(ctx.target_ass, template_path, ctx.base, events, "zh")
    write_ass(ctx.bilingual_ass, template_path, ctx.base, events, "bilingual")

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
