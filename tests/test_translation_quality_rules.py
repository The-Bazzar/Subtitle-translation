import unittest

import translate_srt as t


class TranslationQualityRulesTests(unittest.TestCase):
    def setUp(self):
        self.zh_context = t.TranscriptContext.from_json(
            "video.json", "", "en", "zh"
        )

    def test_glossary_ui_translation_uses_exact_zh_mapping_only(self):
        translated = t.apply_glossary_ui_translation(
            "Rhetoric impossible.",
            "模型译文",
            [{"text": "| Rhetoric | 能说会道 |"}],
            self.zh_context,
        )
        self.assertEqual(translated, "[能说会道]：不可能")

        self.assertEqual(
            t.apply_glossary_ui_translation(
                "Rhetoric impossible.",
                "模型译文",
                [{"text": "| Other | 其他 |"}],
                self.zh_context,
            ),
            "模型译文",
        )

        non_zh_context = t.TranscriptContext.from_json("video.json", "", "en", "ja")
        self.assertEqual(
            t.apply_glossary_ui_translation(
                "Rhetoric impossible.",
                "モデル訳",
                [{"text": "| Rhetoric | 能说会道 |"}],
                non_zh_context,
            ),
            "モデル訳",
        )

    def test_retrieval_asr_evidence_forces_human_review_but_normal_context_does_not(self):
        source = "Got the farm?"
        flagged = t.merge_retrieval_review_evidence(
            source,
            {},
            [
                {
                    "text": (
                        "Current subtitle: Got the farm?; ASR transcription is 疑似/破损，"
                        "需对照音频确认。"
                    )
                }
            ],
        )
        self.assertTrue(flagged["needs_human"])
        self.assertIn("source_ASR", flagged["categories"])

        ordinary = t.merge_retrieval_review_evidence(
            source,
            {},
            [{"text": "Current subtitle: Got the farm?; this is ordinary context."}],
        )
        self.assertEqual(ordinary, {})

    def test_source_term_candidates_keep_short_named_phrase_and_rare_long_word(self):
        transcript = t.Transcript(
            "video.json",
            "en",
            [
                t.TranscriptSegment(1, 0.0, 1.0, "Rhetoric impossible."),
                t.TranscriptSegment(2, 1.0, 2.0, "We discuss electrochemistry today."),
                t.TranscriptSegment(3, 2.0, 3.0, "However this is only a sentence opener."),
            ],
        )

        candidates = t.transcript_source_term_candidates(transcript)

        self.assertIn("Rhetoric", candidates)
        self.assertIn("electrochemistry", candidates)
        self.assertNotIn("However", candidates)

    def test_explicit_web_term_mappings_support_two_formats_and_filter_noise(self):
        transcript = t.Transcript(
            "video.json",
            "en",
            [
                t.TranscriptSegment(1, 0.0, 1.0, "Rhetoric is impossible."),
                t.TranscriptSegment(2, 1.0, 2.0, "We discuss electrochemistry."),
            ],
        )
        sidecar = t.WebEvidenceSidecar(
            records=[
                t.WebEvidenceRecord(
                    query="terms",
                    results=[
                        t.WebEvidenceEntry(
                            url="https://example.test/terms",
                            content=(
                                "Rhetoric - 能说会道; "
                                "Electrochemistry（电化学）; "
                                "NOISE - 噪声; Unlisted - 不应保留."
                            ),
                        )
                    ],
                )
            ]
        )

        mappings = t.explicit_web_term_mappings(transcript, sidecar)
        mapping_pairs = {(source, target) for source, target, _ in mappings}

        self.assertIn(("Rhetoric", "能说会道"), mapping_pairs)
        self.assertIn(("Electrochemistry", "电化学"), mapping_pairs)
        self.assertNotIn(("NOISE", "噪声"), mapping_pairs)
        self.assertNotIn(("Unlisted", "不应保留"), mapping_pairs)

    def test_merge_explicit_web_term_mappings_is_idempotent_with_one_section(self):
        transcript = t.Transcript(
            "video.json",
            "en",
            [t.TranscriptSegment(1, 0.0, 1.0, "Rhetoric is impossible.")],
        )
        sidecar = t.WebEvidenceSidecar(
            records=[
                t.WebEvidenceRecord(
                    query="terms",
                    results=[
                        t.WebEvidenceEntry(
                            url="https://example.test/terms",
                            content="Rhetoric - 能说会道",
                        )
                    ],
                )
            ]
        )
        glossary = "# Glossary\n\n## 核心术语\n| 原文 | 译文 |\n|---|---|"

        once = t.merge_explicit_web_term_mappings(glossary, transcript, sidecar)
        twice = t.merge_explicit_web_term_mappings(once, transcript, sidecar)

        self.assertEqual(twice, once)
        self.assertEqual(twice.count("## 网页证据明确术语映射"), 1)
        self.assertEqual(twice.count("| Rhetoric | 能说会道 |"), 1)


if __name__ == "__main__":
    unittest.main()
