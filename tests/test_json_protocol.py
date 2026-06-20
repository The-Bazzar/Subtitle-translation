import unittest

import translate_srt as t


class JsonProtocolTests(unittest.TestCase):
    def setUp(self):
        self.ctx = t.TranscriptContext.from_json("video.json", "", "en", "zh")

    def test_extract_json_object_rejects_array_shape(self):
        data = t._extract_json_value('{"markdown": "# Glossary"}')
        self.assertEqual(data, {"markdown": "# Glossary"})

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

    def test_parse_proofread_response_aligns_by_actual_ids(self):
        pairs = t.parse_proofread_response(
            [{"id": 3, "en": "corrected source", "zh": "corrected target"}],
            [3],
            [("fallback source", "fallback target")],
            self.ctx,
        )

        self.assertEqual(pairs, [("corrected source", "corrected target")])

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


if __name__ == "__main__":
    unittest.main()
