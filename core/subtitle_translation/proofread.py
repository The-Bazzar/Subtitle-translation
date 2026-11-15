"""Explicit target-only proofreading of completed translation artifacts."""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import shutil
import tempfile
from contextlib import ExitStack
from pathlib import Path

import translate_srt as t

from .artifacts import atomic_write_json, file_sha256, json_sha256, read_json_object
from .project_logging import log, project_run, validate_log_level


EXPORT_FIELDS = ("split_source_srt", "split_target_srt", "source_ass", "target_ass", "bilingual_ass")


def load_completed_translation(path, payload):
    rows = payload.get("segments")
    pipeline = payload.get("pipeline")
    if (not isinstance(pipeline, dict) or pipeline.get("format") != "json-first-transcript"
            or not isinstance(rows, list) or not rows):
        raise ValueError("proofread requires a completed translation working JSON, not original ASR, ASS or SRT")
    ids, segments = set(), []
    for row in rows:
        if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] in ids:
            raise ValueError("proofread requires unique integer sentence IDs")
        ids.add(row["id"])
        for field in ("text", "translation", "event_plan_key", "translation_key"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(f"sentence #{row['id']} has no completed {field}; run translate first")
        events = row.get("split_events")
        if not isinstance(events, list) or not events:
            raise ValueError("proofread requires existing bilingual events")
        for item in (row, *events):
            if not isinstance(item, dict) or any(type(item.get(k)) not in (int, float) or not math.isfinite(item[k])
                                                 for k in ("start", "end")):
                raise ValueError("proofread requires finite completed timing, not pending text checkpoints")
            if item.get("timing_status") == "pending" or not 0 <= item["start"] < item["end"]:
                raise ValueError("proofread cannot align or repair pending/invalid timing")
        for event in events:
            if any(not isinstance(event.get(k), str) or not event[k].strip() for k in ("id", "en", "zh")):
                raise ValueError("proofread requires nonempty event IDs and bilingual text")
        segment = t.TranscriptSegment.from_json(row["id"], row)
        if (segment.text != row["text"] or segment.segment_timing.get("quality") == "pending"
                or not t.source_events_are_valid(segment)):
            raise ValueError(f"sentence #{segment.index} has an invalid or pending source event plan")
        segments.append(segment)
    return t.Transcript(str(path), str(payload.get("language", "")), segments,
                        {key: value for key, value in payload.items() if key != "segments"})


def language_pair(args, env, payload):
    stored = payload.get("subtitle_languages", {})
    if not isinstance(stored, dict):
        raise ValueError("invalid subtitle_languages metadata")
    if stored and any(not isinstance(stored.get(key), str) or not stored[key] for key in ("source", "target")):
        raise ValueError("subtitle_languages must contain source and target ISO codes")
    source = args.source_lang or stored.get("source") or env.get("SOURCE_LANG") or payload.get("language") or "en"
    target = args.target_lang or stored.get("target") or env.get("TARGET_LANG") or "zh"
    source = t.iso_639_suffix(source, "source")
    target = t.iso_639_suffix(target, "target")
    if any(stored.get(key) and stored[key] != value for key, value in (("source", source), ("target", target))):
        raise ValueError("proofread language conflicts with the saved translation; use translate to change languages")
    if not stored:
        log("WARNING", "Legacy working JSON has no saved language pair; using CLI/config/ASR language defaults")
    return source, target


def output_paths(args, input_path, source, target):
    destination = Path(args.output_json).resolve() if args.output_json else input_path
    if destination.suffix.lower() != ".json":
        raise ValueError("--output-json must name a translation working JSON")
    if destination != input_path and destination.exists():
        load_completed_translation(destination, read_json_object(destination))
    if args.output and Path(args.output).suffix.lower() != ".ass":
        raise ValueError("--output must name a bilingual ASS file; use --output-json for JSON")
    ctx = t.TranscriptContext.from_json(str(destination), args.output, source, target, str(destination))
    paths = [destination, *[Path(getattr(ctx, field)).resolve() for field in EXPORT_FIELDS]]
    if len(set(paths)) != len(paths) or input_path in paths[1:]:
        raise ValueError("proofread output paths must not collide")
    if any(not path.parent.is_dir() for path in paths):
        raise ValueError("proofread output directories must already exist")
    return destination, ctx


def publish_results(transcript, payload, input_path, input_hash, destination, destination_hash, ctx, template):
    updated = copy.deepcopy(payload)
    for row, segment in zip(updated["segments"], transcript.segments):
        row["translation"] = segment.translation
        for event, result in zip(row["split_events"], segment.split_events):
            event["zh"] = result.zh
        if segment.proofread_key:
            row["proofread_key"] = segment.proofread_key
    updated.pop("word_segments", None)
    updated["subtitle_languages"] = {**updated.get("subtitle_languages", {}),
                                     "source": ctx.source_lang_code, "target": ctx.target_lang_code}
    marker = input_path.parent / ".asr_work" / "text_events" / (json_sha256(str(input_path)) + ".timed.json")
    timed_manifest = None
    if destination == input_path:
        try:
            candidate = read_json_object(marker)
            if candidate.get("sha256") == input_hash and isinstance(candidate.get("key"), str) and candidate["key"]:
                timed_manifest = candidate
        except (OSError, ValueError):
            # Missing or invalid timing manifests remain cache misses.
            pass
    with ExitStack() as stack:
        directories, staged, exports = {}, copy.copy(ctx), []

        def temporary(target):
            parent = target.parent
            if parent not in directories:
                directories[parent] = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=".proofread-", dir=parent)))
            return directories[parent] / target.name

        for field in EXPORT_FIELDS:
            target = Path(getattr(ctx, field)).resolve()
            path = temporary(target)
            setattr(staged, field, str(path))
            exports.append((path, target))
        t.export_subtitles(transcript, staged, template)
        json_path = temporary(destination)
        atomic_write_json(json_path, updated)
        if file_sha256(input_path) != input_hash:
            raise ValueError("translation input changed during proofreading; results were not published")
        current_hash = file_sha256(destination) if destination.exists() else None
        if current_hash != destination_hash:
            raise ValueError("translation output changed during proofreading; results were not published")
        exports.append((json_path, destination))
        backups, attempted = {}, []
        for _, target in exports:
            backup = temporary(target).with_name(target.name + ".before") if target.exists() else None
            if backup is not None:
                shutil.copy2(target, backup)
            backups[target] = backup
        try:
            for staged_path, target in exports:
                attempted.append(target)
                os.replace(staged_path, target)
        except BaseException:
            for target in reversed(attempted):
                try:
                    if backups[target] is None:
                        target.unlink(missing_ok=True)
                    else:
                        os.replace(backups[target], target)
                except OSError:
                    log("ERROR", f"Could not restore output after interrupted publish: {target}")
            raise
    if timed_manifest is not None:
        try:
            atomic_write_json(marker, {**timed_manifest, "sha256": file_sha256(destination)})
        except OSError:
            log("WARNING", "Proofread JSON saved, but timed-result manifest could not be refreshed; it will be a cache miss")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="subtitle-translation proofread",
        description="Reproofread target text in a completed translation JSON and re-export subtitles; no translation or timing stages.")
    parser.add_argument("json", help="Completed translation working JSON, usually <base>.beautified.json")
    parser.add_argument("--output-json", default="", help="Save the updated working JSON separately; default: update input")
    parser.add_argument("-o", "--output", default="", help="Bilingual ASS output path")
    parser.add_argument("-t", "--template", default="", help="ASS template path")
    parser.add_argument("--source-lang", "--lang", default="")
    parser.add_argument("--target-lang", default="")
    parser.add_argument("--glossary", default="", help="Existing glossary path; never rebuilt")
    args = parser.parse_args(argv)
    root = t._config_root()
    env = t.load_env(root)
    try:
        level = validate_log_level(env.get("LOG_LEVEL", "INFO"))
        batch_size = int(env.get("TRANSLATE_BATCH_SIZE", "50") or "50")
        if batch_size < 1 or int(env.get("PROOFREAD_BATCH_SIZE", "") or max(1, batch_size // 2)) < 1:
            raise ValueError("translation/proofread batch sizes must be positive")
    except ValueError as error:
        parser.error(str(error))
    path = Path(args.json).resolve()
    if path.suffix.lower() != ".json" or not path.is_file():
        log("ERROR", "proofread requires an existing translation working JSON")
        return 1
    with project_run(path.parent, level):
        try:
            initial_hash = file_sha256(path)
            payload = read_json_object(path)
            transcript = load_completed_translation(path, payload)
            source, target = language_pair(args, env, payload)
            ctx = t.TranscriptContext.from_json(str(path), source_lang=source, target_lang=target)
            destination, output_ctx = output_paths(args, path, source, target)
            destination_hash = file_sha256(destination) if destination.exists() else None
            template = args.template or str(Path(root) / "template.ass")
            t.load_template(template)
            llm = t.proofread_llm_from_env(env, t.translate_llm_from_env(env, batch_size), batch_size)
            if not llm.provider:
                raise ValueError("PROOFREAD_PROVIDER or TRANSLATE_PROVIDER is required")
            config = t.EmbeddingConfig.from_env(env, ctx)
            if config.enabled and config.store != "chroma":
                raise ValueError("proofread only supports the chroma embedding store")
            retriever = t.refresh_embedding_retriever(transcript, config, env, ctx, fatal=True) if config.enabled else None
            prompt = t.render_prompt_template(t.load_prompt("proofread_prompt", t._PROOFREAD_PROMPT_FALLBACK), ctx)
            prompt += t.load_description(ctx.desc)
            prompt += t.load_glossary_prompt_context(args.glossary or ctx.glossary, retriever)
            for segment in transcript.segments:
                segment.proofread_key = ""
            original = copy.deepcopy(transcript)
            if file_sha256(path) != initial_hash:
                raise ValueError("translation input changed before proofreading")
            log("INFO", "Explicit proofread: rechecking existing target text; translation, glossary generation, MFA and metadata are not run")
            t.proofread_target_events(transcript, ctx, llm, prompt, retriever,
                                     retrieval_top_k=t.proofread_retrieval_top_k_from_env(env))
            frozen = copy.deepcopy(transcript)
            for before, after in zip(original.segments, frozen.segments):
                after.translation, after.proofread_key = before.translation, before.proofread_key
                for old_event, event in zip(before.split_events, after.split_events):
                    event.zh = old_event.zh
            if frozen != original:
                raise ValueError("proofreading changed frozen source, timing or event structure")
            publish_results(transcript, payload, path, initial_hash, destination, destination_hash, output_ctx, template)
            for segment in transcript.segments:
                level, status = ("INFO", "reviewed") if segment.proofread_key else ("WARNING", "kept_original")
                for index, event in enumerate(segment.split_events, 1):
                    log(level, f"Proofread event #{segment.index}.{index} id={event.id} status={status} "
                               f"time={event.start:.3f}-{event.end:.3f}s "
                               f"{source}={json.dumps(event.en, ensure_ascii=False)} "
                               f"{target}={json.dumps(event.zh, ensure_ascii=False)}", stage="proofread")
            log("INFO", f"Proofread working JSON: {destination}")
            print(f"OUTPUT_JSON={destination}")
            print(f"OUTPUT_ASS={Path(output_ctx.bilingual_ass).resolve()}")
            return 0
        except (OSError, ValueError, TypeError, KeyError) as error:
            log("ERROR", f"Proofread failed: {error}")
            return 1
