import copy
import io
import json
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import translate_srt as t
from subtitle_translation import windowed_mfa as w
from subtitle_translation import bilingual_translation as bt
from subtitle_translation.artifacts import atomic_write_json, file_sha256, text_sha256
from subtitle_translation.source_review import compose_adopted_text
from fakes import FakeLLM


class WindowedMfaTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.ctx = t.TranscriptContext.from_json(str(self.root / "video.json"), source_lang="en", target_lang="zh")
        self.audio = self.root / "video.wav"
        with wave.open(str(self.audio), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\0\0" * 16000 * 30)
        self.runtime = SimpleNamespace(version="3.4.3", work_directory=self.root / "tool-work")
        self.llm = FakeLLM()

    def review(self, texts, times):
        items = [{"id": i, "text": text, "decision": "keep", "source_refs": [f"whisperx:{i}"], "input_range": bounds}
                 for i, (text, bounds) in enumerate(zip(texts, times), 1)]
        text, ranges = compose_adopted_text(items, "en")
        return {"language": "en", "text": text, "text_sha256": text_sha256(text), "items": items,
                "source_map": ranges, "generation": "review"}

    def source(self, review):
        return w.text_source(review, t.Transcript(str(self.audio.with_suffix(".json")), "en", [], {"_batch_artifact": {"media_generation": "one"}}))

    def reply(self, responder):
        def ask(session, content, validator=None, **kwargs):
            answer = json.dumps(responder(json.loads(content)))
            return answer, validator(answer)
        return patch.object(t.ChatSession, "ask_validated", autospec=True, side_effect=ask)

    def planned(self, review):
        source = self.source(review)
        source.raw["_source_first_input"] = "text-input"
        for segment in source.segments:
            segment.event_plan_key = "fixture-plan"
            ranges = bt.source_ranges(segment.text, segment.text.split(), "en")
            segment.split_events = [t.SplitEvent(None, None, segment.text[a:b].strip(), "", f"fixture-{i}", (a, b))
                                    for i, (a, b) in enumerate(ranges)]
        return source

    def translate(self, source, mismatch=False):
        def reply(payload):
            return {"items": [{"id": item["id"], "zh": "whole" if mismatch else "".join("part" for _ in item["en"].split()),
                               "split_events": [{"en": "Unlocatable" if mismatch else word, "zh": "part"} for word in item["en"].split()]} for item in payload["items"]]}
        with self.reply(reply), patch("sys.stderr", io.StringIO()):
            bt.translate_events(source, self.ctx, self.llm, "style", )

    def align(self, source, review, response):
        with self.corpus_reply(response) as native, patch("sys.stderr", io.StringIO()):
            result = w.align_events(source, review, self.audio, self.runtime, None, self.ctx)
        return result, native

    def corpus_reply(self, response):
        def run(entries, runtime, work, tokenize, **kwargs):
            return {e["id"]: response(e["audio"], e["text"], runtime, work, tokenize, **kwargs) for e in entries}
        return patch.object(w, "align_window_corpus", side_effect=run)

    def test_no_asr_words_required_and_pending_events_cannot_be_exported(self):
        review = self.review(["Hello world."], [[10, 12]])
        source = self.planned(review)
        self.assertEqual(len(source.segments[0].split_events), 2)
        self.assertTrue(t.source_events_are_valid(source.segments[0]))
        event = source.segments[0].split_events[0].to_json()
        self.assertNotIn("start", event)
        self.assertEqual(event["timing_status"], "pending")
        self.assertIsNone(t.SplitEvent.from_json(event).start)
        with self.assertRaisesRegex(ValueError, "before timing"):
            t.all_events(source)
        self.translate(source)
        self.assertTrue(source.segments[0].translation)

    def test_cross_source_sentence_merges_windows_with_full_intervening_text(self):
        review = self.review(["Hello", "world. Good day."], [[10, 11], [11, 14]])
        source = self.source(review)
        windows = w.plan_windows(review, source.segments, 30)
        self.assertEqual(len(windows), 1)
        self.assertEqual(windows[0]["segments"], [1, 2])
        self.assertEqual((windows[0]["start_sample"], windows[0]["end_sample"]), (160000, 224000))
        self.assertEqual(review["text"][slice(*windows[0]["source_range"])], review["text"])

    def test_adjacent_independent_sentences_do_not_merge_and_limits_are_bounded(self):
        review = self.review(["Hello.", "Next."], [[0, 1], [1, 2]])
        self.assertEqual(len(w.plan_windows(review, self.source(review).segments, 30)), 2)
        review = self.review(["Hello", "world."], [[0, 30], [30, 90]])
        window = w.plan_windows(review, self.source(review).segments, 90)[0]
        self.assertFalse(window["eligible"])

    def test_exact_sample_offset_applied_once_and_translation_not_changed(self):
        review = self.review(["Hello", "world."], [[10, 11], [11, 12]])
        source = self.planned(review)
        self.translate(source)
        before = copy.deepcopy(source.to_json())
        def native(audio, text, *args, **kwargs):
            self.assertEqual(text, "Hello world.")
            with wave.open(str(audio), "rb") as file:
                self.assertEqual(file.getnframes(), 32000)
            return {"status": "available", "words": [{"word": "hello", "start": 0.1, "end": 0.4}, {"word": "world", "start": 0.6, "end": 0.9}]}
        result, native_mock = self.align(source, review, native)
        self.assertEqual(native_mock.call_count, 1)
        self.assertAlmostEqual(result.segments[0].split_events[0].start, 10.1)
        self.assertAlmostEqual(result.segments[0].split_events[1].end, 10.9)
        self.assertEqual(source.to_json(), before)
        self.assertEqual(result.segments[0].translation, source.segments[0].translation)
        self.assertEqual([e.id for e in result.segments[0].split_events], [e.id for e in source.segments[0].split_events])
        self.assertEqual(result.raw["_batch_artifact"], source.raw["_batch_artifact"])

    def test_translation_whole_fallback_precedes_native_event_projection(self):
        review = self.review(["Hello world."], [[10, 12]])
        source = self.planned(review)
        self.translate(source, mismatch=True)
        self.assertEqual(len(source.segments[0].split_events), 1)
        self.assertIsNone(source.segments[0].split_events[0].start)
        result, _ = self.align(source, review, lambda *a, **k: {"status": "available", "words": [
            {"word": "hello", "start": 0.1, "end": 0.4}, {"word": "world", "start": 0.6, "end": 0.9}]})
        self.assertEqual(len(result.segments[0].split_events), 1)
        self.assertEqual(result.segments[0].split_events[0].zh, "whole")

    def test_target_proofread_accepts_mfa_timing_and_coarse_merged_results(self):
        for mode in ("aligned", "whole_fallback", "coarse_merged"):
            with self.subTest(mode=mode):
                text = "Hello world." if mode != "coarse_merged" else "First sentence. Second sentence."
                review = self.review([text], [[10, 14]])
                source = self.planned(review)
                self.translate(source, mismatch=mode == "whole_fallback")
                def native(audio, text, *args, **kwargs):
                    if mode == "coarse_merged":
                        return {"status": "unavailable", "reason": "fixture_failure"}
                    return {"status": "available", "words": [
                        {"word": "hello", "start": .1, "end": .4},
                        {"word": "world", "start": .6, "end": .9}]}
                result, _ = self.align(source, review, native)
                before = copy.deepcopy(result.to_json())
                def respond(payload):
                    return {"items": [{"id": row["id"], "zh": "Revised whole.", "split_events": [
                        {"id": event["id"], "zh": "Revised part."} for event in row["split_events"]
                    ]} for row in payload["items"]]}
                with self.reply(respond):
                    self.assertTrue(t.proofread_target_events(result, self.ctx, self.llm, "style", ))
                after = result.to_json()
                for old, new in zip(before["segments"], after["segments"]):
                    self.assertEqual(new["translation"], "Revised whole.")
                    self.assertEqual(len(old["split_events"]), len(new["split_events"]))
                    old.pop("translation")
                    new.pop("translation")
                    new.pop("proofread_key")
                    for old_event, new_event in zip(old["split_events"], new["split_events"]):
                        self.assertEqual(new_event.pop("zh"), "Revised part.")
                        old_event.pop("zh")
                    self.assertEqual(old, new)
                with self.reply(lambda _: self.fail("proofreading cache must accept MFA output")):
                    self.assertFalse(t.proofread_target_events(result, self.ctx, self.llm, "style", ))
                stored = self.root / "proofread.json"
                t.save_transcript(result, str(stored))
                restored = t.load_transcript(str(stored))
                with self.reply(lambda _: self.fail("persisted proofreading must remain cached")):
                    self.assertFalse(t.proofread_target_events(restored, self.ctx, self.llm, "style", ))

    def test_failed_window_does_not_shift_next_window_or_restore_old_words(self):
        review = self.review(["Hello.", "Next."], [[10, 11], [20, 21]])
        source = self.planned(review)
        self.translate(source)
        def native(audio, text, *args, **kwargs):
            if text == "Hello.":
                return {"status": "unavailable", "reason": "mfa_failed"}
            return {"status": "available", "words": [{"word": "next", "start": 0.2, "end": 0.6}]}
        result, _ = self.align(source, review, native)
        self.assertEqual(result.segments[0].words, [])
        self.assertEqual((result.segments[0].start, result.segments[0].end), (10, 11))
        self.assertAlmostEqual(result.segments[1].split_events[0].start, 20.2)

    def test_long_gap_or_outside_words_cannot_be_accepted_as_healthy(self):
        review = self.review(["Hello world."], [[10, 20]])
        source = self.planned(review)
        self.translate(source)
        for end in (9.3, 11):
            with self.subTest(end=end):
                result, _ = self.align(source, review, lambda *a, **k: {"status": "available", "words": [
                    {"word": "hello", "start": 0.1, "end": 0.4}, {"word": "world", "start": 9, "end": end}]})
                self.assertEqual(len(result.segments[0].split_events), 1)
                self.assertEqual(result.segments[0].segment_timing["quality"], "coarse")
                self.assertEqual((result.segments[0].start, result.segments[0].end), (10, 20))

    def test_text_checkpoint_roundtrip_preserves_translation_without_timing(self):
        review = self.review(["Hello world."], [[10, 12]])
        raw = t.Transcript(str(self.audio.with_suffix(".json")), "en", [], {})
        source, checkpoint = w.load_text_checkpoint(raw, review, self.ctx)
        self.translate(source)
        t.save_transcript(source, str(checkpoint))
        cached, _ = w.load_text_checkpoint(raw, review, self.ctx)
        self.assertEqual(cached.to_json(), source.to_json())
        with self.reply(lambda _: self.fail("valid text translation must be cached")):
            self.assertFalse(bt.translate_events(cached, self.ctx, self.llm, "style", ))

    def test_beautify_preserves_event_content_ids_and_does_not_touch_words(self):
        review = self.review(["Hello world."], [[10, 12]])
        source = self.planned(review)
        self.translate(source)
        def native(*args, **kwargs):
            return {"status": "available", "words": [{"word": "hello", "start": 0.1, "end": 0.4},
                                                       {"word": "world", "start": 0.6, "end": 0.9}]}
        with self.corpus_reply(native), \
             patch.object(t, "get_scene_changes", return_value=[10.05, 10.8]), \
             patch.object(t, "write_scene_change_sidecars"):
            result, marker, key = w.finish_timing(source, review, self.audio, self.runtime, None, self.ctx,
                str(self.audio), t.SceneDetectionOptions(), False, False, )
        self.assertEqual([e.id for e in result.segments[0].split_events], [e.id for e in source.segments[0].split_events])
        self.assertEqual([e.zh for e in result.segments[0].split_events], [e.zh for e in source.segments[0].split_events])
        self.assertAlmostEqual(result.segments[0].words[0].start, 10.1)
        self.assertTrue(t.source_events_are_valid(result.segments[0]))

    def test_timed_cache_requires_digest_and_does_not_mutate_text_checkpoint(self):
        review = self.review(["Hello world."], [[10, 12]])
        source = self.planned(review)
        self.translate(source)
        before = source.to_json()
        def native(*args, **kwargs):
            return {"status": "available", "words": [{"word": "hello", "start": 0.1, "end": 0.4},
                                                       {"word": "world", "start": 0.6, "end": 0.9}]}
        with self.corpus_reply(native):
            first, marker, key = w.finish_timing(source, review, self.audio, self.runtime, None, self.ctx,
                "", t.SceneDetectionOptions(), True, False, )
            marker.parent.mkdir(parents=True, exist_ok=True)
            t.save_transcript(first, self.ctx.beautified_json)
            atomic_write_json(marker, {"key": key, "sha256": file_sha256(self.ctx.beautified_json)})
            second, _, key2 = w.finish_timing(source, review, self.audio, self.runtime, None, self.ctx,
                "", t.SceneDetectionOptions(), True, False, )
        self.assertEqual(first.to_json(), second.to_json())
        self.assertEqual(key, key2)
        self.assertEqual(source.to_json(), before)

    def test_cancellation_is_not_converted_to_coarse_fallback(self):
        review = self.review(["Hello world."], [[10, 12]])
        source = self.planned(review)
        self.translate(source)
        before = source.to_json()
        with patch.object(w, "align_window_corpus", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            w.align_events(source, review, self.audio, self.runtime, None, self.ctx)
        self.assertEqual(source.to_json(), before)

    def test_boundary_inside_indivisible_word_span_falls_back_without_interpolation(self):
        review = self.review(["A part."], [[10, 12]])
        source = self.planned(review)
        self.translate(source)
        result, _ = self.align(source, review, lambda *a, **k: {"status": "available", "words": [
            {"word": "apart", "start": 0.1, "end": 0.9}]})
        self.assertEqual(result.segments[0].words, [])
        self.assertEqual(len(result.segments[0].split_events), 1)
        self.assertEqual(result.segments[0].translation, source.segments[0].translation)

    def test_project_budget_expiry_does_not_admit_native_alignment(self):
        review = self.review(["Hello."], [[10, 12]])
        source = self.planned(review)
        self.translate(source)
        with patch.object(w.time, "monotonic", side_effect=[0, 500, 500]), \
             patch.object(w, "align_window_corpus") as native, patch("sys.stderr", io.StringIO()):
            result = w.align_events(source, review, self.audio, self.runtime, None, self.ctx)
        native.assert_not_called()
        self.assertEqual(result.segments[0].segment_timing["quality"], "coarse")

    def test_many_independent_windows_share_one_native_corpus(self):
        review = self.review(["Hello.", "Next."], [[10, 11], [20, 21]])
        source = self.planned(review)
        self.translate(source)
        with self.corpus_reply(lambda audio, text, *a, **k: {"status": "available", "words": [
            {"word": text.lower().rstrip("."), "start": 0.1, "end": 0.5}]}) as native:
            result = w.align_events(source, review, self.audio, self.runtime, None, self.ctx)
        self.assertEqual(native.call_count, 1)
        self.assertEqual(len(native.call_args.args[0]), 2)
        self.assertAlmostEqual(result.segments[0].start, 10.1)
        self.assertAlmostEqual(result.segments[1].start, 20.1)

    def test_shared_failed_source_interval_emits_one_complete_bilingual_context(self):
        review = self.review(["First sentence. Second sentence.", "Later."], [[10, 14], [20, 21]])
        source = self.planned(review)
        self.translate(source)
        before = copy.deepcopy(source.to_json())
        def native(audio, text, *args, **kwargs):
            if "First" in text:
                return {"status": "unavailable", "reason": "fixture_failure"}
            return {"status": "available", "words": [{"word": "later", "start": .1, "end": .5}]}
        result, _ = self.align(source, review, native)
        self.assertEqual(len(result.segments), 2)
        first = result.segments[0]
        self.assertEqual(first.text, "First sentence. Second sentence.")
        self.assertEqual(first.translation, " ".join(s.translation for s in source.segments[:2]))
        self.assertEqual((first.start, first.end), (10, 14))
        self.assertEqual(len(first.split_events), 1)
        self.assertTrue(t.source_events_are_valid(first))
        self.assertLess(first.end, result.segments[1].start)
        self.assertEqual(source.to_json(), before)

    def test_stale_root_word_axis_removed_without_mutating_input_or_invalidating_text_cache(self):
        review = self.review(["Hello world."], [[10, 12]])
        original = t.Transcript(str(self.audio.with_suffix(".json")), "en", [], {"word_segments": [{"word": "old"}]})
        source, path = w.load_text_checkpoint(original, review, self.ctx)
        self.assertNotIn("word_segments", source.raw)
        self.assertIn("word_segments", original.raw)
        self.translate(source)
        source.raw["word_segments"] = [{"word": "legacy checkpoint"}]
        atomic_write_json(path, source.to_json())
        cached, _ = w.load_text_checkpoint(original, review, self.ctx)
        self.assertTrue(cached.segments[0].translation)
        self.assertNotIn("word_segments", cached.raw)
        t.save_transcript(original, str(self.root / "derived.json"))
        self.assertNotIn("word_segments", json.loads((self.root / "derived.json").read_text()))
        self.assertIn("word_segments", original.raw)

    def test_default_derived_cache_migrates_root_words_without_retranslation(self):
        source = t.Transcript(self.ctx.input_json, "en", [t.TranscriptSegment(1, 0, 1, "Hello.")])
        cached = copy.deepcopy(source.to_json())
        cached["word_segments"] = [{"word": "legacy"}]
        cached["_source_first_input"] = "same"
        cached["segments"][0]["translation"] = "Existing target"
        atomic_write_json(self.ctx.beautified_json, cached)
        result = t.load_or_create_beautified(self.ctx, source, "", t.SceneDetectionOptions(), True, False, source_identity="same")
        self.assertEqual(result.segments[0].translation, "Existing target")
        self.assertNotIn("word_segments", json.loads(Path(self.ctx.beautified_json).read_text()))

    def test_scene_detection_does_not_change_any_subtitle_timestamp(self):
        review = self.review(["Hello world."], [[10, 12]])
        source = self.planned(review)
        self.translate(source)
        native = lambda *a, **k: {"status": "available", "words": [
            {"word": "hello", "start": .1, "end": .4}, {"word": "world", "start": .6, "end": .9}]}
        with self.corpus_reply(native), patch.object(t, "get_frame_rate", return_value=24), \
             patch.object(t, "get_scene_changes", return_value=[10.05, 10.8]):
            first, _, _ = w.finish_timing(source, review, self.audio, self.runtime, None, self.ctx,
                str(self.audio), t.SceneDetectionOptions(), False, False, )
        self.assertEqual([(e.start, e.end) for e in first.segments[0].split_events], [(10.1, 10.4), (10.6, 10.9)])
        self.assertTrue(Path(self.ctx.scenes_json).is_file())
        self.assertTrue(Path(self.ctx.scenechange_txt).is_file())
        self.assertEqual(first.segments[0].words[0].start, 10.1)

    def test_removed_timeline_parameters_are_rejected(self):
        for flag in ("--snap-frames", "--end-offset-frames", "--min-duration", "--min-gap",
                     "--max-gap-merge", "--aggressive", "--no-scene-snap", "--only-beautify"):
            value = ["1"] if flag in {"--snap-frames", "--end-offset-frames", "--min-duration", "--min-gap", "--max-gap-merge"} else []
            with self.subTest(flag=flag), patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as caught:
                t.main(["missing.json", flag, *value])
            self.assertEqual(caught.exception.code, 2)

    def test_scene_settings_refresh_sidecars_without_modifying_subtitle_cache(self):
        def fresh():
            return t.Transcript(self.ctx.input_json, "en", [t.TranscriptSegment(1, 1, 1.4, "Hello.",
                words=[t.TranscriptWord("Hello.", 1, 1.4)], translation="Existing target")])
        video = self.root / "video.mkv"
        video.write_bytes(b"synthetic video")
        with patch.object(t, "get_frame_rate", return_value=24), \
             patch.object(t, "get_scene_changes", return_value=[1.1]) as detect:
            first = t.load_or_create_beautified(self.ctx, fresh(), str(video), t.SceneDetectionOptions(), False, False, source_identity="same")
            before = first.to_json()["segments"]
            t.load_or_create_beautified(self.ctx, fresh(), str(video), t.SceneDetectionOptions(), False, False, source_identity="same")
            self.assertEqual(detect.call_count, 1)
            last = t.load_or_create_beautified(self.ctx, fresh(), str(video), t.SceneDetectionOptions(scene_threshold=.2), False, False, source_identity="same")
        self.assertEqual(detect.call_count, 2)
        self.assertEqual(last.to_json()["segments"], before)
        self.assertEqual(last.segments[0].words[0].start, 1)
        self.assertEqual(last.segments[0].words[0].end, 1.4)


if __name__ == "__main__":
    unittest.main()
