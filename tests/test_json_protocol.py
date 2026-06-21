import json
import os
import tempfile
import unittest
from unittest.mock import patch

import translate_srt as t


class JsonProtocolTests(unittest.TestCase):
    def setUp(self):
        self.ctx = t.TranscriptContext.from_json("video.json", "", "en", "zh")

    def test_extract_json_object_rejects_array_shape(self):
        data = t._extract_json_value('{"markdown": "# Glossary"}')
        self.assertEqual(data, {"markdown": "# Glossary"})

    def test_glossary_output_parses_json_object(self):
        result = t.GlossaryOutput.from_json_content('{"markdown": "# 术语知识库"}')
        self.assertEqual(result.markdown, "# 术语知识库")

    def test_glossary_output_accepts_plain_markdown_fallback(self):
        result = t.GlossaryOutput.from_json_content("# 术语知识库\n\n## 核心术语")
        self.assertEqual(result.markdown, "# 术语知识库\n\n## 核心术语")

    def test_batch_response_accepts_wrapped_items(self):
        response = t.LLMBatchResponse.from_json_value({"items": [{"id": 1, "zh": "译文"}]})
        self.assertEqual(response.to_items(), [{"id": 1, "zh": "译文"}])

    def test_batch_response_keeps_legacy_array_compatibility(self):
        response = t.LLMBatchResponse.from_json_value([{"id": 1, "zh": "译文"}])
        self.assertEqual(response.to_items(), [{"id": 1, "zh": "译文"}])

    def test_batch_request_serializes_to_items_object(self):
        request = t.LLMBatchRequest([t.LLMBatchItem(7, {"en": "source"})])
        self.assertEqual(request.to_json_value(), {"items": [{"id": 7, "en": "source"}]})

    def test_typed_translate_item_serializes_language_key(self):
        item = t.TranslateInputItem(7, "source text", self.ctx)
        self.assertEqual(item.to_json_value(), {"id": 7, "en": "source text"})

    def test_typed_split_result_parses_language_arrays(self):
        result = t.SplitOutputItem.from_json_value(
            {"id": 12, "en": ["a", "b"], "zh": ["甲", "乙"]},
            self.ctx,
        )
        self.assertEqual(result.id, 12)
        self.assertEqual(result.source_parts, ["a", "b"])
        self.assertEqual(result.target_parts, ["甲", "乙"])

    def test_typed_split_input_serializes_context(self):
        item = t.SplitInputItem(
            12,
            "current source",
            "current target",
            self.ctx,
            context_before=[t.SplitContextItem(11, "before source", "before target", self.ctx)],
            context_after=[t.SplitContextItem(13, "after source", "after target", self.ctx)],
        )

        data = item.to_json_value()
        self.assertEqual(data["id"], 12)
        self.assertEqual(data["en"], "current source")
        self.assertEqual(data["zh"], "current target")
        self.assertEqual(data["context_before"], [{"id": 11, "en": "before source", "zh": "before target"}])
        self.assertEqual(data["context_after"], [{"id": 13, "en": "after source", "zh": "after target"}])

    def test_typed_proofread_result_parses_language_values(self):
        result = t.ProofreadOutputItem.from_json_value(
            {"id": 3, "en": "corrected source", "zh": "corrected target"},
            self.ctx,
        )
        self.assertEqual(result.source_text, "corrected source")
        self.assertEqual(result.target_text, "corrected target")

    def test_chat_session_passes_provider_response_format(self):
        calls = []

        class FakeMessage:
            content = '{"markdown": "ok"}'

        class FakeChoice:
            message = FakeMessage()

        class FakeResponse:
            choices = [FakeChoice()]

        class FakeCompletions:
            def create(self, **kwargs):
                calls.append(kwargs)
                return FakeResponse()

        class FakeChat:
            completions = FakeCompletions()

        class FakeClient:
            chat = FakeChat()

        class FakeLLM:
            def model_name(self):
                return "fake-model"

            def cfg(self):
                return {"response_format": {"type": "json_object"}}

            def _client(self):
                return FakeClient()

        t.ChatSession(FakeLLM(), "system").ask("{}", max_tokens=128)

        self.assertEqual(calls[0]["response_format"], {"type": "json_object"})

    def test_response_keys_match_language_codes_only(self):
        source_candidates = t.response_key_candidates(self.ctx, "source")
        target_candidates = t.response_key_candidates(self.ctx, "target")

        self.assertEqual(t.get_language_keyed_value({"en": "source text"}, source_candidates), "source text")
        self.assertEqual(t.get_language_keyed_value({"zh": "target text"}, target_candidates), "target text")
        self.assertIsNone(t.get_language_keyed_value({"source": "legacy"}, source_candidates))
        self.assertIsNone(t.get_language_keyed_value({"target": "legacy"}, target_candidates))

    def test_parse_split_response_aligns_by_actual_ids(self):
        source, target, error = t.parse_split_response(
            [
                {"id": 12, "en": ["source a", "source b"], "zh": ["target a", "target b"]},
                {"id": 44, "en": ["source c"], "zh": ["target c"]},
            ],
            [12, 44],
            self.ctx,
        )

        self.assertEqual(error, "")
        self.assertEqual(source[12], ["source a", "source b"])
        self.assertEqual(target[44], ["target c"])

    def test_parse_split_response_keeps_other_ids_when_one_item_has_part_count_mismatch(self):
        source, target, error = t.parse_split_response(
            [
                {"id": 73, "en": ["source a"], "zh": ["target a"]},
                {"id": 74, "en": ["source b"], "zh": ["target b"]},
                {"id": 88, "en": ["source c", "source d"], "zh": ["target c"]},
            ],
            [73, 74, 88],
            self.ctx,
        )

        self.assertEqual(error, "")
        self.assertEqual(source[73], ["source a"])
        self.assertEqual(target[74], ["target b"])
        self.assertEqual(source[88], ["source c", "source d"])
        self.assertEqual(target[88], ["target c"])

    def test_parse_proofread_response_aligns_by_actual_ids(self):
        pairs = t.parse_proofread_response(
            [{"id": 3, "en": "corrected source", "zh": "corrected target"}],
            [3],
            [("fallback source", "fallback target")],
            self.ctx,
        )

        self.assertEqual(pairs, [("corrected source", "corrected target")])

    def test_get_scene_changes_handles_missing_ffmpeg_stderr(self):
        class FakeCompletedProcess:
            stderr = None

        with tempfile.NamedTemporaryFile(suffix=".mp4") as video:
            with patch.object(t.subprocess, "run", return_value=FakeCompletedProcess()):
                self.assertEqual(t.get_scene_changes(video.name, 0.15, 0.1, quiet=True), [])

    def test_transcript_segment_round_trips_split_status(self):
        seg = t.TranscriptSegment.from_json(
            1,
            {
                "id": 1,
                "start": 0.0,
                "end": 1.0,
                "text": "source",
                "split_status": "fallback",
                "split_reason": "token_reconstruct_failed",
                "split_reason_detail": "test detail",
                "split_events": [{"start": 0.0, "end": 1.0, "en": "source", "zh": "target"}],
            },
        )

        data = seg.to_json()
        self.assertEqual(data["split_status"], "fallback")
        self.assertEqual(data["split_reason"], "token_reconstruct_failed")
        self.assertEqual(data["split_reason_detail"], "test detail")

    def test_translate_description_writes_metadata_header_and_translated_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = os.path.join(tmp, "video.beautified.json")
            open(json_path, "w", encoding="utf-8").close()
            ctx = t.TranscriptContext.from_json(json_path, "", "en", "zh")

            with open(ctx.info_json, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "title": "Original Title",
                        "webpage_url": "https://example.test/watch",
                        "uploader": "Original Channel",
                        "upload_date": "20260620",
                    },
                    f,
                )
            with open(ctx.desc, "w", encoding="utf-8") as f:
                f.write("Original description.")
            with open(ctx.tags, "w", encoding="utf-8") as f:
                f.write("['AI', 'philosophy']")

            captured_request = {}

            def fake_llm_json_once(llm, system_prompt, request, max_tokens, temperature=0.3):
                captured_request.update(request.to_json_value())
                return {
                    "title": "译后标题",
                    "description": "译后简介。",
                    "tags": ["人工智能", "哲学"],
                }

            class FakeLLM:
                pass

            with patch.object(t, "llm_json_once", side_effect=fake_llm_json_once):
                result = t.translate_description(ctx, FakeLLM(), quiet=True)

            self.assertEqual(result, ctx.target_desc)
            self.assertEqual(captured_request["tags"], ["AI", "philosophy"])
            with open(ctx.target_desc, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn("译后标题\n\n", content)
            self.assertIn("原视频：https://example.test/watch\n", content)
            self.assertIn("原标题：Original Title\n", content)
            self.assertIn("原作者：Original Channel\n", content)
            self.assertIn("上传时间：2026-06-20\n", content)
            self.assertIn("=====\n\n译后简介。", content)
            self.assertIn("标签：人工智能, 哲学", content)

    def test_write_ass_uses_named_output_modes(self):
        template = os.path.abspath("template.ass")
        event = t.SplitEvent(1.0, 2.0, "source line", "目标行")

        with tempfile.TemporaryDirectory() as tmp:
            source_path = os.path.join(tmp, "source.ass")
            target_path = os.path.join(tmp, "target.ass")
            bilingual_path = os.path.join(tmp, "bilingual.ass")

            t.write_ass(source_path, template, "title", [event], t.AssOutputMode.SOURCE)
            t.write_ass(target_path, template, "title", [event], t.AssOutputMode.TARGET)
            t.write_ass(bilingual_path, template, "title", [event], t.AssOutputMode.BILINGUAL)

            with open(source_path, "r", encoding="utf-8") as f:
                source_ass = f.read()
            with open(target_path, "r", encoding="utf-8") as f:
                target_ass = f.read()
            with open(bilingual_path, "r", encoding="utf-8") as f:
                bilingual_ass = f.read()

        self.assertIn(",bi-en,,0,0,0,,source line", source_ass)
        self.assertNotIn(",bi-zh,,0,0,0,,目标行", source_ass)
        self.assertIn(",bi-zh,,0,0,0,,目标行", target_ass)
        self.assertNotIn(",zh,,0,0,0,,目标行", target_ass)
        self.assertNotIn(",bi-en,,0,0,0,,source line", target_ass)
        self.assertIn(",bi-en,,0,0,0,,source line", bilingual_ass)
        self.assertIn(",bi-zh,,0,0,0,,目标行", bilingual_ass)


if __name__ == "__main__":
    unittest.main()
