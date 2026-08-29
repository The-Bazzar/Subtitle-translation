"""Offline regression tests for continuity-aware, regression-guarded proofreading.

These checks intentionally exercise the local proofread boundary.  They do not
call a model or a search backend: the LLM batch and network are replaced with
deterministic fakes.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

import translate_srt as t


class FakeLLM:
    provider = "fake"
    batch_size = 8

    def model_name(self):
        return "offline-proofreader"

    def cfg(self):
        return {}


class ProofreadContinuityAndUncertaintyTests(unittest.TestCase):
    def setUp(self):
        self.ctx = t.TranscriptContext.from_json("sample.json", "", "en", "zh")

    def test_split_events_receive_their_complete_sentence_context(self):
        """A split clause must see its siblings even with neighbor window disabled."""
        first = t.SplitEvent(0.0, 1.0, "If you see it,", "要是你看见它，")
        second = t.SplitEvent(1.0, 2.0, "don't touch it.", "就别碰它。")
        segment = t.TranscriptSegment(
            7,
            0.0,
            2.0,
            "If you see it, don't touch it.",
            split_events=[first, second],
        )
        transcript = t.Transcript("sample.json", "en", [segment])
        captured_items = []

        def unchanged_batch(request, _session, _quiet, retries=3, raise_on_failure=False):
            captured_items.extend(request.to_json_value()["items"])
            return [
                {
                    "id": item["id"],
                    "en": item["en"],
                    "zh": item["zh"],
                    "edit": {
                        "source_changed": False,
                        "target_changed": False,
                        "categories": [],
                        "reasons": [],
                    },
                    "review": {},
                }
                for item in request.to_json_value()["items"]
            ]

        llm = FakeLLM()
        llm.batch_size = 1
        with patch.object(t, "llm_numbered_batch", side_effect=unchanged_batch):
            t.proofread_split_events(
                transcript,
                self.ctx,
                llm,
                "system",
                quiet=True,
                conservative=True,
            )

        items = captured_items
        self.assertEqual(len(items), 2)
        for current_index, item in enumerate(items):
            # `sentence_context` preserves the full parent sentence for this event:
            # it contains every event of this original TranscriptSegment.
            context = item["sentence_context"]
            self.assertEqual(context["current_index"], current_index)
            self.assertEqual(context["full_source"], "If you see it, don't touch it.")
            self.assertEqual(context["full_target"], "要是你看见它，就别碰它。")
            self.assertEqual(
                [(event["en"], event["zh"]) for event in context["events"]],
                [(first.en, first.zh), (second.en, second.zh)],
            )

    def test_target_quality_edits_across_language_categories_are_allowed(self):
        cases = [
            ("The result was beyond expectation.", "结果是在预期之外的。", "结果出乎意料。", "naturalness"),
            ("This medicine takes effect quickly.", "这种药很快发生效果。", "这种药见效很快。", "expression"),
            ("After the meeting, he finally answered.", "在会议之后，他终于作出了回答。", "会后，他终于给了答复。", "naturalness"),
            ("And that is why I left.", "并且那就是我离开的原因。", "所以我才离开。", "context"),
            ("Get out of my sight!", "请离开我的视线范围！", "滚出我的视线！", "expression"),
            ("What I want to say is that we should leave.", "我想要说的事情是我们应该离开。", "我的意思是，我们该走了。", "expression"),
        ]
        for source, baseline, candidate, category in cases:
            with self.subTest(category=category, source=source):
                _source, target, _review = t.apply_proofread_safety_constraints(
                    source,
                    baseline,
                    source,
                    candidate,
                    {
                        "source_changed": False,
                        "target_changed": True,
                        "categories": [category],
                        "reasons": [category],
                    },
                    {},
                    safety_mode=True,
                )
                self.assertEqual(target, candidate)

    def test_pipeline_still_applies_a_well_supported_accuracy_fix(self):
        event = t.SplitEvent(0.0, 1.0, "He did not agree.", "他同意了。")
        transcript = t.Transcript(
            "sample.json",
            "en",
            [t.TranscriptSegment(1, 0.0, 1.0, event.en, split_events=[event])],
        )

        def corrected_batch(request, _session, _quiet, retries=3, raise_on_failure=False):
            return [
                {
                    "id": 1,
                    "en": event.en,
                    "zh": "他不同意。",
                    "edit": {
                        "source_changed": False,
                        "target_changed": True,
                        "categories": ["accuracy"],
                        "reasons": ["原译遗漏否定，候选译文恢复了 did not 的否定关系"],
                    },
                    "review": {},
                }
            ]

        with patch.object(t, "llm_numbered_batch", side_effect=corrected_batch):
            changed = t.proofread_split_events(
                transcript,
                self.ctx,
                FakeLLM(),
                "system",
                quiet=True,
                conservative=True,
            )

        self.assertTrue(changed)
        self.assertEqual(event.zh, "他不同意。")

    def test_clear_semantic_anchor_regressions_are_rejected(self):
        cases = [
            ("He did not agree.", "他不同意。", "他同意。"),
            ("Only he could open it.", "只有他能打开。", "他能打开。"),
            ("All of them survived.", "他们全都活了下来。", "他们有人活了下来。"),
            ("It was extremely cold.", "天气极其寒冷。", "天气很冷。"),
            ("It was cold.", "天气很冷。", "天气冷得极其可怕。"),
            ("You must leave now.", "你现在必须离开。", "你现在可以离开。"),
            ("He might leave now.", "他现在可能会离开。", "他现在会离开。"),
        ]
        for source, baseline, candidate in cases:
            with self.subTest(source=source):
                _source, target, _review = t.apply_conservative_proofread_result(
                    source,
                    baseline,
                    source,
                    candidate,
                    None,
                    {},
                    regression_only=True,
                )
                self.assertEqual(target, baseline)

    def test_safety_gate_diagnostics_distinguish_keep_apply_and_rollback(self):
        keep = t.proofread_decision_diagnostic(
            "source", "译文", "source", "译文", "source", "译文", {}, []
        )
        applied = t.proofread_decision_diagnostic(
            "source", "旧译", "source", "新译", "source", "新译", {}, []
        )
        rolled_back = t.proofread_decision_diagnostic(
            "Only he came.",
            "只有他来了。",
            "Only he came.",
            "他来了。",
            "Only he came.",
            "只有他来了。",
            {},
            ["semantic_anchor:exclusivity"],
        )

        self.assertEqual(keep, ("KEEP_BY_MODEL", []))
        self.assertEqual(applied, ("EDIT_APPLIED", []))
        self.assertEqual(
            rolled_back,
            ("EDIT_ROLLED_BACK", ["semantic_anchor:exclusivity"]),
        )

    def test_term_evidence_cannot_bypass_a_semantic_regression(self):
        """Adding a confirmed term must not license dropping unrelated meaning."""
        _source, target, _review = t.apply_conservative_proofread_result(
            "Only Qelth can open it.",
            "只有凯尔斯能打开。",
            "Only Qelth can open it.",
            "刻尔斯能打开。",
            None,
            {},
            terminology_constraints=[
                {
                    "source": "Qelth",
                    "target": "刻尔斯",
                    "source_variants": [],
                }
            ],
            regression_only=True,
        )

        self.assertEqual(target, "只有凯尔斯能打开。")

    def test_accuracy_fix_can_remove_a_baseline_anchor_absent_from_source(self):
        """Source-aware checks must not preserve a mistranslated negation."""
        _source, target, _review = t.apply_conservative_proofread_result(
            "He agreed.",
            "他没有同意。",
            "He agreed.",
            "他同意了。",
            None,
            {},
            regression_only=True,
        )

        self.assertEqual(target, "他同意了。")

    def test_equivalent_chinese_negation_is_not_falsely_rejected(self):
        _source, target, _review = t.apply_conservative_proofread_result(
            "This is not important.",
            "这不重要。",
            "This is not important.",
            "这无关紧要。",
            None,
            {},
            regression_only=True,
        )

        self.assertEqual(target, "这无关紧要。")

    def test_common_english_contracted_negation_is_protected(self):
        _source, target, _review = t.apply_conservative_proofread_result(
            "He won't agree.",
            "他不会同意。",
            "He won't agree.",
            "他同意。",
            None,
            {},
            regression_only=True,
        )

        self.assertEqual(target, "他不会同意。")

    def test_unbounded_source_rewrite_is_reverted_for_human_review(self):
        source, _target, review = t.apply_conservative_proofread_result(
            "Northwind crashed yesterday.",
            "诺斯风昨天崩溃了。",
            "Northwind Protocol was invented today.",
            "诺斯风协议今天被发明。",
            {
                "source_changed": True,
                "target_changed": True,
                "categories": ["source_ASR"],
                "reasons": ["suspected ASR"],
            },
            {},
            terminology_constraints=[
                {
                    "source": "Northwind Protocol",
                    "target": "诺斯风协议",
                    "source_variants": ["Northwind"],
                }
            ],
            regression_only=True,
        )

        self.assertEqual(source, "Northwind crashed yesterday.")
        self.assertEqual(_target, "诺斯风昨天崩溃了。")
        self.assertTrue(review["needs_human"])
        self.assertIn("source_ASR", review["categories"])

    def test_local_source_asr_edit_without_structured_evidence_is_reverted(self):
        source, target, review = t.apply_proofread_safety_constraints(
            "I saw bar Drill today.",
            "我今天见到了巴德里尔。",
            "I saw Baudrillard today.",
            "我今天见到了鲍德里亚。",
            {
                "source_changed": True,
                "target_changed": True,
                "categories": ["source_ASR"],
                "reasons": ["suspected name correction"],
            },
            {},
            safety_mode=True,
        )

        self.assertEqual(source, "I saw bar Drill today.")
        self.assertEqual(target, "我今天见到了巴德里尔。")
        self.assertTrue(review["needs_human"])
        self.assertIn("source_ASR", review["categories"])

    def test_exact_evidence_backed_source_term_replacement_is_allowed(self):
        source, _target, review = t.apply_conservative_proofread_result(
            "The North Wind Protocol failed.",
            "诺斯风协议失败了。",
            "The Northwind Protocol failed.",
            "诺斯风协议失败了。",
            {
                "source_changed": True,
                "target_changed": False,
                "categories": ["terminology"],
                "reasons": ["confirmed term"],
            },
            {},
            terminology_constraints=[
                {
                    "source": "Northwind Protocol",
                    "target": "诺斯风协议",
                    "source_variants": ["North Wind Protocol"],
                }
            ],
            regression_only=True,
        )

        self.assertEqual(source, "The Northwind Protocol failed.")
        self.assertEqual(review, {})

    def test_legacy_provider_target_edit_without_edit_metadata_is_kept(self):
        """Old providers may omit audit metadata; that alone is not a regression."""
        source, target, _review = t.apply_conservative_proofread_result(
            "That sounds awkward.",
            "那听起来是尴尬的。",
            "That sounds awkward.",
            "那听着挺别扭。",
            None,
            {},
            regression_only=True,
        )

        self.assertEqual(source, "That sounds awkward.")
        self.assertEqual(target, "那听着挺别扭。")

    def test_editor_only_target_edit_ignores_legacy_metadata(self):
        """Optional legacy audit metadata cannot veto an otherwise safe target edit."""
        source, target, review = t.apply_proofread_safety_constraints(
            "It felt out of place.",
            "它感觉不在地方。",
            "It felt out of place.",
            "感觉有些格格不入。",
            {
                "source_changed": False,
                "target_changed": False,
                "categories": [],
                "reasons": [],
            },
            {},
            safety_mode=True,
        )

        self.assertEqual(source, "It felt out of place.")
        self.assertEqual(target, "感觉有些格格不入。")
        self.assertEqual(review, {})

    def test_nonfinal_split_cannot_be_polished_into_a_closed_sentence(self):
        first = t.SplitEvent(0.0, 1.0, "If you see it,", "要是你看见它，")
        second = t.SplitEvent(1.0, 2.0, "don't touch it.", "就别碰它。")
        transcript = t.Transcript(
            "sample.json",
            "en",
            [
                t.TranscriptSegment(
                    1,
                    0.0,
                    2.0,
                    "If you see it, don't touch it.",
                    split_events=[first, second],
                )
            ],
        )

        def risky_batch(request, _session, _quiet, retries=3, raise_on_failure=False):
            outputs = []
            for item in request.items:
                target = "如果你看见它。" if item.id == 1 else item.fields["zh"]
                outputs.append(
                    {
                        "id": item.id,
                        "en": item.fields["en"],
                        "zh": target,
                        "edit": {
                            "source_changed": False,
                            "target_changed": item.id == 1,
                            "categories": ["naturalness"] if item.id == 1 else [],
                            "reasons": ["原译条件分句措辞略显口语，候选改用更常见的条件表达"] if item.id == 1 else [],
                        },
                        "review": {},
                    }
                )
            return outputs

        with patch.object(t, "llm_numbered_batch", side_effect=risky_batch):
            t.proofread_split_events(
                transcript, self.ctx, FakeLLM(), "system", quiet=True, conservative=True
            )

        self.assertEqual(first.zh, "要是你看见它，")
        self.assertEqual(second.zh, "就别碰它。")

    def test_unresolved_search_for_item_forces_human_review_without_guessing(self):
        """An empty lookup for a doubtful name/ASR token stays item-scoped."""
        runtime = t.WebSearchRuntime(
            settings=t.WebSearchSettings(tavily_key="offline-key"),
            max_queries=1,
        )
        with patch.object(t, "tavily_search", return_value=[]):
            result = runtime.execute_search(
                {"query": "Xylophar official name", "item_ids": [1]},
                search_stage="proofread_tool",
            )

        self.assertEqual(result["results"], [])
        self.assertIn(1, runtime.unresolved_item_ids)

        event = t.SplitEvent(0.0, 1.0, "Xylophar said hello.", "赛洛法尔打了招呼。")
        ordinary = t.SplitEvent(1.0, 2.0, "The door opened.", "门开了。")
        transcript = t.Transcript(
            "sample.json",
            "en",
            [
                t.TranscriptSegment(1, 0.0, 1.0, event.en, split_events=[event]),
                t.TranscriptSegment(2, 1.0, 2.0, ordinary.en, split_events=[ordinary]),
            ],
        )

        def unchanged_batch(request, _session, _quiet, retries=3, raise_on_failure=False):
            return [
                {
                    "id": item.id,
                    "en": item.fields["en"],
                    "zh": "西洛法尔打了招呼。" if item.id == 1 else item.fields["zh"],
                    "edit": {
                        "source_changed": False,
                        "target_changed": item.id == 1,
                        "categories": ["terminology"] if item.id == 1 else [],
                        "reasons": ["候选译名试图统一疑似专名的中文音译写法"] if item.id == 1 else [],
                    },
                    "review": {},
                }
                for item in request.items
            ]

        with patch.object(t, "llm_numbered_batch", side_effect=unchanged_batch):
            t.proofread_split_events(
                transcript,
                self.ctx,
                FakeLLM(),
                "system",
                quiet=True,
                search_runtime=runtime,
                conservative=True,
            )

        self.assertEqual(event.en, "Xylophar said hello.")
        self.assertEqual(event.zh, "赛洛法尔打了招呼。")
        self.assertTrue(event.review["needs_human"])
        self.assertTrue(event.review["reasons"])
        self.assertEqual(ordinary.review, {})

    def test_existing_event_review_survives_a_later_proofread_pass(self):
        event = t.SplitEvent(
            0.0,
            1.0,
            "Xylophar said hello.",
            "赛洛法尔打了招呼。",
            review={
                "needs_human": True,
                "categories": ["external_verification"],
                "reasons": ["专名尚无可靠证据"],
            },
        )
        transcript = t.Transcript(
            "sample.json",
            "en",
            [t.TranscriptSegment(1, 0.0, 1.0, event.en, split_events=[event])],
        )

        def unchanged_batch(request, _session, _quiet, retries=3, raise_on_failure=False):
            item = request.items[0]
            return [{"id": item.id, "en": item.fields["en"], "zh": item.fields["zh"], "edit": {}, "review": {}}]

        with patch.object(t, "llm_numbered_batch", side_effect=unchanged_batch):
            t.proofread_split_events(
                transcript, self.ctx, FakeLLM(), "system", quiet=True, conservative=True
            )

        self.assertTrue(event.review["needs_human"])
        self.assertIn("专名尚无可靠证据", event.review["reasons"])

    def test_uncategorized_human_review_roundtrips_and_survives_next_pass(self):
        event = t.SplitEvent(
            0.0,
            1.0,
            "Xylophar said hello.",
            "赛洛法尔打了招呼。",
            review={
                "needs_human": True,
                "reasons": ["专名仍需人工核验"],
                "alternatives": ["西洛法尔"],
                "note": "请对照片尾名单",
            },
        )
        event = t.SplitEvent.from_json(event.to_json())
        self.assertNotIn("categories", event.review)
        self.assertTrue(event.review["needs_human"])

    def test_only_rolled_back_event_gets_one_targeted_retry(self):
        risky = t.SplitEvent(0.0, 1.0, "Only he can open it.", "只有他能打开。")
        keep = t.SplitEvent(1.0, 2.0, "The door is blue.", "门是蓝色的。")
        transcript = t.Transcript(
            "sample.json",
            "en",
            [
                t.TranscriptSegment(1, 0.0, 1.0, risky.en, split_events=[risky]),
                t.TranscriptSegment(2, 1.0, 2.0, keep.en, split_events=[keep]),
            ],
        )
        calls = []

        def fake_batch(request, _session, _quiet, retries=3, raise_on_failure=False):
            calls.append([item.id for item in request.items])
            if len(calls) == 1:
                return [
                    {
                        "id": 1,
                        "en": risky.en,
                        "zh": "他才有办法打开。",
                        "edit": {"source_changed": False, "target_changed": True, "categories": ["naturalness"], "reasons": ["改善表达"]},
                        "review": {},
                    },
                    {"id": 2, "en": keep.en, "zh": keep.zh, "edit": {}, "review": {}},
                ]
            self.assertEqual(retries, 1)
            self.assertTrue(raise_on_failure)
            retry = request.items[0]
            self.assertEqual(retry.fields["safety_retry"]["attempt"], 1)
            self.assertIn("semantic_anchor:exclusivity", retry.fields["safety_retry"]["gate_reasons"])
            self.assertEqual(retry.fields["sentence_context"]["full_source"], risky.en)
            return [
                {
                    "id": retry.id,
                    "en": risky.en,
                    "zh": "只有他才有办法打开。",
                    "edit": {"source_changed": False, "target_changed": True, "categories": ["naturalness"], "reasons": ["保留排他关系并改善表达"]},
                    "review": {},
                }
            ]

        original_timing = [(event.start, event.end) for event in (risky, keep)]
        with patch.object(t, "llm_numbered_batch", side_effect=fake_batch):
            t.proofread_split_events(
                transcript,
                self.ctx,
                FakeLLM(),
                "system",
                quiet=True,
                safety_mode=True,
            )

        self.assertEqual(calls, [[1, 2], [1]])
        self.assertEqual(len(t.all_events(transcript)), 2)
        self.assertEqual([(event.start, event.end) for event in (risky, keep)], original_timing)
        self.assertEqual(risky.zh, "只有他才有办法打开。")
        self.assertEqual(keep.zh, "门是蓝色的。")

    def test_failed_safety_retry_rolls_back_without_a_third_call(self):
        event = t.SplitEvent(0.0, 1.0, "Only he can open it.", "只有他能打开。")
        transcript = t.Transcript(
            "sample.json",
            "en",
            [t.TranscriptSegment(1, 0.0, 1.0, event.en, split_events=[event])],
        )
        calls = 0

        def unsafe_batch(request, _session, _quiet, retries=3, raise_on_failure=False):
            nonlocal calls
            calls += 1
            return [
                {
                    "id": request.items[0].id,
                    "en": event.en,
                    "zh": "他可以打开。",
                    "edit": {"source_changed": False, "target_changed": True, "categories": ["expression"], "reasons": ["调整表达"]},
                    "review": {},
                }
            ]

        with patch.object(t, "llm_numbered_batch", side_effect=unsafe_batch):
            t.proofread_split_events(
                transcript,
                self.ctx,
                FakeLLM(),
                "system",
                quiet=True,
                safety_mode=True,
            )

        self.assertEqual(calls, 2)
        self.assertEqual(event.zh, "只有他能打开。")
        self.assertTrue(event.review["needs_human"])
        self.assertIn("proofread_safety_retry", event.review["categories"])

    def test_partially_applied_edit_gets_one_retry_with_the_same_evidence(self):
        event = t.SplitEvent(
            0.0,
            1.0,
            "Only Northwind works.",
            "只有诺斯风协议有效。",
        )
        transcript = t.Transcript(
            "sample.json",
            "en",
            [t.TranscriptSegment(1, 0.0, 1.0, event.en, split_events=[event])],
        )
        sidecar = t.WebEvidenceSidecar(
            records=[t.WebEvidenceRecord(
                query="official name",
                results=[t.WebEvidenceEntry(
                    url="https://example.test/official-name",
                    content="Northwind Protocol - 诺斯风协议",
                    preferred_domain_hit=True,
                )],
            )],
            confirmed_terms=[t.ConfirmedTermEvidence(
                source="Northwind Protocol",
                target="诺斯风协议",
                source_variants=["Northwind"],
                evidence_urls=["https://example.test/official-name"],
            )],
        )
        calls = []

        def fake_batch(request, _session, _quiet, retries=3, raise_on_failure=False):
            item = request.items[0]
            calls.append(item.fields)
            if len(calls) == 1:
                return [{
                    "id": item.id,
                    "en": "Only Northwind Protocol works.",
                    "zh": "诺斯风协议有效。",
                    "edit": {
                        "source_changed": True,
                        "target_changed": True,
                        "categories": ["source_ASR", "expression"],
                        "reasons": ["应用已确认名称并调整表达"],
                    },
                    "review": {},
                }]
            return [{
                "id": item.id,
                "en": "Only Northwind Protocol works.",
                "zh": "只有诺斯风协议有效。",
                "edit": {
                    "source_changed": True,
                    "target_changed": False,
                    "categories": ["source_ASR"],
                    "reasons": ["仅应用已确认名称"],
                },
                "review": {},
            }]

        with tempfile.TemporaryDirectory() as tmp:
            ctx = t.TranscriptContext.from_json(
                os.path.join(tmp, "sample.json"), "", "en", "zh"
            )
            t.write_web_evidence_sidecar(ctx, sidecar)
            with patch.object(t, "llm_numbered_batch", side_effect=fake_batch):
                t.proofread_split_events(
                    transcript,
                    ctx,
                    FakeLLM(),
                    "system",
                    quiet=True,
                    safety_mode=True,
                )

        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0]["terminology_constraints"],
            calls[1]["terminology_constraints"],
        )
        self.assertEqual(calls[0]["sentence_context"], calls[1]["sentence_context"])
        self.assertEqual(event.en, "Only Northwind Protocol works.")
        self.assertEqual(event.zh, "只有诺斯风协议有效。")


if __name__ == "__main__":
    unittest.main()
