"""One semantic translation response supplies both source and target events."""

from __future__ import annotations

import json
import re
import time

from .artifacts import json_sha256
from .project_logging import log, logged_stage


PROTOCOL_VERSION = "semantic-bilingual-events-v1"
PROTOCOL = """SEMANTIC BILINGUAL SUBTITLE TRANSLATION (overrides earlier splitting and response instructions):
Read the complete source sentence, its context and the full glossary. Translate
the whole meaning naturally, then choose readable semantic subtitle units and
return their source and target texts together in this SAME response.
Return {"items":[{"id":<sentence id>,"${TARGET_LANG_CODE}":<whole translation>,
"split_events":[{"${SOURCE_LANG_CODE}":<source excerpt>,"${TARGET_LANG_CODE}":<matching translation>}]}]}.
Copy consecutive source excerpts verbatim and in order. The source is frozen:
do not correct, paraphrase, omit or repeat it. Translate with whole-sentence
context, not by independently translating isolated words or fragments.
Break at meaningful clause, contrast, explanation or thought boundaries. Keep
subtitles comfortable to read; avoid both dense multi-clause paragraphs and
tiny dependent fragments. Keep names, fixed expressions and joke setup/punchline
coherent. A short sentence may stay whole, but character count alone is not a
reason to skip semantic judgment. There is no required number of pieces.
Let each target excerpt express its source unit naturally while preserving the
whole meaning, tone, negation, names and quantities. Source and target word order
need not match word for word. Keep necessary connective meaning across units.
Do not calculate character counts, offsets, boundary IDs, event IDs, durations,
timestamps or exact concatenation checks. The whole translation and target
parts may differ in spacing or punctuation; do not distort natural translation
to make their strings identical. Follow the target subtitle style.
Return actual nonempty subtitles, not explanations, doubt markers or editorial
notes. Do not generate a separate split response or alter sentence IDs.
"""


def source_ranges(source: str, parts: list[str], language: str) -> list[tuple[int, int]]:
    """Locate excerpts locally; the model never computes character coordinates."""
    ranges, cursor = [], 0
    for part in parts:
        if not isinstance(part, str) or not part.strip():
            raise ValueError("empty source excerpt")
        begin = cursor
        while cursor < len(source) and source[cursor].isspace():
            cursor += 1
        pattern = r"\s+".join(re.escape(word) for word in part.split())
        match = re.compile(pattern).match(source, cursor)
        if not match:
            raise ValueError("source excerpts cannot be located in the frozen text")
        cursor = match.end()
        if (language not in {"zh", "ja", "ko"} and cursor < len(source)
                and source[cursor - 1].isalnum() and source[cursor].isalnum()):
            raise ValueError("source excerpt cuts inside a word")
        while cursor < len(source) and source[cursor].isspace():
            cursor += 1
        ranges.append((begin, cursor))
    if not ranges or cursor != len(source):
        raise ValueError("source excerpts leave unlocated text")
    return ranges


def whole_event(segment, whole, plan_key):
    import translate_srt as t

    pending = segment.segment_timing.get("quality") == "pending"
    return t.SplitEvent(None if pending else segment.start, None if pending else segment.end,
                        segment.text, whole, json_sha256([plan_key, 0, len(segment.text)])[:24], (0, len(segment.text)))


def response_events(row, segment, ctx, plan_key, split_enabled):
    import translate_srt as t

    whole = row.get(ctx.target_lang_code)
    if not isinstance(whole, str) or not whole.strip():
        raise ValueError("whole translation must be a nonempty string")
    whole = whole.strip()
    if not split_enabled:
        return whole, [whole_event(segment, whole, plan_key)], None
    try:
        rows = row.get("split_events")
        if not isinstance(rows, list) or not rows:
            raise ValueError("missing bilingual subtitle parts")
        source, target = [], []
        for item in rows:
            if not isinstance(item, dict):
                raise ValueError("invalid bilingual subtitle part")
            en, zh = item.get(ctx.source_lang_code), item.get(ctx.target_lang_code)
            if not isinstance(en, str) or not en.strip() or not isinstance(zh, str) or not zh.strip():
                raise ValueError("bilingual subtitle parts must have actual source and target text")
            source.append(en)
            target.append(zh.strip())
        ranges = source_ranges(segment.text, source, ctx.source_lang_code)
        if segment.segment_timing.get("quality") == "pending":
            events = [t.SplitEvent(None, None, segment.text[a:b].strip(), zh,
                                  json_sha256([plan_key, a, b])[:24], (a, b))
                      for (a, b), zh in zip(ranges, target)]
        elif len(ranges) == 1:
            events = [whole_event(segment, target[0], plan_key)]
        else:
            boundaries = t.source_word_boundaries(segment)
            by_offset = {item["offset"]: item["id"] for item in boundaries}
            cuts = [0, *[b for a, b in ranges]]
            if any(c not in by_offset for c in cuts):
                raise ValueError("source excerpts have no reliable word timing boundary")
            events = t.source_events_from_boundaries(segment, boundaries, [by_offset[c] for c in cuts], plan_key)
            for event, zh in zip(events, target):
                event.zh = zh
        return whole, events, None
    except (ValueError, KeyError, TypeError) as error:
        return whole, [whole_event(segment, whole, plan_key)], str(error)


def log_events(segment, ctx, whole, events, status, prefix, *, cached=False, warning=None):
    level = "INFO" if status in {"split", "unsplit"} else "WARNING"
    cache = " cache=hit" if cached else ""
    detail = (f"{prefix} sentence #{segment.index} status={status} events={len(events)}{cache} project={ctx.base!r}"
              f" {ctx.source_lang_code}={json.dumps(segment.text, ensure_ascii=False)}"
              f" {ctx.target_lang_code}={json.dumps(whole, ensure_ascii=False)}")
    if warning:
        detail += f" reason={json.dumps(warning, ensure_ascii=False)}; keeping whole bilingual sentence"
    log(level, detail)
    for index, subtitle in enumerate(events, 1):
        timing = ("pending" if subtitle.start is None or subtitle.end is None
                  else f"{subtitle.start:.3f}-{subtitle.end:.3f}s")
        log(level, f"{prefix} event #{segment.index}.{index} id={subtitle.id} status={status}{cache} "
                   f"time={timing} project={ctx.base!r}"
                   f" {ctx.source_lang_code}={json.dumps(subtitle.en, ensure_ascii=False)}"
                   f" {ctx.target_lang_code}={json.dumps(subtitle.zh, ensure_ascii=False)}")


@logged_stage("translate")
def translate_events(transcript, ctx, llm, system_prompt, retriever=None, *, split_enabled=True,
                     context_window=1, checkpoint=None, admission_guard=None):
    import translate_srt as t

    prompt = system_prompt + "\n\n" + t._JSON_FORMAT + "\n\n" + t.render_prompt_template(PROTOCOL, ctx)
    if not split_enabled:
        prompt += "\nSEGMENTATION DISABLED: Return one bilingual event for the complete source sentence."
    if retriever is not None:
        prompt += "\n\n" + t._RETRIEVED_CONTEXT_RULES
    pending, plans, changed = [], {}, False
    for segment in transcript.segments:
        context = t.source_first_context(transcript, segment, max(0, context_window))
        if retriever is not None:
            context["retrieved_context"] = retriever.retrieve_texts([segment.text], top_k=1)[0]
        plan_key = json_sha256({"version": PROTOCOL_VERSION, "source": segment.text, "id": segment.index,
                               "project": ctx.dir, "generation": transcript.raw.get("_batch_artifact", {}),
                               "source_input": transcript.raw.get("_source_first_input", ""), "context": context,
                               "split_enabled": split_enabled, "prompt": prompt, "model": t.source_first_model_identity(llm),
                               "languages": [ctx.source_lang_code, ctx.target_lang_code]})
        if (segment.translation_key == plan_key and segment.event_plan_key == plan_key
                and isinstance(segment.translation, str) and segment.translation.strip()
                and t.source_events_are_valid(segment) and all(isinstance(e.zh, str) and e.zh.strip() for e in segment.split_events)):
            log("TRACE", f"Translation cache hit: sentence #{segment.index}")
            status = "split" if len(segment.split_events) > 1 else "unsplit"
            if segment.split_status not in {"ok", "unsplit"}:
                status = segment.split_status or "unknown"
            log_events(segment, ctx, segment.translation, segment.split_events, status, "Translate cache:", cached=True)
            continue
        plans[segment.index] = (segment, plan_key)
        pending.append(t.make_language_item(segment.index, ctx, source=segment.text, extra=context))

    validated_results = {}
    round_number, round_start = 0, 0.0

    def validate(item_id, row):
        segment, key = plans[item_id]
        result = response_events(row, segment, ctx, key, split_enabled)
        validated_results[item_id] = result
        return result

    def round_log(event, **data):
        nonlocal round_number, round_start
        if event == "request":
            round_number += 1
            round_start = time.monotonic()
            ids = ",".join(str(i) for i in data["ids"])
            message = f"request attempt={data['attempt']}/2 items={len(data['ids'])} ids=[{ids}] chars={data['chars']}"
        elif event == "response":
            message = f"response chars={data['chars']} elapsed={time.monotonic() - round_start:.2f}s"
        elif event == "validated":
            ids = data["accepted"]
            parts = ",".join(f"#{i}:{len(validated_results[i][1])}" for i in ids)
            fallback = sum(bool(validated_results[i][2]) for i in ids)
            split = sum(len(validated_results[i][1]) > 1 for i in ids)
            unsplit = sum(len(validated_results[i][1]) == 1 and not validated_results[i][2] for i in ids)
            failed = ",".join(str(i) for i in data["failed"])
            message = (f"validated accepted={len(ids)} failed={len(data['failed'])} "
                       f"events=[{parts}] split={split} unsplit={unsplit} whole_fallback={fallback} retry={str(data['retry']).lower()}")
            if failed:
                message += f" failed_ids=[{failed}]"
        else:
            message = f"{event} type={data['kind']} elapsed={time.monotonic() - round_start:.2f}s"
        level = "ERROR" if event == "error" else "WARNING" if event == "aborted" else "INFO"
        log(level, f'Translate LLM round {round_number}: {message} project={ctx.base!r}')
        if event == "validated":
            prefix = f"Translate LLM round {round_number}:"
            for item_id in data["accepted"]:
                whole, events, warning = validated_results[item_id]
                status = "whole_fallback" if warning else "split" if len(events) > 1 else "unsplit"
                log_events(plans[item_id][0], ctx, whole, events, status, prefix, warning=warning)
            for item_id in data["failed"]:
                status = "retry" if data["retry"] else "failed"
                log("WARNING", f'{prefix} sentence #{item_id} status={status} project={ctx.base!r}')
    log("INFO", f'Translate bilingual events: {len(pending)} pending sentence(s)')
    for start in range(0, len(pending), max(1, llm.batch_size)):
        accepted, errors = t.source_first_batch(llm, prompt, pending[start:start + max(1, llm.batch_size)], validate, admission_guard,
                                              round_log=round_log)
        for item_id, (whole, events, warning) in accepted.items():
            segment, key = plans[item_id]
            segment.translation, segment.split_events = whole, events
            segment.event_plan_key = segment.translation_key = key
            segment.proofread_key = ""
            segment.split_status = "ok" if len(events) > 1 else "unsplit"
            segment.split_reason = segment.split_reason_detail = ""
            changed = True
        if checkpoint and accepted:
            checkpoint()
        if errors:
            raise ValueError("bilingual translation failed: " + "; ".join(f"#{i}: {e}" for i, e in errors.items()))
    return changed
