import unittest

import translate_srt as t


class JsonProtocolTests(unittest.TestCase):
    def setUp(self):
        self.ctx = t.TranscriptContext.from_json("video.json", "", "en", "zh")

    def test_extract_json_object_rejects_array_shape(self):
        data = t._extract_json_value('{"markdown": "# Glossary"}')
        self.assertEqual(data, {"markdown": "# Glossary"})

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


if __name__ == "__main__":
    unittest.main()
