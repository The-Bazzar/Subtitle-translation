"""Offline regression tests for continuity-aware, regression-guarded proofreading.

These checks intentionally exercise the local proofread boundary.  They do not
call a model or a search backend: the LLM batch and network are replaced with
deterministic fakes.
"""

import os
import tempfile
import threading
import time
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
            self.assertTrue(_session.disable_provider_search)
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

        records = []
        original_timing = [(event.start, event.end) for event in (risky, keep)]
        with patch.object(t, "llm_numbered_batch", side_effect=fake_batch):
            t.proofread_split_events(
                transcript,
                self.ctx,
                FakeLLM(),
                "system",
                quiet=True,
                safety_mode=True,
                decision_records=records,
            )

        self.assertEqual(calls, [[1, 2], [1]])
        self.assertEqual(len(t.all_events(transcript)), 2)
        self.assertEqual([(event.start, event.end) for event in (risky, keep)], original_timing)
        self.assertEqual([record["item_id"] for record in records], [1, 2])
        self.assertEqual(risky.zh, "只有他才有办法打开。")
        self.assertEqual(keep.zh, "门是蓝色的。")
        self.assertEqual(records[0]["first_decision"], "EDIT_ROLLED_BACK")
        self.assertTrue(records[0]["retry_attempted"])
        self.assertEqual(records[0]["retry_decision"], "EDIT_APPLIED")
        self.assertEqual(records[0]["final_target"], "只有他才有办法打开。")
        self.assertFalse(records[1]["retry_attempted"])
        self.assertEqual(records[1]["first_decision"], "KEEP_BY_MODEL")

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

        records = []
        with patch.object(t, "llm_numbered_batch", side_effect=unsafe_batch):
            t.proofread_split_events(
                transcript,
                self.ctx,
                FakeLLM(),
                "system",
                quiet=True,
                safety_mode=True,
                decision_records=records,
            )

        self.assertEqual(calls, 2)
        self.assertEqual(event.zh, "只有他能打开。")
        self.assertEqual(records[0]["first_decision"], "EDIT_ROLLED_BACK")
        self.assertEqual(records[0]["retry_decision"], "EDIT_ROLLED_BACK")
        self.assertEqual(records[0]["final_decision"], "EDIT_ROLLED_BACK")
        self.assertEqual(records[0]["final_target"], "只有他能打开。")

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

        records = []
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
                    decision_records=records,
                )

        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0]["terminology_constraints"],
            calls[1]["terminology_constraints"],
        )
        self.assertEqual(calls[0]["sentence_context"], calls[1]["sentence_context"])
        self.assertEqual(records[0]["first_decision"], "EDIT_PARTIALLY_APPLIED")
        self.assertTrue(records[0]["retry_attempted"])
        self.assertEqual(records[0]["retry_decision"], "EDIT_APPLIED")
        self.assertEqual(event.en, "Only Northwind Protocol works.")
        self.assertEqual(event.zh, "只有诺斯风协议有效。")

    def test_proofread_report_records_initial_retry_and_final_versions(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = t.TranscriptContext.from_json(os.path.join(tmp, "sample.json"), "", "en", "zh")
            record = {
                "item_id": 1,
                "start": 0.0,
                "end": 1.0,
                "original_source": "Only he can open it.",
                "original_target": "只有他能打开。",
                "first_proposal_source": "Only he can open it.",
                "first_proposal_target": "他才有办法打开。",
                "first_decision": "EDIT_ROLLED_BACK",
                "first_gate_reasons": ["semantic_anchor:exclusivity"],
                "retry_attempted": True,
                "retry_proposal_source": "Only he can open it.",
                "retry_proposal_target": "只有他才有办法打开。",
                "retry_decision": "EDIT_APPLIED",
                "retry_gate_reasons": [],
                "retry_error": "",
                "final_decision": "EDIT_APPLIED",
                "final_source": "Only he can open it.",
                "final_target": "只有他才有办法打开。",
                "review": {},
            }

            path = t.write_proofread_report(ctx, [record])
            with open(path, "r", encoding="utf-8") as f:
                report = f.read()

        self.assertIn("EDIT_ROLLED_BACK", report)
        self.assertIn("semantic_anchor:exclusivity", report)
        self.assertIn("Retry proposal: 只有他才有办法打开。", report)
        self.assertIn("Final target: 只有他才有办法打开。", report)
        self.assertIn("Final EDIT_APPLIED: 1", report)
        self.assertIn("Final KEEP: 0", report)
        self.assertIn("Thinking: provider-default", report)

    def test_editor_only_response_derives_keep_and_edit_without_metadata(self):
        first = t.SplitEvent(0.0, 1.0, "That works.", "那个工作")
        second = t.SplitEvent(1.0, 2.0, "Already fine.", "已经很好")
        transcript = t.Transcript("x.json", "en", [
            t.TranscriptSegment(1, 0.0, 1.0, first.en, split_events=[first]),
            t.TranscriptSegment(2, 1.0, 2.0, second.en, split_events=[second]),
        ])

        def editor(request, _session, _quiet, retries=3, raise_on_failure=False):
            return [
                {"id": item.id, "en": item.fields["en"],
                 "zh": "这样就行" if item.id == 1 else item.fields["zh"], "review": {}}
                for item in request.items
            ]

        records = []
        with patch.object(t, "llm_numbered_batch", side_effect=editor):
            t.proofread_split_events(transcript, self.ctx, FakeLLM(), "system", True,
                                     decision_records=records)

        self.assertEqual(first.zh, "这样就行")
        self.assertEqual([row["first_decision"] for row in records],
                         ["EDIT_APPLIED", "KEEP_BY_MODEL"])

    def test_concurrent_batches_are_in_flight_and_commit_in_event_order(self):
        events = [t.SplitEvent(float(i), float(i + 1), f"source {i}", f"译文{i}") for i in range(4)]
        transcript = t.Transcript("x.json", "en", [
            t.TranscriptSegment(i + 1, float(i), float(i + 1), event.en, split_events=[event])
            for i, event in enumerate(events)
        ])
        llm = FakeLLM()
        llm.batch_size = 2
        lock = threading.Lock()
        active = 0
        peak = 0
        completed = []

        def concurrent_editor(request, _session, _quiet, retries=3, raise_on_failure=False):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.04 if request.items[0].id == 1 else 0.01)
            with lock:
                active -= 1
                completed.append(request.items[0].id)
            return [{"id": item.id, "en": item.fields["en"],
                     "zh": item.fields["zh"] + "改", "review": {}} for item in request.items]

        records = []
        with patch.object(t, "llm_numbered_batch", side_effect=concurrent_editor):
            t.proofread_split_events(transcript, self.ctx, llm, "system", True,
                                     concurrency=2, decision_records=records)

        self.assertGreaterEqual(peak, 2)
        self.assertEqual(completed, [3, 1])
        self.assertEqual([row["item_id"] for row in records], [1, 2, 3, 4])
        self.assertEqual([event.zh for event in events], [f"译文{i}改" for i in range(4)])

    def test_sentence_group_retry_is_atomic_and_never_restores_parent_target(self):
        first = t.SplitEvent(0.0, 1.0, "Only he", "只有他")
        second = t.SplitEvent(1.0, 2.0, "can go.", "能去")
        parent = "只有他能去"
        transcript = t.Transcript("x.json", "en", [
            t.TranscriptSegment(1, 0.0, 2.0, "Only he can go.", translation=parent,
                                split_events=[first, second])
        ])
        calls = []

        def unsafe(request, _session, _quiet, retries=3, raise_on_failure=False):
            calls.append([item.id for item in request.items])
            return [
                {"id": item.id, "en": item.fields["en"],
                 "zh": parent if item.id == 2 else "他", "review": {}}
                for item in request.items
            ]

        records = []
        with patch.object(t, "llm_numbered_batch", side_effect=unsafe):
            t.proofread_split_events(transcript, self.ctx, FakeLLM(), "system", True,
                                     decision_records=records)

        self.assertEqual(calls, [[1, 2], [1, 2]])
        self.assertEqual([first.zh, second.zh], ["只有他", "能去"])
        self.assertNotEqual(second.zh, parent)
        self.assertTrue(any("sentence_group_full_target_repeated" in row["first_gate_reasons"]
                            for row in records))

    def test_length_exhaustion_is_review_not_keep_or_empty_subtitle(self):
        event = t.SplitEvent(0.0, 1.0, "source", "原译")
        transcript = t.Transcript("x.json", "en", [
            t.TranscriptSegment(1, 0.0, 1.0, event.en, split_events=[event])
        ])

        def exhausted(*_args, **_kwargs):
            raise t.LLMOutputLengthError("finish_reason=length")

        records = []
        with patch.object(t, "llm_numbered_batch", side_effect=exhausted):
            t.proofread_split_events(transcript, self.ctx, FakeLLM(), "system", True,
                                     decision_records=records)

        self.assertEqual(event.zh, "原译")
        self.assertTrue(event.review["needs_human"])
        self.assertEqual(records[0]["first_decision"], "REVIEW_BY_MODEL")
        self.assertIn("output_length_exhausted", records[0]["first_gate_reasons"])

    def test_output_length_split_never_splits_sibling_events(self):
        first = t.SplitEvent(0.0, 1.0, "part one", "甲")
        second = t.SplitEvent(1.0, 2.0, "part two", "乙")
        third = t.SplitEvent(2.0, 3.0, "other", "丙")
        transcript = t.Transcript("x.json", "en", [
            t.TranscriptSegment(1, 0.0, 2.0, "part one part two", translation="甲乙",
                                split_events=[first, second]),
            t.TranscriptSegment(2, 2.0, 3.0, third.en, split_events=[third]),
        ])
        llm = FakeLLM(); llm.batch_size = 3
        calls = []

        def length_then_edit(request, *_args, **_kwargs):
            ids = [item.id for item in request.items]; calls.append(ids)
            if ids == [1, 2, 3]:
                raise t.LLMOutputLengthError("finish_reason=length")
            return [{"id": item.id, "en": item.fields["en"],
                     "zh": item.fields["zh"] + "改", "review": {}} for item in request.items]

        metrics = {}
        records = []
        with patch.object(t, "llm_numbered_batch", side_effect=length_then_edit):
            t.proofread_split_events(
                transcript, self.ctx, llm, "system", True,
                metrics=metrics, decision_records=records,
            )

        self.assertEqual(calls, [[1, 2, 3], [1, 2], [3]])
        self.assertFalse(any(ids in ([1], [2]) for ids in calls))
        self.assertEqual(metrics["length_group_splits"], 1)
        self.assertEqual([first.zh, second.zh, third.zh], ["甲改", "乙改", "丙改"])
        self.assertTrue(all(event.zh.count("改") == 1 for event in (first, second, third)))
        self.assertEqual([row["item_id"] for row in records], [1, 2, 3])

    def test_group_provider_failure_rolls_back_sibling_report_state(self):
        first = t.SplitEvent(0.0, 1.0, "first", "甲")
        second = t.SplitEvent(1.0, 2.0, "second", "乙")
        transcript = t.Transcript("x.json", "en", [
            t.TranscriptSegment(1, 0.0, 2.0, "first second", translation="甲乙",
                                split_events=[first, second])
        ])

        def partial_response(*_args, **_kwargs):
            raise t.LLMOutputLengthError("finish_reason=length")

        records = []
        with patch.object(t, "llm_numbered_batch", side_effect=partial_response):
            t.proofread_split_events(transcript, self.ctx, FakeLLM(), "system", True,
                                     decision_records=records)

        self.assertEqual([first.zh, second.zh], ["甲", "乙"])
        self.assertTrue(all(row["final_target"] == original for row, original in zip(records, ["甲", "乙"])))
        self.assertTrue(all(row["final_decision"] != "EDIT_APPLIED" for row in records))

    def test_group_rollback_report_never_leaves_safe_sibling_final_edit_applied(self):
        first = t.SplitEvent(0.0, 1.0, "plain", "甲")
        second = t.SplitEvent(1.0, 2.0, "Only him", "只有他")
        transcript = t.Transcript("x.json", "en", [
            t.TranscriptSegment(1, 0.0, 2.0, "plain Only him", translation="甲只有他",
                                split_events=[first, second])
        ])
        calls = 0

        def proposals(request, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            return [
                {"id": item.id, "en": item.fields["en"],
                 "zh": "甲改" if item.id == 1 else "他", "review": {}}
                for item in request.items
            ]

        records = []
        with patch.object(t, "llm_numbered_batch", side_effect=proposals):
            t.proofread_split_events(transcript, self.ctx, FakeLLM(), "system", True,
                                     decision_records=records)

        self.assertEqual(calls, 2)
        self.assertEqual([first.zh, second.zh], ["甲", "只有他"])
        self.assertEqual(records[0]["first_decision"], "EDIT_APPLIED")
        self.assertEqual(records[0]["final_decision"], "EDIT_ROLLED_BACK")
        self.assertTrue(all(row["group_final_decision"] == "GROUP_ROLLED_BACK" for row in records))

    def test_safety_retry_length_exhaustion_is_explicit_group_rollback(self):
        first = t.SplitEvent(0.0, 1.0, "Only", "只有")
        second = t.SplitEvent(1.0, 2.0, "him.", "他。")
        transcript = t.Transcript("x.json", "en", [
            t.TranscriptSegment(1, 0.0, 2.0, "Only him.", translation="只有他。",
                                split_events=[first, second])
        ])
        calls = 0

        def first_then_length(request, *_args, **_kwargs):
            nonlocal calls; calls += 1
            if calls == 2:
                raise t.LLMOutputLengthError("finish_reason=length")
            return [{"id": item.id, "en": item.fields["en"], "zh": "他", "review": {}}
                    for item in request.items]

        records, metrics = [], {}
        with patch.object(t, "llm_numbered_batch", side_effect=first_then_length):
            t.proofread_split_events(transcript, self.ctx, FakeLLM(), "system", True,
                                     decision_records=records, metrics=metrics)

        self.assertEqual(calls, 2)
        self.assertEqual([first.zh, second.zh], ["只有", "他。"])
        self.assertEqual(metrics["output_length_exhaustions"], 1)
        self.assertTrue(all("proofread_output_length" in event.review["categories"]
                            for event in [first, second]))
        self.assertTrue(all(row["group_final_decision"] == "GROUP_ROLLED_BACK" for row in records))

if __name__ == "__main__":
    unittest.main()
