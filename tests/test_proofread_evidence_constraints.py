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
        mappings = "; ".join(f"{term.source} - {term.target}" for term in terms)
        return t.WebEvidenceSidecar(
            records=[
                t.WebEvidenceRecord(
                    query="official terminology",
                    results=[
                        t.WebEvidenceEntry(
                            url="https://example.test/official-terms",
                            title="Official terms",
                            content=mappings or "Official terminology reference.",
                            preferred_domain_hit=True,
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
        sidecar = t.WebEvidenceSidecar(
            records=[
                t.WebEvidenceRecord(
                    query="official terminology",
                    results=[
                        t.WebEvidenceEntry(
                            url="https://example.test/official-terms",
                            title="Official terms",
                            content="Northwind Protocol - 诺斯风协议",
                            preferred_domain_hit=True,
                        )
                    ],
                )
            ]
        )
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
        item = t.make_proofread_item(
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
        self.assertTrue(changed)
        self.assertEqual(event.zh, "诺斯风协议失败了。")
        self.assertTrue(event.review["needs_human"])
        self.assertIn("proofread_safety_retry", event.review["categories"])

    def test_same_round_web_mapping_is_enriched_before_candidate_safety(self):
        class FakeLLM:
            provider = "fake"
            batch_size = 1

            def model_name(self):
                return "fake"

            def cfg(self):
                return {}

        event = t.SplitEvent(0.0, 1.0, "I saw bar Drill today.", "我今天看见了巴尔·德里尔。")
        transcript = t.Transcript(
            "sample.json", "en", [
                t.TranscriptSegment(1, 0.0, 1.0, event.en, split_events=[event])
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            ctx = t.TranscriptContext.from_json(os.path.join(tmp, "sample.json"), "", "en", "zh")
            runtime = t.WebSearchRuntime(settings=t.WebSearchSettings(tavily_key="test"), max_queries=1)

            def same_round_search(_request, _session, runtime_arg, _quiet):
                runtime_arg.sidecar = t.WebEvidenceSidecar(records=[t.WebEvidenceRecord(
                    query="Baudrillard Chinese name",
                    provider="tavily",
                    search_stage="proofread_tool",
                    results=[t.WebEvidenceEntry(
                        url="https://example.test/official-name",
                        content="Baudrillard - 鲍德里亚",
                        preferred_domain_hit=True,
                    )],
                )])
                return [{"id": 1, "en": "I saw Baudrillard today.", "zh": "我今天看见了鲍德里亚。"}]

            with patch.object(t, "llm_numbered_batch_with_web_search", side_effect=same_round_search):
                self.assertTrue(t.proofread_split_events(
                    transcript, ctx, FakeLLM(), "system", quiet=True,
                    enhanced=True, search_runtime=runtime, safety_mode=True,
                ))
            saved = t.load_web_evidence_sidecar(ctx.web_evidence_json)

        self.assertEqual(event.en, "I saw Baudrillard today.")
        self.assertEqual(event.zh, "我今天看见了鲍德里亚。")
        self.assertEqual(saved.confirmed_terms[0].source_variants, ["bar Drill"])

    def test_same_round_terms_are_reused_by_safety_retry(self):
        class FakeLLM:
            provider = "fake"
            batch_size = 1
            def model_name(self): return "fake"
            def cfg(self): return {}

        event = t.SplitEvent(0.0, 1.0, "Only bar Drill spoke.", "只有巴尔·德里尔说话。")
        transcript = t.Transcript("sample.json", "en", [t.TranscriptSegment(1, 0, 1, event.en, split_events=[event])])
        captured = {}
        with tempfile.TemporaryDirectory() as tmp:
            ctx = t.TranscriptContext.from_json(os.path.join(tmp, "sample.json"), "", "en", "zh")
            runtime = t.WebSearchRuntime(settings=t.WebSearchSettings(tavily_key="test"), max_queries=1)
            def tool_batch(_request, _session, runtime_arg, _quiet):
                runtime_arg.sidecar = t.WebEvidenceSidecar(records=[t.WebEvidenceRecord(
                    query="name", provider="tavily", search_stage="proofread_tool",
                    results=[t.WebEvidenceEntry(url="https://example.test/name", content="Baudrillard - 鲍德里亚", preferred_domain_hit=True)],
                )])
                return [{"id": 1, "en": "Only Baudrillard spoke.", "zh": "鲍德里亚说话。"}]
            def retry_batch(request, _session, _quiet, **_kwargs):
                captured.update(request.to_json_value()["items"][0])
                return [{"id": 1, "en": "Only Baudrillard spoke.", "zh": "只有鲍德里亚说话。"}]
            with patch.object(t, "llm_numbered_batch_with_web_search", side_effect=tool_batch), patch.object(t, "llm_numbered_batch", side_effect=retry_batch):
                self.assertTrue(t.proofread_split_events(transcript, ctx, FakeLLM(), "system", quiet=True, enhanced=True, search_runtime=runtime, safety_mode=True))
        self.assertEqual(captured["terminology_constraints"][0]["target"], "鲍德里亚")
        self.assertEqual(event.zh, "只有鲍德里亚说话。")

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
                            preferred_domain_hit=True,
                        )
                    ],
                )
            ]
        )

        pairs = {(source, target) for source, target, _url in t.explicit_web_term_mappings(transcript, sidecar)}

        self.assertIn(("NASA", "美国国家航空航天局"), pairs)
        self.assertNotIn(("Art", "艺术"), pairs)

    def test_real_url_without_mapping_content_is_not_confirmed(self):
        transcript = self.transcript("Northwind Protocol is deployed.")
        sidecar = t.WebEvidenceSidecar(
            records=[
                t.WebEvidenceRecord(
                    query="official terminology",
                    results=[
                        t.WebEvidenceEntry(
                            url="https://example.test/official-terms",
                            content="This page discusses deployment history only.",
                            preferred_domain_hit=True,
                        )
                    ],
                )
            ]
        )

        confirmed = t.validated_confirmed_terms(
            [{
                "source": "Northwind Protocol",
                "target": "诺斯风协议",
                "confidence": "confirmed",
                "evidence_urls": ["https://example.test/official-terms"],
            }],
            transcript,
            sidecar,
        )

        self.assertEqual(confirmed, [])
        self.assertTrue(transcript.segments[0].review["needs_human"])

    def test_unrelated_bilingual_pair_is_not_promoted(self):
        transcript = self.transcript("Northwind Protocol is deployed.")
        sidecar = t.WebEvidenceSidecar(
            records=[
                t.WebEvidenceRecord(
                    query="official terminology",
                    results=[
                        t.WebEvidenceEntry(
                            url="https://example.test/official-terms",
                            content="Southwind Manual - 南风手册",
                            preferred_domain_hit=True,
                        )
                    ],
                )
            ]
        )

        self.assertEqual(t.explicit_web_term_mappings(transcript, sidecar), [])
        self.assertEqual(transcript.segments[0].review, {})

    def test_conflicting_authoritative_targets_are_downgraded_to_review(self):
        transcript = self.transcript("Northwind Protocol is deployed.")
        sidecar = t.WebEvidenceSidecar(
            records=[
                t.WebEvidenceRecord(
                    query="official title one",
                    results=[t.WebEvidenceEntry(
                        url="https://one.example/terms",
                        content="Northwind Protocol - 诺斯风协议",
                        preferred_domain_hit=True,
                    )],
                ),
                t.WebEvidenceRecord(
                    query="official title two",
                    results=[t.WebEvidenceEntry(
                        url="https://two.example/terms",
                        content="Northwind Protocol - 北风协议",
                        preferred_domain_hit=True,
                    )],
                ),
            ]
        )

        self.assertEqual(t.explicit_web_term_mappings(transcript, sidecar), [])
        review = transcript.segments[0].review
        self.assertTrue(review["needs_human"])
        self.assertEqual(set(review["alternatives"]), {"诺斯风协议", "北风协议"})

    def test_forged_url_does_not_validate_an_otherwise_supported_mapping(self):
        transcript = self.transcript("Northwind Protocol is deployed.")
        sidecar = t.WebEvidenceSidecar(
            records=[
                t.WebEvidenceRecord(
                    query="official terminology",
                    results=[t.WebEvidenceEntry(
                        url="https://example.test/official-terms",
                        content="Northwind Protocol - 诺斯风协议",
                        preferred_domain_hit=True,
                    )],
                )
            ]
        )

        confirmed = t.validated_confirmed_terms(
            [{
                "source": "Northwind Protocol",
                "target": "诺斯风协议",
                "confidence": "confirmed",
                "evidence_urls": ["https://forged.example/claim"],
            }],
            transcript,
            sidecar,
        )

        self.assertEqual(confirmed, [])
        self.assertTrue(transcript.segments[0].review["needs_human"])

    def test_two_independent_sources_can_corroborate_a_mapping(self):
        transcript = self.transcript("Northwind Protocol is deployed.")
        sidecar = t.WebEvidenceSidecar(
            records=[
                t.WebEvidenceRecord(
                    query="title one",
                    results=[t.WebEvidenceEntry(
                        url="https://one.example/terms",
                        content="Northwind Protocol - 诺斯风协议",
                    )],
                ),
                t.WebEvidenceRecord(
                    query="title two",
                    results=[t.WebEvidenceEntry(
                        url="https://two.example/terms",
                        content="Northwind Protocol - 诺斯风协议",
                    )],
                ),
            ]
        )

        mappings = t.explicit_web_term_mappings(transcript, sidecar)

        self.assertEqual(len(mappings), 2)
        self.assertEqual({pair[:2] for pair in mappings}, {("Northwind Protocol", "诺斯风协议")})
        self.assertEqual(transcript.segments[0].review, {})

    def test_unsupported_target_language_keeps_raw_evidence_without_hard_term(self):
        transcript = self.transcript("Northwind Protocol is deployed.")
        url = "https://example.test/japanese-title"
        sidecar = t.WebEvidenceSidecar(
            records=[t.WebEvidenceRecord(
                query="official Japanese title",
                results=[t.WebEvidenceEntry(
                    url=url,
                    content="Northwind Protocol（ノースウィンド・プロトコル）",
                    preferred_domain_hit=True,
                )],
            )],
        )

        enriched = t.enrich_confirmed_term_evidence(
            transcript,
            sidecar,
            [{
                "source": "Northwind Protocol",
                "target": "ノースウィンド・プロトコル",
                "confidence": "confirmed",
                "evidence_urls": [url],
            }],
            source_lang="en",
            target_lang="ja",
        )

        self.assertFalse(t.supports_structured_web_term_promotion("en", "ja"))
        self.assertFalse(t.supports_structured_web_term_promotion("ja", "zh"))
        self.assertTrue(t.supports_structured_web_term_promotion("en", "zh"))
        self.assertEqual(enriched.confirmed_terms, [])
        self.assertEqual(enriched.records[0].results[0].content, sidecar.records[0].results[0].content)
        self.assertTrue(transcript.segments[0].review["needs_human"])

    def test_confirmed_mapping_remains_relevant_through_an_asr_variant(self):
        transcript = self.transcript("bar Drill spoke about simulation.")
        sidecar = t.WebEvidenceSidecar(
            records=[t.WebEvidenceRecord(
                query="official name",
                results=[t.WebEvidenceEntry(
                    url="https://example.test/official-name",
                    content="Baudrillard - 鲍德里亚",
                    preferred_domain_hit=True,
                )],
            )]
        )

        confirmed = t.validated_confirmed_terms(
            [{
                "source": "Baudrillard",
                "target": "鲍德里亚",
                "source_variants": ["bar Drill"],
                "confidence": "confirmed",
                "evidence_urls": ["https://example.test/official-name"],
            }],
            transcript,
            sidecar,
        )

        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0].source_variants, ["bar Drill"])
        self.assertEqual(transcript.segments[0].review, {})

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

    def test_non_en_zh_disables_only_semantic_anchor_and_keeps_term_safety(self):
        ctx = t.TranscriptContext.from_json("video.json", "", "ja", "ko")
        self.assertFalse(t.supports_en_zh_semantic_anchor_gate(ctx))
        source, target, review = t.apply_proofread_safety_constraints(
            "東京だけだ。",
            "도쿄뿐이다.",
            "東京だけだ。",
            "다른 곳도 있다.",
            {"target_changed": True, "categories": ["accuracy"], "reasons": ["test"]},
            {},
            terminology_constraints=[{"source": "東京", "target": "도쿄", "source_variants": []}],
            safety_mode=True,
            semantic_anchor_enabled=t.supports_en_zh_semantic_anchor_gate(ctx),
        )

        self.assertEqual(source, "東京だけだ。")
        self.assertEqual(target, "도쿄뿐이다.")
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
