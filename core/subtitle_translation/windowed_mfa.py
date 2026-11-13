"""Text-first planning and bounded, source-anchored post-translation alignment."""

from __future__ import annotations

import copy
import math
import time
import uuid
import wave
from pathlib import Path

import pysbd

from .aligned_transcript import map_display_words
from .artifacts import file_sha256, json_sha256, read_json_object, text_sha256
from .mfa import CORPUS_POLICY, align_window_corpus, canonical_audio_duration, validate_alignment
from .mfa_pipeline import canonical_audio
from .source_review import compose_adopted_text
from .project_logging import log, logged_stage


POLICY = "post-translation-windowed-mfa-v1"
MAX_WINDOW_SECONDS = 60.0
MAX_WINDOW_UNITS = 12
MAX_WORD_SECONDS = 2.0
MAX_WORD_GAP = 3.0
POSTPROCESS_POLICY = "coarse-output-scene-only-v3"


def text_source(review, source):
    import translate_srt as t

    text = review["text"]
    if (review["language"] != "en" or text_sha256(text) != review["text_sha256"]
            or compose_adopted_text(review["items"], "en") != (text, review["source_map"])):
        raise ValueError("invalid adopted text for windowed MFA")
    segments = []
    covered = 0
    for span in pysbd.Segmenter(language="en", clean=False, char_span=True).segment(text):
        if text[covered:span.start].strip():
            raise ValueError("sentence contexts lost source text")
        start = span.start + len(span.sent) - len(span.sent.lstrip())
        end = span.end - len(span.sent) + len(span.sent.rstrip())
        covered = span.end
        if start == end:
            continue
        entries = [r for r in review["source_map"] if r["range"][0] < end and r["range"][1] > start]
        items = [i for i in review["items"] if i["id"] in {r["id"] for r in entries}]
        if not items:
            raise ValueError("source context has no timing provenance")
        begin, finish = min(i["input_range"][0] for i in items), max(i["input_range"][1] for i in items)
        if (type(begin) not in (int, float) or type(finish) not in (int, float)
                or not math.isfinite(begin) or not math.isfinite(finish) or not 0 <= begin < finish):
            raise ValueError("invalid source coarse timing")
        segment = t.TranscriptSegment(len(segments) + 1, begin, finish, text[start:end])
        segment.source_range = (start, end)
        segment.source_refs = list(dict.fromkeys(ref for i in items for ref in i["source_refs"]))
        segment.text_origin = "glossary_corrected" if any(i["decision"] == "edit" for i in items) else "glossary_selected"
        segment.segment_timing = {"quality": "pending", "basis": "source_input_range", "text_planned": True}
        segments.append(segment)
    if text[covered:].strip() or not segments:
        raise ValueError("incomplete source sentence contexts")
    raw = copy.deepcopy(source.raw)
    raw.pop("word_segments", None)
    raw["_asr"] = {"schema_version": 2, "pipeline": POLICY, "final_text_sha256": review["text_sha256"],
                   "source_review_generation": review.get("generation", "")}
    return t.Transcript(source.path, "en", segments, raw)


def load_text_checkpoint(source, review, ctx, force=False):
    import translate_srt as t

    identity = json_sha256({"policy": POLICY, "review": review, "source": source.to_json(),
                           "project": ctx.dir, "languages": [ctx.source_lang_code, ctx.target_lang_code]})
    root = Path(ctx.dir) / ".asr_work" / "text_events" / identity
    root.mkdir(parents=True, exist_ok=True)
    path = root / "translated.json"
    fresh = text_source(review, source)
    fresh.raw["_source_first_input"] = identity
    fresh.raw = {k: v for k, v in fresh.to_json().items() if k != "segments"}
    if not force:
        try:
            cached = t.load_transcript(str(path))
            cached.raw.pop("word_segments", None)
            if (cached.raw == fresh.raw and len(cached.segments) == len(fresh.segments)
                    and all((a.index, a.text, round(a.start, 3), round(a.end, 3), a.source_range, a.source_refs, a.segment_timing)
                            == (b.index, b.text, round(b.start, 3), round(b.end, 3), b.source_range, b.source_refs, b.segment_timing)
                            and not a.words and (t.source_events_are_valid(a) or (
                                not a.split_events and not any((a.translation, a.event_plan_key, a.translation_key, a.proofread_key))))
                            for a, b in zip(cached.segments, fresh.segments))):
                return cached, path
        except (OSError, ValueError, TypeError, KeyError, SystemExit):
            # Invalid checkpoints are rebuilt from the immutable adopted text.
            pass
    return fresh, path


def plan_windows(review, segments, duration):
    """Merge shared/crossed provenance ranges, never merely adjacent padding."""
    ranges = review["source_map"]
    items = {i["id"]: i for i in review["items"]}
    groups = []
    for segment in segments:
        start, end = segment.source_range
        indices = [i for i, r in enumerate(ranges) if r["range"][0] < end and r["range"][1] > start]
        if not indices:
            raise ValueError("event context has no source window")
        low, high = min(indices), max(indices)
        if groups and low <= groups[-1]["high"]:
            groups[-1]["high"] = max(high, groups[-1]["high"])
            groups[-1]["segments"].append(segment.index)
        else:
            groups.append({"low": low, "high": high, "segments": [segment.index]})
    for group in groups:
        entries = ranges[group["low"]:group["high"] + 1]
        units = [items[r["id"]] for r in entries]
        begin = min(i["input_range"][0] for i in units)
        end = max(i["input_range"][1] for i in units)
        if not 0 <= begin < end <= duration + 0.001:
            raise ValueError("source timing is outside canonical audio")
        group.update(start_sample=math.floor(begin * 16000), end_sample=min(round(duration * 16000), math.ceil(end * 16000)),
                     source_range=[entries[0]["range"][0], entries[-1]["range"][1]],
                     eligible=end - begin <= MAX_WINDOW_SECONDS and len(units) <= MAX_WINDOW_UNITS)
    return groups


def crop_audio(audio, path, start, end):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{uuid.uuid4().hex}.wav")
    try:
        with wave.open(str(audio), "rb") as source, wave.open(str(temporary), "wb") as target:
            target.setparams(source.getparams())
            source.setpos(start)
            remaining = end - start
            while remaining:
                count = min(remaining, 16000 * 30)
                data = source.readframes(count)
                if len(data) != count * 2:
                    raise ValueError("truncated canonical window")
                target.writeframes(data)
                remaining -= count
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def align_events(transcript, review, media, runtime, config, ctx, admission_guard=None):
    import translate_srt as t

    if admission_guard:
        admission_guard()
    result = copy.deepcopy(transcript)
    local = Path(ctx.dir) / ".asr_work"
    audio = canonical_audio(media, config, local, transcript.raw.get("_batch_artifact", {}), admission_guard)
    audio_hash = file_sha256(audio)
    duration = canonical_audio_duration(audio)
    windows = plan_windows(review, result.segments, duration)
    deadline = time.monotonic() + max(300.0, 4 * duration)
    result.raw["_asr"].update(aligner="mfa", window_policy={"version": POLICY, "max_seconds": MAX_WINDOW_SECONDS,
        "max_units": MAX_WINDOW_UNITS, "max_word_seconds": MAX_WORD_SECONDS, "max_word_gap": MAX_WORD_GAP,
        "padding_samples": 0, "execution": CORPUS_POLICY}, audio_sha256=audio_hash, audio_duration=duration, window_count=len(windows))
    work = runtime.work_directory / json_sha256(str(Path(ctx.dir).resolve())) if runtime.work_directory else local / "mfa"
    entries = []
    for window in windows:
        if admission_guard:
            admission_guard()
        a, b = window["source_range"]
        text = review["text"][a:b]
        identity = {"policy": POLICY, "audio": audio_hash, "samples": [window["start_sample"], window["end_sample"]],
                    "text": text, "generation": transcript.raw.get("_batch_artifact", {})}
        root = work / "windows" / json_sha256(identity)
        window["id"] = json_sha256(identity)
        window["failure"] = "window_limit_exceeded" if window["eligible"] is False else "mfa_project_timeout"
        if window["eligible"] and deadline > time.monotonic():
            crop_audio(audio, root / "input.wav", window["start_sample"], window["end_sample"])
            entries.append({"id": window["id"], "audio": root / "input.wav", "text": text})
    remaining = deadline - time.monotonic()
    results = (align_window_corpus(entries, runtime, work, t.text_tokens, admission_guard=admission_guard, time_budget=remaining)
               if entries and remaining > 0 else {})
    for window in windows:
        if admission_guard:
            admission_guard()
        a, b = window["source_range"]
        text = review["text"][a:b]
        mapped, failure = [], window["failure"]
        alignment = results.get(window["id"])
        if alignment is not None:
            failure = alignment.get("reason") or "invalid_window_alignment"
            try:
                if alignment["status"] != "available":
                    raise ValueError(failure)
                words = validate_alignment({"schema_version": 1, "mfa_version": runtime.version, "words": alignment["words"]},
                                           text, (window["end_sample"] - window["start_sample"]) / 16000, runtime.version, t.text_tokens)
                if (any(w["end"] - w["start"] > MAX_WORD_SECONDS for w in words)
                        or any(y["start"] - x["end"] > MAX_WORD_GAP for x, y in zip(words, words[1:]))):
                    raise ValueError("abnormal_window_timing")
                mapped = map_display_words(text, words, t.text_tokens)
                for word in mapped:
                    word["source_range"] = [p + a for p in word["source_range"]]
                    word["start"] += window["start_sample"] / 16000
                    word["end"] += window["start_sample"] / 16000
            except (KeyError, TypeError, ValueError) as error:
                mapped, failure = [], str(error)
        if admission_guard:
            admission_guard()
        for segment in result.segments:
            if segment.index not in window["segments"]:
                continue
            sa, sb = segment.source_range
            words = [w for w in mapped if sa <= w["source_range"][0] and w["source_range"][1] <= sb]
            valid = bool(words) and "".join(t.text_tokens(" ".join(w["word"] for w in words))) == "".join(t.text_tokens(segment.text))
            event_times = []
            for event in segment.split_events:
                left, right = event.source_range
                selected = [w for w in words if sa + left <= w["source_range"][0] and w["source_range"][1] <= sa + right]
                complete = bool(selected) and t.text_tokens(" ".join(w["word"] for w in selected)) == t.text_tokens(event.en)
                valid = valid and complete
                if selected:
                    event_times.append((selected[0]["start"], selected[-1]["end"]))
            if valid:
                coarse_bounds = [segment.start, segment.end]
                for event, (start, end) in zip(segment.split_events, event_times):
                    event.start, event.end = start, end
                segment.start, segment.end = event_times[0][0], event_times[-1][1]
                segment.words = [t.TranscriptWord.from_json(w) for w in words]
                segment.word_timing = {"status": "available", "aligner": "mfa-window"}
                segment.segment_timing = {"quality": "aligned", "basis": "mfa_words", "text_planned": True,
                                          "input_range": coarse_bounds}
            else:
                segment.split_events = [t.SplitEvent(segment.start, segment.end, segment.text, segment.translation,
                                       json_sha256([segment.event_plan_key, "coarse-whole"])[:24], (0, len(segment.text)))]
                segment.words = []
                segment.split_status = "unsplit"
                segment.split_reason = segment.split_reason_detail = ""
                segment.word_timing = {"status": "unavailable", "aligner": "mfa-window"}
                segment.segment_timing = {"quality": "coarse", "basis": "source_input_range", "text_planned": True}
                log("WARNING", f'Warning: MFA #{segment.index}: {failure}; keeping whole bilingual sentence at source coarse times')
    if file_sha256(audio) != audio_hash:
        raise ValueError("canonical audio changed during windowed MFA")
    return merge_coarse_overlaps(result, review)


def merge_coarse_overlaps(transcript, review):
    """Project overlapping coarse contexts together, without guessed word times."""
    import translate_srt as t

    grouped = []
    for segment in transcript.segments:
        grouped.append(segment)
        while len(grouped) > 1:
            left, right = grouped[-2:]
            if (left.end <= right.start + 0.001 or not any(
                    s.segment_timing.get("quality") == "coarse" for s in (left, right))):
                break
            start, end = left.source_range[0], right.source_range[1]
            merged = copy.deepcopy(left)
            merged.start, merged.end = min(left.start, right.start), max(left.end, right.end)
            merged.text = review["text"][start:end]
            merged.source_range = (start, end)
            merged.source_refs = list(dict.fromkeys(left.source_refs + right.source_refs))
            merged.translation = " ".join([left.translation, right.translation])
            merged.event_plan_key = json_sha256([POSTPROCESS_POLICY, left.event_plan_key, right.event_plan_key, start, end])
            merged.translation_key = json_sha256([merged.event_plan_key, left.translation_key, right.translation_key])
            merged.proofread_key = ""
            merged.words = []
            merged.split_status = "unsplit"
            merged.split_reason = merged.split_reason_detail = ""
            merged.word_timing = {"status": "unavailable", "aligner": "mfa-window"}
            merged.segment_timing = {"quality": "coarse", "basis": "source_input_range", "text_planned": True}
            if right.text_origin == "glossary_corrected":
                merged.text_origin = right.text_origin
            merged.split_events = [t.SplitEvent(merged.start, merged.end, merged.text, merged.translation,
                                   merged.event_plan_key[:24], (0, len(merged.text)))]
            grouped[-2:] = [merged]
            log("WARNING", f'Warning: MFA coarse overlap: merged contexts #{left.index}/#{right.index}; preserved complete bilingual text')
    transcript.segments = grouped
    return transcript




@logged_stage("mfa")
def finish_timing(transcript, review, media, runtime, config, ctx, video, options, skip, force, admission_guard=None):
    import translate_srt as t

    options = copy.deepcopy(options)
    timed = align_events(transcript, review, media, runtime, config, ctx, admission_guard)
    if not skip:
        if admission_guard:
            admission_guard()
        t.detect_project_scenes(ctx, video, options, )
    key = json_sha256({"policy": POLICY, "postprocess": POSTPROCESS_POLICY, "timed": timed.to_json()})
    marker = Path(ctx.dir) / ".asr_work" / "text_events" / (json_sha256(str(Path(ctx.beautified_json).resolve())) + ".timed.json")
    if not force:
        try:
            manifest = read_json_object(marker)
            if manifest["key"] == key and manifest["sha256"] == file_sha256(ctx.beautified_json):
                cached = t.load_transcript(ctx.beautified_json)
                if all(t.source_events_are_valid(s) for s in cached.segments):
                    return cached, marker, key
        except (OSError, ValueError, TypeError, KeyError, SystemExit):
            # A stale timed projection does not invalidate translated text.
            pass
    return timed, marker, key
