"""Run with the MFA environment's Python, never the CLI's optional dependencies."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import sys
from pathlib import Path


def environment_fingerprint() -> str:
    distributions = sorted((item.metadata.get("Name", ""), item.version)
                           for item in importlib.metadata.distributions())
    conda = []
    for path in (Path(sys.prefix) / "conda-meta").glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        conda.append([data.get(key, "") for key in ("name", "version", "build", "sha256", "md5")])
    payload = {"python": list(sys.version_info[:3]), "distributions": distributions, "conda": sorted(conda)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def export_word_tiers(path: Path) -> list[dict]:
    from praatio import textgrid

    grid = textgrid.openTextgrid(str(path), includeEmptyIntervals=False, reportingMode="error")
    words = []
    for name in grid.tierNames:
        if name != "words" and not name.endswith(" - words"):
            continue
        tier = grid.getTier(name)
        if not isinstance(tier, textgrid.IntervalTier):
            raise ValueError("MFA word tier must contain intervals")
        for entry in tier.entries:
            words.append({"word": entry.label, "start": entry.start, "end": entry.end})
    return sorted(words, key=lambda item: (item["start"], item["end"]))


def export_corpus_words(directory: Path, ids: list[str]) -> list[dict]:
    if (not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not re.fullmatch(r"[0-9a-f]{64}", i) for i in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError("invalid MFA corpus IDs")
    grids = {}
    for path in directory.rglob("*.TextGrid"):
        grids.setdefault(path.stem, []).append(path)
    result = []
    for item_id in ids:
        paths = grids.get(item_id, [])
        try:
            if len(paths) != 1:
                raise ValueError("missing_or_ambiguous_textgrid")
            words = export_word_tiers(paths[0])
            result.append({"id": item_id, "words": words})
        except Exception:
            result.append({"id": item_id, "words": [], "error": "mfa_window_export_failed"})
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--aligned-directory")
    parser.add_argument("--ids-file")
    parser.add_argument("--expected-vad-revision", default="")
    parser.add_argument("--expected-vad-sha256", default="")
    args = parser.parse_args(argv)
    version = importlib.metadata.version("montreal-forced-aligner")
    if version != args.expected_version:
        raise ValueError(f"MFA version mismatch: expected {args.expected_version}, got {version}")
    from praatio import textgrid  # noqa: F401

    result = {"schema_version": 1, "mfa_version": version,
              "praatio_version": importlib.metadata.version("praatio"), "environment_sha256": environment_fingerprint()}
    for name in ("speechbrain", "torch", "torchaudio", "huggingface_hub"):
        try:
            result[name + "_version"] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name + "_version"] = ""
    if args.expected_vad_revision or args.expected_vad_sha256:
        if __package__:
            from .mfa_prefetch import cache_info
        else:
            from mfa_prefetch import cache_info

        info = cache_info(Path(os.environ["HF_HOME"]))
        if (info["revision"], info["files_sha256"]) != (args.expected_vad_revision, args.expected_vad_sha256):
            raise ValueError("MFA VAD cache differs from its runtime manifest")
        result.update(vad_revision=info["revision"], vad_sha256=info["files_sha256"])
    if args.aligned_directory:
        ids = json.loads(Path(args.ids_file).read_text(encoding="utf-8"))["ids"]
        result["items"] = export_corpus_words(Path(args.aligned_directory), ids)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
