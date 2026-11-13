import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import translate_srt as t
from subtitle_translation import source_review as review
from subtitle_translation.artifacts import atomic_write_json, json_sha256
from fakes import FakeLLM


class SourceReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "video.json"
        self.segment = t.TranscriptSegment(1, 0, 3, "I buy it yesterday.", words=[t.TranscriptWord("I", 0, 1)])
        self.transcript = t.Transcript(str(self.path), "en", [self.segment])
        t.save_transcript(self.transcript, str(self.path))
        self.ctx = t.TranscriptContext.from_json(str(self.path), source_lang="en", target_lang="zh")
        Path(self.ctx.glossary).write_text("# Terms\n\nA full resident glossary.", encoding="utf-8")
        self.llm = FakeLLM()
        self.calls = []

    def reply(self, responder):
        def ask(session, content, validator=None, **kwargs):
            payload = json.loads(content)
            self.calls.append((payload, session))
            answer = json.dumps(responder(payload))
            return answer, validator(answer)
        return patch.object(t.ChatSession, "ask_validated", autospec=True, side_effect=ask)

    def keep(self, payload):
        return {"items": [{"id": item["id"], "en": item["en"], "decision": "keep",
                           "basis": "candidate", "reason": ""} for item in payload["items"]]}

    def build(self, **kwargs):
        return t.build_source_review(self.transcript, self.ctx, self.llm, **kwargs)

    def test_review_publishes_linked_immutable_glossary_and_actual_text_without_modifying_words(self):
        original = copy.deepcopy(self.transcript.to_json())
        def edit(payload):
            row = self.keep(payload)
            row["items"][0].update(en="I bought it yesterday.", decision="edit", basis="readability", reason="Past tense.")
            return row
        with self.reply(edit):
            result = self.build()
        self.assertEqual(result["text"], "I bought it yesterday.")
        self.assertEqual(self.transcript.to_json(), original)
        pointer = self.root / "video.source_review.json"
        manifest = json.loads(pointer.read_text(encoding="utf-8"))
        snapshot = self.root / manifest["generation_dir"]
        self.assertEqual((snapshot / "glossary.md").read_text(encoding="utf-8"), Path(self.ctx.glossary).read_text(encoding="utf-8"))
        self.assertEqual(json_sha256(result), manifest["source_review_sha256"])
        self.assertEqual(self.calls[0][1].llm.request_timeout, 120)
        self.assertEqual(self.calls[0][1].llm.api_max_retries, 0)
        self.assertIn("A full resident glossary.", self.calls[0][1].system_prompt)

    def test_bad_response_has_actual_fallback_and_is_cached(self):
        with self.reply(lambda _: {"items": []}):
            result = self.build()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result["text"], self.segment.text)
        self.assertEqual(result["items"][0]["decision"], "fallback")
        with self.reply(lambda _: self.fail("matching dual artifacts must be reused")):
            self.assertEqual(self.build(), result)

    def test_terminology_file_alone_does_not_skip_source_review(self):
        with self.reply(self.keep):
            result = self.build()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result["items"][0]["decision"], "keep")

    def test_glossary_change_invalidates_review_without_changing_original_source(self):
        with self.reply(self.keep):
            first = self.build()
        Path(self.ctx.glossary).write_text("# New terminology", encoding="utf-8")
        with self.reply(self.keep):
            second = self.build()
        self.assertNotEqual(first["input_sha256"], second["input_sha256"])
        self.assertEqual(self.segment.text, "I buy it yesterday.")

    def test_force_rebuilds_review_even_with_matching_manifest(self):
        with self.reply(self.keep):
            first = self.build()
            second = self.build(force=True)
        self.assertEqual(first["input_sha256"], second["input_sha256"])
        self.assertNotEqual(first["generation"], second["generation"])
        self.assertEqual(len(self.calls), 2)

    def test_changed_glossary_during_request_does_not_publish_new_pointer(self):
        with self.reply(self.keep):
            self.build()
        pointer = self.root / "video.source_review.json"
        before = pointer.read_bytes()
        def change(payload):
            Path(self.ctx.glossary).write_text("changed concurrently", encoding="utf-8")
            return self.keep(payload)
        with self.reply(change), self.assertRaisesRegex(ValueError, "glossary changed"):
            self.build(force=True)
        self.assertEqual(pointer.read_bytes(), before)

    def test_changed_source_during_request_does_not_publish_pointer(self):
        def change(payload):
            self.path.write_text('{"segments": []}', encoding="utf-8")
            return self.keep(payload)
        with self.reply(change), self.assertRaisesRegex(ValueError, "input changed"):
            self.build()
        self.assertFalse((self.root / "video.source_review.json").exists())

    def test_candidate_not_available_in_input_cannot_be_claimed_as_an_edit_basis(self):
        unit = review.SourceUnit(1, "original", 0, 1, "whisperx:1")
        invalid = [
            {"en": "new", "decision": "keep", "basis": "candidate", "reason": ""},
            {"en": "new", "decision": "edit", "basis": "candidate", "reason": "guess"},
            {"en": "new", "decision": "edit", "basis": "context", "reason": ""},
            {"en": [], "decision": "edit", "basis": "context", "reason": "guess"},
            {"en": "", "decision": "keep", "basis": "candidate", "reason": ""},
        ]
        for row in invalid:
            with self.subTest(row=row), self.assertRaises(ValueError):
                review.validate_review({"id": 1, **row}, unit, "en")

    def test_invalid_or_corrupt_snapshot_cannot_be_loaded_as_a_completed_review(self):
        with self.reply(self.keep):
            result = self.build()
        pointer = self.root / "video.source_review.json"
        manifest = json.loads(pointer.read_text(encoding="utf-8"))
        snapshot = self.root / manifest["generation_dir"]
        (snapshot / "source_review.json").write_text("{}", encoding="utf-8")
        units = [review.SourceUnit(1, self.segment.text, 0, 3, "whisperx:1")]
        self.assertIsNone(review.load_review(pointer, result["input_sha256"], Path(self.ctx.glossary).read_text(encoding="utf-8"), units))
        manifest["generation_dir"] = "../outside"
        atomic_write_json(pointer, manifest)
        self.assertIsNone(review.load_review(pointer, result["input_sha256"], "glossary", units))

    def test_source_review_requires_unique_nonempty_input_and_valid_ranges(self):
        for units in ([review.SourceUnit(1, "", 0, 1, "w:1")],
                      [review.SourceUnit(1, "text", 2, 1, "w:1")],
                      [review.SourceUnit(1, "a", 0, 1, "w:1"), review.SourceUnit(1, "b", 1, 2, "w:2")]):
            with self.subTest(units=units), self.assertRaises(ValueError):
                review.review_input_identity(units, "en", "terms", {})

    def test_full_draft_does_not_treat_asr_units_as_paragraph_boundaries(self):
        items = [{"id": 1, "text": "This is"}, {"id": 2, "text": "one sentence."}]
        text, ranges = review.compose_adopted_text(items, "en")
        self.assertEqual(text, "This is one sentence.")
        for item, mapping in zip(items, ranges):
            start, end = mapping["range"]
            self.assertEqual(text[start:end], item["text"])
        self.assertEqual(ranges[1]["range"][0], len("This is "))

    def test_partial_invalid_review_retries_only_bad_sentence_and_preserves_good_text(self):
        self.transcript.segments.append(t.TranscriptSegment(2, 3, 4, "A joke, unchanged."))
        t.save_transcript(self.transcript, str(self.path))
        def respond(payload):
            response = self.keep(payload)
            response["items"] = [item for item in response["items"] if item["id"] != 2]
            return response
        with self.reply(respond):
            result = self.build()
        self.assertEqual([item["decision"] for item in result["items"]], ["keep", "fallback"])
        self.assertEqual(result["items"][1]["text"], "A joke, unchanged.")
        self.assertEqual([item["id"] for item in self.calls[1][0]["items"]], [2])

    def test_cancellation_does_not_publish_fallback_or_repeat_request(self):
        with patch.object(t.ChatSession, "ask_validated", side_effect=KeyboardInterrupt) as request:
            with self.assertRaises(KeyboardInterrupt):
                self.build()
        self.assertEqual(request.call_count, 1)
        self.assertFalse((self.root / "video.source_review.json").exists())

    def test_only_glossary_publishes_review_and_stops_before_translation_or_alignment(self):
        with patch.object(t, "load_env", return_value={"GLOSSARY_PROVIDER": "fake"}), \
             patch.object(t, "glossary_llm_from_env", return_value=self.llm), \
             patch.object(t, "build_glossary", return_value=self.ctx.glossary), \
             patch("subtitle_translation.bilingual_translation.translate_events") as translate, self.reply(self.keep):
            result = t.main([str(self.path), "--only-glossary"], stage_overrides={"PIPELINE_SKIP_SCENE_DETECTION": "1", })
        self.assertIsNone(result)
        self.assertTrue((self.root / "video.source_review.json").is_file())
        translate.assert_not_called()
        self.assertEqual(t.load_transcript(str(self.path)).segments[0].text, self.segment.text)

    def test_batch_reuse_glossary_does_not_enable_unaligned_review_in_the_active_pipeline(self):
        with patch.object(t, "load_env", return_value={"GLOSSARY_PROVIDER": "fake"}), \
             patch.object(t, "glossary_llm_from_env", return_value=self.llm), \
             patch.object(t, "build_glossary", return_value=self.ctx.glossary), \
             patch.object(t, "build_source_review") as stage:
            t.main([str(self.path), "--only-glossary"], stage_overrides={"GLOSSARY_REUSE": "1", "PIPELINE_SKIP_SCENE_DETECTION": "1", })
        stage.assert_not_called()

    def test_review_llm_timeout_and_retry_budget_do_not_modify_default_client_policy(self):
        llm = t.LLMConfig("fake", api_key="test-placeholder", request_timeout=120, api_max_retries=0)
        with patch.object(t.LLMConfig, "cfg", return_value={"url": "https://example.invalid/v1"}), \
             patch.object(t, "OpenAI") as client:
            llm._client()
        self.assertEqual(client.call_args.kwargs["timeout"], 120)
        self.assertEqual(client.call_args.kwargs["max_retries"], 0)
        default = t.LLMConfig("fake", api_key="test-placeholder")
        with patch.object(t.LLMConfig, "cfg", return_value={"url": "https://example.invalid/v1"}), \
             patch.object(t, "OpenAI") as client:
            default._client()
        self.assertNotIn("timeout", client.call_args.kwargs)
        self.assertNotIn("max_retries", client.call_args.kwargs)

    def test_source_review_timeout_cannot_be_overridden_by_provider_request_kwargs(self):
        self.llm.request_timeout = 120
        create = Mock()
        self.llm._client = lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(self.llm, "cfg", return_value={"request_kwargs": {"timeout": 999}}):
            t.ChatSession(self.llm, "style").create()
        self.assertEqual(create.call_args.kwargs["timeout"], 120)


if __name__ == "__main__":
    unittest.main()
