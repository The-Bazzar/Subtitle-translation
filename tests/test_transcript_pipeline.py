import copy
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import translate_srt as t
from subtitle_translation import bilingual_translation as bt
from fakes import FakeLLM


class TranscriptPipelineTests(unittest.TestCase):
    def setUp(self):
        self.ctx = t.TranscriptContext.from_json("video.json", source_lang="en", target_lang="zh")
        self.llm = FakeLLM()
        self.segment = t.TranscriptSegment(
            1, 0.0, 4.0, "Hello, nice day!",
            words=[t.TranscriptWord("Hello,", 0.0, 0.8),
                   t.TranscriptWord("nice", 1.0, 2.0), t.TranscriptWord("day!", 2.5, 4.0)],
        )
        self.transcript = t.Transcript("video.json", "en", [self.segment])
        self.calls = []


    def reply(self, responder):
        def ask(session, content, validator=None, retry_template=None, **kwargs):
            payload = json.loads(content)
            self.calls.append((copy.deepcopy(payload), session.system_prompt))
            answer = json.dumps(responder(payload), ensure_ascii=False)
            return answer, validator(answer)
        return patch.object(t.ChatSession, "ask_validated", autospec=True, side_effect=ask)


    def test_unknown_duplicate_or_noninteger_sentence_id_is_rejected(self):
        for rows in ([{"id": 1}, {"id": 1}], [{"id": 2}], [{"id": True}], [{"id": "1"}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                t.source_first_rows(json.dumps({"items": rows}), [1])


    def test_main_has_no_separate_split_call_and_does_not_export_on_translation_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "video.json")
            t.save_transcript(self.transcript, path)
            order = []
            with patch.object(t, "load_env", return_value={"TRANSLATE_PROVIDER": "fake"}), \
                 patch.object(t, "translate_llm_from_env", return_value=self.llm), \
                 patch.object(bt, "translate_events", side_effect=lambda *a, **k: order.append("translate") or (_ for _ in ()).throw(ValueError("bad translation"))), \
                 patch.object(t, "write_ass") as export:
                code = t.main([path], stage_overrides={"PIPELINE_SKIP_SCENE_DETECTION": "1", "PIPELINE_SKIP_KNOWLEDGE": "1", "PROOFREAD": "0", })
            self.assertEqual(code, 1)
            self.assertEqual(order, ["translate"])
            export.assert_not_called()


    def test_beautified_cache_requires_matching_source_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "video.json")
            t.save_transcript(self.transcript, source_path)
            ctx = t.TranscriptContext.from_json(source_path, source_lang="en", target_lang="zh")
            self.transcript.raw["_source_first_input"] = "old-generation"
            self.transcript.segments[0].text = "stale source"
            t.save_transcript(self.transcript, ctx.beautified_json)
            fresh = t.load_transcript(source_path)
            result = t.load_or_create_beautified(ctx, fresh, "", t.SceneDetectionOptions(), True, False, source_identity="new-generation")
            self.assertEqual(result.segments[0].text, "Hello, nice day!")
            self.assertEqual(result.raw["_source_first_input"], "new-generation")


    def test_corrupt_work_cache_is_rebuilt_from_original(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "video.json")
            t.save_transcript(self.transcript, source_path)
            ctx = t.TranscriptContext.from_json(source_path, source_lang="en", target_lang="zh")
            with open(ctx.beautified_json, "w", encoding="utf-8") as file:
                file.write('{"segments": [')
            result = t.load_or_create_beautified(
                ctx, self.transcript, "", t.SceneDetectionOptions(), True, False, source_identity="new-generation",
            )
            self.assertEqual(result.segments[0].text, "Hello, nice day!")
            self.assertEqual(t.load_transcript(ctx.beautified_json).segments[0].text, result.segments[0].text)


    def test_failed_atomic_save_preserves_existing_cache_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = os.path.join(directory, "video.json")
            t.save_transcript(self.transcript, destination)
            with open(destination, encoding="utf-8") as file:
                before = file.read()
            self.segment.translation = "new translation"
            with patch.object(t.os, "replace", side_effect=PermissionError("locked")):
                with self.assertRaises(PermissionError):
                    t.save_transcript(self.transcript, destination)
            with open(destination, encoding="utf-8") as file:
                self.assertEqual(file.read(), before)
            self.assertEqual(os.listdir(directory), ["video.json"])


    def test_main_exports_two_bilingual_events_from_one_translation_response(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "video.json")
            template_path = os.path.join(directory, "template.ass")
            t.save_transcript(self.transcript, source_path)
            with open(template_path, "w", encoding="utf-8") as file:
                file.write("[Script Info]\nTitle: Test\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
            def respond(payload):
                return {"items": [{"id": 1, "zh": "Whole translation.", "split_events": [
                    {"en": "Hello,", "zh": "First part"}, {"en": "nice day!", "zh": "Second part"}]}]}
            with patch.object(t, "load_env", return_value={"TRANSLATE_PROVIDER": "fake"}), \
                 patch.object(t, "translate_llm_from_env", return_value=self.llm), \
                 patch.object(t, "load_prompt", return_value="style"), self.reply(respond):
                code = t.main([source_path, "--template", template_path], stage_overrides={"PIPELINE_SKIP_SCENE_DETECTION": "1", "PIPELINE_SKIP_KNOWLEDGE": "1", "PROOFREAD": "0", })
            self.assertIsNone(code)
            self.assertEqual(len(self.calls), 1)
            with open(os.path.join(directory, "video.en-zh.ass"), encoding="utf-8") as file:
                ass = file.read()
            self.assertEqual(ass.count("Dialogue:"), 2)
            saved = t.load_transcript(os.path.join(directory, "video.beautified.json"))
            self.assertTrue(saved.segments[0].translation_key)
            self.assertEqual(len(saved.segments[0].split_events), 2)


    def test_main_exports_whole_bilingual_event_when_source_excerpts_cannot_be_located(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = os.path.join(directory, "video.json")
            template_path = os.path.join(directory, "template.ass")
            t.save_transcript(self.transcript, source_path)
            with open(template_path, "w", encoding="utf-8") as file:
                file.write("[Script Info]\nTitle: Test\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
            def respond(payload):
                return {"items": [{"id": 1, "zh": "Whole translation with a preserved space!", "split_events": [
                    {"en": "Changed source", "zh": "Part"}]}]}
            stderr = io.StringIO()
            with patch.object(t, "load_env", return_value={"TRANSLATE_PROVIDER": "fake"}), \
                 patch.object(t, "translate_llm_from_env", return_value=self.llm), \
                 patch.object(t, "load_prompt", return_value="style"), \
                 patch("sys.stderr", stderr), self.reply(respond):
                code = t.main([source_path, "--template", template_path], stage_overrides={"PIPELINE_SKIP_SCENE_DETECTION": "1", "PIPELINE_SKIP_KNOWLEDGE": "1", "PROOFREAD": "0", })
            self.assertIsNone(code)
            self.assertEqual(len(self.calls), 1)
            with open(os.path.join(directory, "video.en-zh.ass"), encoding="utf-8") as file:
                ass = file.read()
            self.assertEqual(ass.count("Dialogue:"), 1)
            self.assertIn("Hello, nice day!", ass)
            self.assertIn("Whole translation with a preserved space!", ass)
            saved = t.load_transcript(os.path.join(directory, "video.beautified.json"))
            self.assertEqual(len(saved.segments[0].split_events), 1)
            self.assertIn("keeping whole bilingual sentence", stderr.getvalue())
