import os
import tempfile
import unittest
from unittest.mock import patch

import translate_srt as t


class ProofreadEvidenceConstraintTests(unittest.TestCase):
    """Small, offline checks for evidence-backed conservative proofreading."""

    def transcript(self, *lines):
        return t.Transcript(
            "sample.json",
            "en",
            [
                t.TranscriptSegment(index + 1, float(index), float(index + 1), line)
                for index, line in enumerate(lines)
            ],
        )

    @staticmethod
    def evidence_sidecar(*terms):
        return t.WebEvidenceSidecar(
            records=[
                t.WebEvidenceRecord(
                    query="official terminology",
                    results=[
                        t.WebEvidenceEntry(
                            url="https://example.test/official-terms",
                            title="Official terms",
                            content="Official terminology reference.",
                        )
                    ],
                )
            ],
            confirmed_terms=list(terms),
        )

    def test_confirmed_terms_and_sidecar_round_trip(self):
        sidecar = self.evidence_sidecar(
            t.ConfirmedTermEvidence(
                source="Northwind Protocol",
                target="诺斯风协议",
                source_variants=["Northwind"],
                kind="proper_name",
                evidence_urls=["https://example.test/official-terms"],
                note="Official Chinese title.",
            )
        )

        parsed = t.WebEvidenceSidecar.from_json_value(sidecar.to_json_value())

        self.assertEqual(parsed.version, 2)
        self.assertEqual(len(parsed.confirmed_terms), 1)
        self.assertEqual(
            parsed.confirmed_terms[0].to_json_value(),
            sidecar.confirmed_terms[0].to_json_value(),
        )

    def test_validated_terms_require_existing_url_and_certain_claim(self):
        transcript = self.transcript("Northwind Protocol is deployed.")
        sidecar = self.evidence_sidecar()
        raw_terms = [
            {
                "source": "Northwind Protocol",
                "target": "诺斯风协议",
                "confidence": "confirmed",
                "evidence_urls": ["https://example.test/official-terms"],
            },
            {
                "source": "Northwind Protocol",
                "target": "北风协议",
                "confidence": "confirmed",
                "evidence_urls": ["https://invented.example/claim"],
            },
            {
                "source": "Northwind Protocol",
                "target": "诺斯风协议(?)",
                "confidence": "high",
                "evidence_urls": ["https://example.test/official-terms"],
            },
            {
                "source": "Northwind Protocol",
                "target": "诺斯风协议",
                "confidence": "uncertain",
                "evidence_urls": ["https://example.test/official-terms"],
            },
        ]

        confirmed = t.validated_confirmed_terms(raw_terms, transcript, sidecar)

        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0].target, "诺斯风协议")
        self.assertEqual(confirmed[0].evidence_urls, ["https://example.test/official-terms"])

    def test_relevant_constraints_are_stable_and_passed_to_proofread_item(self):
        term = t.ConfirmedTermEvidence(
            source="Northwind Protocol",
            target="诺斯风协议",
            source_variants=["the Northwind Protocol"],
            kind="proper_name",
            evidence_urls=["https://example.test/official-terms"],
        )
        sidecar = self.evidence_sidecar(term)

        first_constraints, first_conflicts = t.relevant_term_evidence(
            "The Northwind Protocol failed.", sidecar
        )
        second_constraints, second_conflicts = t.relevant_term_evidence(
            "Northwind Protocol was restored.", sidecar
        )
        ctx = t.TranscriptContext.from_json("sample.json", "", "en", "zh")
        item = t.make_pair_item(
            1,
            ctx,
            "Northwind Protocol was restored.",
            "诺斯风协议已恢复。",
            terminology_constraints=second_constraints,
            evidence_conflicts=second_conflicts,
        ).to_json_value()

        self.assertFalse(first_conflicts)
        self.assertFalse(second_conflicts)
        self.assertEqual(first_constraints[0]["target"], "诺斯风协议")
        self.assertEqual(second_constraints[0], first_constraints[0])
        self.assertEqual(item["terminology_constraints"], second_constraints)
        self.assertNotIn("evidence_conflicts", item)

    def test_proofread_pipeline_injects_constraint_and_rejects_undeclared_override(self):
        class FakeLLM:
            provider = "fake"
            batch_size = 1

            def model_name(self):
                return "fake"

            def cfg(self):
                return {}

        captured = {}

        def fake_batch(request, session, quiet, retries=3, raise_on_failure=False):
            captured.update(request.to_json_value())
            return [{"id": 1, "en": "Northwind Protocol failed.", "zh": "北风协议失败了。"}]

        term = t.ConfirmedTermEvidence(
            source="Northwind Protocol",
            target="诺斯风协议",
            evidence_urls=["https://example.test/official-terms"],
        )
        event = t.SplitEvent(0.0, 1.0, "Northwind Protocol failed.", "诺斯风协议失败了。")
        transcript = t.Transcript(
            "sample.json",
            "en",
            [t.TranscriptSegment(1, 0.0, 1.0, event.en, split_events=[event])],
        )
        with tempfile.TemporaryDirectory() as tmp:
            ctx = t.TranscriptContext.from_json(os.path.join(tmp, "sample.json"), "", "en", "zh")
            t.write_web_evidence_sidecar(ctx, self.evidence_sidecar(term))
            with patch.object(t, "llm_numbered_batch", side_effect=fake_batch):
                changed = t.proofread_split_events(
                    transcript, ctx, FakeLLM(), "system", quiet=True, conservative=True
                )

        self.assertEqual(captured["items"][0]["terminology_constraints"][0]["target"], "诺斯风协议")
        self.assertFalse(changed)
        self.assertEqual(event.zh, "诺斯风协议失败了。")

    def test_explicit_mapping_keeps_acronyms_and_avoids_substring_false_hits(self):
        transcript = self.transcript("NASA launched the probe near the party venue.")
        sidecar = t.WebEvidenceSidecar(
            records=[
                t.WebEvidenceRecord(
                    query="names",
                    results=[
                        t.WebEvidenceEntry(
                            url="https://example.test/names",
                            content="NASA - 美国国家航空航天局; Art - 艺术",
                        )
                    ],
                )
            ]
        )

        pairs = {(source, target) for source, target, _url in t.explicit_web_term_mappings(transcript, sidecar)}

        self.assertIn(("NASA", "美国国家航空航天局"), pairs)
        self.assertNotIn(("Art", "艺术"), pairs)

    def test_shared_asr_variant_with_two_targets_is_a_conflict(self):
        sidecar = self.evidence_sidecar(
            t.ConfirmedTermEvidence(
                source="Baudrillard",
                target="鲍德里亚",
                source_variants=["bar Drill"],
                evidence_urls=["https://example.test/official-terms"],
            ),
            t.ConfirmedTermEvidence(
                source="Bar Dril",
                target="巴尔·德里尔",
                source_variants=["bar Drill"],
                evidence_urls=["https://example.test/official-terms"],
            ),
        )

        constraints, conflicts = t.relevant_term_evidence("bar Drill said this", sidecar)

        self.assertEqual(constraints, [])
        self.assertEqual(set(conflicts[0]["targets"]), {"鲍德里亚", "巴尔·德里尔"})

    def test_proofread_only_terms_do_not_enter_glossary_fingerprint_evidence(self):
        glossary_url = "https://example.test/glossary"
        proofread_url = "https://example.test/proofread"
        sidecar = t.WebEvidenceSidecar(
            records=[
                t.WebEvidenceRecord(
                    query="global",
                    search_stage="glossary_tool",
                    results=[t.WebEvidenceEntry(url=glossary_url, content="Global - 全局")],
                ),
                t.WebEvidenceRecord(
                    query="local",
                    search_stage="proofread_tool",
                    results=[t.WebEvidenceEntry(url=proofread_url, content="Local - 局部")],
                ),
            ],
            confirmed_terms=[
                t.ConfirmedTermEvidence("Global", "全局", evidence_urls=[glossary_url]),
                t.ConfirmedTermEvidence("Local", "局部", evidence_urls=[proofread_url]),
            ],
        )

        filtered = t.glossary_web_evidence(sidecar)

        self.assertEqual([record.search_stage for record in filtered.records], ["glossary_tool"])
        self.assertEqual([term.source for term in filtered.confirmed_terms], ["Global"])

    def test_reliable_canonical_translation_cannot_be_overwritten(self):
        constraints = [
            {
                "source": "Northwind Protocol",
                "target": "诺斯风协议",
                "source_variants": [],
            }
        ]

        source, target, review = t.apply_conservative_proofread_result(
            "The Northwind Protocol failed.",
            "诺斯风协议失败了。",
            "The Northwind Protocol failed.",
            "北风协议失败了。",
            {"target_changed": True, "categories": ["naturalness"], "reasons": ["wording"]},
            {},
            constraints,
        )

        self.assertEqual(source, "The Northwind Protocol failed.")
        self.assertEqual(target, "诺斯风协议失败了。")
        self.assertEqual(review, {})

    def test_unjustified_rewrite_is_reverted(self):
        source, target, review = t.apply_conservative_proofread_result(
            "This is deliberately formal.",
            "这刻意保持正式。",
            "This is casual now.",
            "现在随便说说。",
            None,
            {},
        )

        self.assertEqual(source, "This is deliberately formal.")
        self.assertEqual(target, "这刻意保持正式。")
        self.assertEqual(review, {})

    def test_conflicting_evidence_preserves_existing_text_and_requests_review(self):
        conflicts = [
            {
                "source": "Northwind",
                "targets": ["诺斯风", "北风"],
                "evidence_urls": ["https://example.test/official-terms"],
            }
        ]

        source, target, review = t.apply_conservative_proofread_result(
            "Northwind is ready.",
            "诺斯风已就绪。",
            "Northwind is ready.",
            "北风已就绪。",
            {"target_changed": True, "categories": ["terminology"], "reasons": ["evidence"]},
            {},
            [],
            conflicts,
        )

        self.assertEqual(source, "Northwind is ready.")
        self.assertEqual(target, "诺斯风已就绪。")
        self.assertTrue(review["needs_human"])
        self.assertIn("terminology", review["categories"])
        self.assertIn("未自动选择译名", review["reasons"][0])

    def test_no_evidence_degrades_to_justified_normal_proofreading(self):
        constraints, conflicts = t.relevant_term_evidence(
            "An ordinary sentence.", self.evidence_sidecar()
        )
        source, target, review = t.apply_conservative_proofread_result(
            "An ordinary sentence.",
            "这是一个普通句子。",
            "An ordinary sentence.",
            "这是一句普通的话。",
            {"target_changed": True, "categories": ["naturalness"], "reasons": ["more idiomatic"]},
            {},
            constraints,
            conflicts,
        )

        self.assertEqual(constraints, [])
        self.assertEqual(conflicts, [])
        self.assertEqual(source, "An ordinary sentence.")
        self.assertEqual(target, "这是一句普通的话。")
        self.assertEqual(review, {})


if __name__ == "__main__":
    unittest.main()
