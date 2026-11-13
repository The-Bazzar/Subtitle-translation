"""Text-only source adoption. A review is not an acoustic truth claim."""

from __future__ import annotations

import uuid
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from .artifacts import atomic_write_json, atomic_write_text, json_sha256, read_json_object, text_sha256


REVIEW_SCHEMA = 1
REVIEW_POLICY = "text-adoption-v1"

REVIEW_PROTOCOL = """SOURCE TEXT ADOPTION AND SUBTITLE EDITING:
This stage has text candidates and context, NOT access to the audio. Do not claim
to have verified what the speaker actually said. Agreement is not proof of truth.
Select the most defensible text and make minimal readability/grammar edits only
when meaning, negation, quantity, names and expressive intent remain unchanged.
Preserve deliberate jokes, quotations, dialect, wordplay and expressive repetition.
If intent or a proposed correction is uncertain, keep the candidate verbatim.
Do not generate subtitle boundaries, timestamps or target-language translations.
The full glossary is binding terminology context, not proof of actual pronunciation.
Respond with a JSON object containing only "items". For every input item return
"id", the actual source ISO language key, "decision" ("keep" or "edit"),
"basis" ("candidate", "context" or "readability"), and "reason" (a short string).
Use "keep"/"candidate" when unchanged; edits must give a nonempty reason and
use "context" or "readability". Never put uncertainty notes into the source text.
Every item must have actual, nonempty source text. No placeholders or missing items.
"""


@dataclass(frozen=True)
class SourceUnit:
    id: int
    text: str
    start: float
    end: float
    source_id: str


def validate_units(units: list[SourceUnit]) -> None:
    ids = [unit.id for unit in units]
    if len(set(ids)) != len(ids) or any(type(value) is not int for value in ids):
        raise ValueError("source review requires unique integer IDs")
    if any(not isinstance(unit.text, str) or not unit.text.strip() for unit in units):
        raise ValueError("source review requires actual nonempty fallback text")
    if any(not isinstance(unit.source_id, str) or not unit.source_id
           or not all(type(value) in (float, int) and math.isfinite(value) for value in (unit.start, unit.end))
           or not 0 <= unit.start <= unit.end for unit in units):
        raise ValueError("source review requires valid provenance and coarse ranges")


def validate_review(row: dict, unit: SourceUnit, source_key: str) -> dict:
    if (set(row) != {"id", source_key, "decision", "basis", "reason"}
            or type(row["id"]) is not int or row["id"] != unit.id):
        raise ValueError("invalid source review fields or ID")
    text, decision, basis, reason = (row[source_key], row["decision"], row["basis"], row["reason"])
    if not isinstance(text, str) or not text.strip() or text != text.strip():
        raise ValueError("review must provide nonempty source text without surrounding whitespace")
    if not isinstance(reason, str):
        raise ValueError("review reason must be a string")
    if decision == "keep":
        if text != unit.text or basis != "candidate":
            raise ValueError("keep must preserve the candidate verbatim")
    elif decision == "edit":
        if text == unit.text or basis not in {"context", "readability"} or not reason.strip():
            raise ValueError("edits require changed text and an explicit editing basis")
    else:
        raise ValueError("invalid review decision")
    return {
        "id": unit.id, "text": text, "original_text": unit.text,
        "decision": decision, "basis": basis, "reason": reason,
        "source_refs": [unit.source_id], "input_range": [unit.start, unit.end],
    }


def fallback_review(unit: SourceUnit, reason: str) -> dict:
    return {
        "id": unit.id, "text": unit.text, "original_text": unit.text,
        "decision": "fallback", "basis": "candidate", "reason": reason,
        "source_refs": [unit.source_id], "input_range": [unit.start, unit.end],
    }


def review_input_identity(units: list[SourceUnit], language: str, glossary: str, context: dict) -> str:
    validate_units(units)
    return json_sha256({
        "schema": REVIEW_SCHEMA, "policy": REVIEW_POLICY, "units": [asdict(unit) for unit in units],
        "language": language, "glossary_sha256": text_sha256(glossary), "context": context,
    })


def _validate_items(items: list[dict], units: list[SourceUnit]) -> None:
    if not isinstance(items, list) or len(items) != len(units):
        raise ValueError("review artifact lost source units")
    for item, unit in zip(items, units):
        if not isinstance(item, dict) or item.get("original_text") != unit.text:
            raise ValueError("review artifact has a different source")
        if item.get("source_refs") != [unit.source_id] or item.get("input_range") != [unit.start, unit.end]:
            raise ValueError("review artifact has different provenance")
        if type(item.get("id")) is not int or item.get("id") != unit.id or not isinstance(item.get("text"), str) or not item["text"].strip():
            raise ValueError("review artifact has an invalid item")
        if item.get("decision") == "fallback":
            if item != fallback_review(unit, item.get("reason", "")) or not isinstance(item.get("reason"), str):
                raise ValueError("invalid fallback artifact")
        else:
            row = {"id": item["id"], "source": item["text"], "decision": item.get("decision"),
                   "basis": item.get("basis"), "reason": item.get("reason")}
            if validate_review(row, unit, "source") != item:
                raise ValueError("invalid review artifact")


def compose_adopted_text(items: list[dict], language: str) -> tuple[str, list[dict]]:
    separator = "" if language in {"zh", "ja"} else " "
    parts, ranges, offset = [], [], 0
    for item in items:
        if parts:
            offset += len(separator)
        ranges.append({"id": item["id"], "range": [offset, offset + len(item["text"])]})
        parts.append(item["text"])
        offset += len(item["text"])
    return separator.join(parts), ranges


def publish_review(
    pointer_path: Path, identity: str, glossary_path: Path, glossary: str,
    units: list[SourceUnit], items: list[dict], language: str,
    *, provenance: dict | None = None,
) -> dict:
    validate_units(units)
    if not re.fullmatch(r"[0-9a-f]{64}", identity):
        raise ValueError("invalid source review identity")
    provenance = provenance or {}
    if (set(provenance) - {"provider", "model", "prompt_sha256", "input_file_sha256"}
            or any(not isinstance(value, str) for value in provenance.values())):
        raise ValueError("invalid source review provenance fields")
    _validate_items(items, units)
    generation = uuid.uuid4().hex
    root = pointer_path.parent / ".asr_work" / "source_review" / identity / generation
    root.mkdir(parents=True, exist_ok=False)
    text, ranges = compose_adopted_text(items, language)
    payload = {
        "schema_version": REVIEW_SCHEMA, "policy": REVIEW_POLICY, "input_sha256": identity,
        "generation": generation, "language": language, "items": items,
        "provenance": provenance,
        "text": text, "source_map": ranges,
    }
    payload["text_sha256"] = text_sha256(payload["text"])
    atomic_write_text(root / "glossary.md", glossary)
    atomic_write_json(root / "source_review.json", payload)
    manifest = {
        "schema_version": REVIEW_SCHEMA, "input_sha256": identity, "generation": generation,
        "generation_dir": root.relative_to(pointer_path.parent).as_posix(),
        "glossary_sha256": text_sha256(glossary), "source_review_sha256": json_sha256(payload),
    }
    atomic_write_json(root / "manifest.json", manifest)
    if glossary_path.read_text(encoding="utf-8") != glossary:
        raise ValueError("glossary changed during source review; generation not published")
    atomic_write_json(pointer_path, manifest)
    return payload


def load_review(pointer_path: Path, identity: str, glossary: str, units: list[SourceUnit]) -> dict | None:
    try:
        if not re.fullmatch(r"[0-9a-f]{64}", identity):
            return None
        manifest = read_json_object(pointer_path)
        if (manifest["schema_version"] != REVIEW_SCHEMA or manifest["input_sha256"] != identity
                or manifest["glossary_sha256"] != text_sha256(glossary)):
            return None
        root = (pointer_path.parent / manifest["generation_dir"]).resolve()
        expected = (pointer_path.parent / ".asr_work" / "source_review" / identity).resolve()
        if not root.is_relative_to(expected) or root.parent != expected or root.name != manifest["generation"]:
            return None
        if read_json_object(root / "manifest.json") != manifest:
            return None
        if text_sha256((root / "glossary.md").read_text(encoding="utf-8")) != manifest["glossary_sha256"]:
            return None
        payload = read_json_object(root / "source_review.json")
        if json_sha256(payload) != manifest["source_review_sha256"]:
            return None
        if (payload["schema_version"] != REVIEW_SCHEMA or payload["policy"] != REVIEW_POLICY
                or payload["input_sha256"] != identity or payload["generation"] != manifest["generation"]):
            return None
        _validate_items(payload["items"], units)
        if not isinstance(payload.get("language"), str):
            return None
        text, ranges = compose_adopted_text(payload["items"], payload["language"])
        if payload["text"] != text or payload["text_sha256"] != text_sha256(text) or payload.get("source_map") != ranges:
            return None
        return payload
    except (OSError, ValueError, TypeError, KeyError):
        return None
