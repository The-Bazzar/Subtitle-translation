import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import translate_srt as t
from subtitle_translation import bilingual_translation as bt
from fakes import FakeLLM


class BilingualTranslationTests(unittest.TestCase):
    def setUp(self):
        self.ctx = t.TranscriptContext.from_json("video.json", source_lang="en", target_lang="zh")
        self.segment = t.TranscriptSegment(1, 0, 4, "Hello, nice day!")
        self.segment.segment_timing = {"quality": "pending", "text_planned": True}
        self.transcript = t.Transcript("video.json", "en", [self.segment], {"_source_first_input": "source"})
        self.llm = FakeLLM()
        self.calls = []
        sleep = patch.object(t.time, "sleep")
        self.addCleanup(sleep.stop)
        self.sleep = sleep.start()

    def reply(self, respond):
        def ask(session, content, validator=None, **kwargs):
            payload = json.loads(content)
            self.calls.append((copy.deepcopy(payload), session.system_prompt))
            answer = json.dumps(respond(payload))
            return answer, validator(answer)
        return patch.object(t.ChatSession, "ask_validated", autospec=True, side_effect=ask)

    def good(self, payload):
        return {"items": [{"id": i["id"], "zh": "A natural whole translation.", "split_events": [
            {"en": "Hello,", "zh": "\u4f60\u597d"}, {"en": "nice day!", "zh": "\u7f8e\u597d\u7684\u4e00\u5929"}]} for i in payload["items"]]}

    def run_translation(self, **kwargs):
        return bt.translate_events(self.transcript, self.ctx, self.llm, "style", **kwargs)

    def test_short_sentence_is_segmented_in_translation_call_without_math_inputs(self):
        with self.reply(self.good):
            self.assertTrue(self.run_translation())
        self.assertEqual(len(self.calls), 1)
        item = self.calls[0][0]["items"][0]
        for key in ("boundaries", "limits", "words", "split_events", "start", "end"):
            self.assertNotIn(key, item)
        self.assertEqual(item["en"], self.segment.text)
        self.assertEqual([e.en for e in self.segment.split_events], ["Hello,", "nice day!"])
        self.assertTrue(all(e.start is None for e in self.segment.split_events))
        self.assertTrue(t.source_events_are_valid(self.segment))
        self.assertIn("Do not calculate character counts", self.calls[0][1])

    def test_target_join_variations_do_not_collapse_events(self):
        with self.reply(self.good):
            self.run_translation()
        self.assertNotEqual(self.segment.translation, "".join(e.zh for e in self.segment.split_events))
        self.assertEqual(len(self.segment.split_events), 2)
        with self.reply(lambda _: self.fail("natural target variation must remain cacheable")):
            self.assertFalse(self.run_translation())

    def test_source_locator_handles_repetition_whitespace_and_cjk_without_offsets_from_model(self):
        self.assertEqual(bt.source_ranges("go go now", ["go", "go now"], "en"), [(0, 3), (3, 9)])
        self.assertEqual(bt.source_ranges("Hello,  nice\nday!", ["Hello,", "nice day!"], "en"), [(0, 8), (8, 17)])
        self.assertEqual(bt.source_ranges("\u4f60\u597d\u4e16\u754c", ["\u4f60\u597d", "\u4e16\u754c"], "zh"), [(0, 2), (2, 4)])
        for parts in (["go", "now"], ["go go", "go now"], ["g", "o go now"]):
            with self.subTest(parts=parts), self.assertRaises(ValueError):
                bt.source_ranges("go go now", parts, "en")

    def test_unlocatable_source_falls_back_without_retry_or_source_edit(self):
        before = self.segment.text
        def respond(payload):
            result = self.good(payload)
            result["items"][0]["split_events"][0]["en"] = "Paraphrased source."
            return result
        stderr = io.StringIO()
        with self.reply(respond), patch("sys.stderr", stderr):
            self.run_translation()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.segment.text, before)
        self.assertEqual(len(self.segment.split_events), 1)
        self.assertEqual(self.segment.split_events[0].zh, self.segment.translation)
        self.assertIn("keeping whole bilingual sentence", stderr.getvalue())
        self.assertNotIn("split_reason", self.segment.to_json())

    def test_no_split_only_changes_segmentation_mode_not_translation_call_count(self):
        with self.reply(self.good):
            self.run_translation(split_enabled=False)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.segment.split_events), 1)
        self.assertEqual(self.segment.split_events[0].en, self.segment.text)
        self.assertEqual(self.segment.split_events[0].zh, self.segment.translation)

    def test_default_word_timing_is_projected_locally_after_joint_response(self):
        self.segment.segment_timing = {}
        self.segment.words = [t.TranscriptWord("Hello,", 0, .8), t.TranscriptWord("nice", 1, 2), t.TranscriptWord("day!", 2.5, 4)]
        with self.reply(self.good):
            self.run_translation()
        self.assertEqual([(e.start, e.end) for e in self.segment.split_events], [(0, .8), (1, 4)])
        self.assertEqual(len(self.calls), 1)

    def test_missing_default_word_axis_keeps_whole_translation_without_fake_times(self):
        self.segment.segment_timing = {}
        with self.reply(self.good), patch("sys.stderr", io.StringIO()):
            self.run_translation()
        self.assertEqual(len(self.segment.split_events), 1)
        self.assertEqual((self.segment.split_events[0].start, self.segment.split_events[0].end), (0, 4))

    def test_empty_whole_translation_retries_but_never_exports_empty_text(self):
        before = copy.deepcopy(self.segment.to_json())
        def respond(payload):
            result = self.good(payload)
            result["items"][0]["zh"] = " "
            return result
        with self.reply(respond), self.assertRaisesRegex(ValueError, "bilingual translation failed"):
            self.run_translation()
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.segment.to_json(), before)

    def test_bad_parts_keep_the_valid_whole_translation(self):
        for value in (None, [], [{"en": "Hello,", "zh": ""}], [False]):
            with self.subTest(parts=value):
                self.segment.translation_key = ""
                def respond(payload):
                    return {"items": [{"id": 1, "zh": "Whole target", "split_events": value}]}
                with self.reply(respond), patch("sys.stderr", io.StringIO()):
                    self.run_translation()
                self.assertEqual(len(self.segment.split_events), 1)
                self.assertEqual(self.segment.split_events[0].zh, "Whole target")

    def test_roundtrip_reuses_the_joint_result_and_configuration_changes_invalidate_it(self):
        with self.reply(self.good):
            self.run_translation()
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "text.json")
            t.save_transcript(self.transcript, path)
            self.transcript = t.load_transcript(path)
            self.segment = self.transcript.segments[0]
        with self.reply(lambda _: self.fail("joint output must survive serialization")):
            self.assertFalse(self.run_translation())
        with self.reply(self.good):
            self.assertTrue(self.run_translation(split_enabled=False))

    def test_old_fixed_event_cache_does_not_hide_new_semantic_translation(self):
        self.segment.translation_key = self.segment.event_plan_key = "legacy-plan"
        self.segment.translation = "old"
        self.segment.split_events = [t.SplitEvent(None, None, self.segment.text, "old", "old", (0, len(self.segment.text)))]
        with self.reply(self.good):
            self.run_translation()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.segment.split_events), 2)

    def test_cancellation_before_commit_does_not_modify_the_sentence(self):
        before = self.segment.to_json()
        checks = []
        def guard():
            checks.append(True)
            if len(checks) > 1:
                raise RuntimeError("admission closed")
        with self.reply(self.good), self.assertRaisesRegex(RuntimeError, "admission closed"):
            self.run_translation(admission_guard=guard)
        self.assertEqual(self.segment.to_json(), before)

    def test_target_language_is_not_fixed_to_chinese(self):
        self.ctx = t.TranscriptContext.from_json("video.json", source_lang="en", target_lang="fr")
        with self.reply(lambda p: {"items": [{"id": 1, "fr": "Bonjour, belle journee!", "split_events": [
            {"en": "Hello,", "fr": "Bonjour"}, {"en": "nice day!", "fr": "Belle journee!"}]}]}):
            self.run_translation()
        self.assertEqual(len(self.segment.split_events), 2)
        self.assertIn("Bonjour", self.segment.split_events[0].zh)

    def test_partial_success_is_checkpointed_and_only_bad_sentence_is_retried(self):
        second = copy.deepcopy(self.segment)
        second.index = 2
        self.transcript.segments.append(second)
        checkpoints = []
        def respond(payload):
            result = self.good(payload)
            for row in result["items"]:
                if row["id"] == 2:
                    row["zh"] = ""
            return result
        with self.reply(respond), self.assertRaises(ValueError):
            self.run_translation(checkpoint=lambda: checkpoints.append(self.segment.translation))
        self.assertTrue(self.segment.translation)
        self.assertFalse(second.translation)
        self.assertTrue(checkpoints)
        self.calls.clear()
        with self.reply(self.good):
            self.run_translation()
        self.assertEqual([item["id"] for item in self.calls[0][0]["items"]], [2])

    def test_unfinished_mfa_text_checkpoint_does_not_discard_good_joint_results(self):
        from subtitle_translation import windowed_mfa as w
        from subtitle_translation.artifacts import text_sha256
        from subtitle_translation.source_review import compose_adopted_text

        items = [{"id": i, "text": "Hello, nice day!", "decision": "keep", "input_range": [i * 4, i * 4 + 3],
                  "source_refs": [f"whisperx:{i}"]} for i in (1, 2)]
        text, ranges = compose_adopted_text(items, "en")
        review = {"language": "en", "items": items, "text": text, "source_map": ranges, "text_sha256": text_sha256(text)}
        with tempfile.TemporaryDirectory() as directory:
            self.ctx = t.TranscriptContext.from_json(str(Path(directory) / "video.json"), source_lang="en", target_lang="zh")
            original = t.Transcript(self.ctx.input_json, "en", [], {})
            self.transcript, path = w.load_text_checkpoint(original, review, self.ctx)
            self.segment = self.transcript.segments[0]
            def respond(payload):
                result = self.good(payload)
                for row in result["items"]:
                    if row["id"] == 2:
                        row["zh"] = ""
                return result
            with self.reply(respond), self.assertRaises(ValueError):
                self.run_translation(checkpoint=lambda: t.save_transcript(self.transcript, str(path)))
            restored, _ = w.load_text_checkpoint(original, review, self.ctx)
            self.assertTrue(restored.segments[0].translation)
            self.assertEqual(restored.segments[1].split_events, [])
            self.transcript = restored
            self.calls.clear()
            with self.reply(self.good):
                self.run_translation()
            self.assertEqual([i["id"] for i in self.calls[0][0]["items"]], [2])

    def test_each_translation_round_logs_request_response_and_result(self):
        second = copy.deepcopy(self.segment)
        second.index = 2
        self.transcript.segments.append(second)
        self.llm.batch_size = 1
        stderr, stdout = io.StringIO(), io.StringIO()
        with self.reply(self.good), patch("sys.stderr", stderr), patch("sys.stdout", stdout):
            self.run_translation()
        log = stderr.getvalue()
        for number in (1, 2):
            self.assertIn(f"Translate LLM round {number}: request", log)
            self.assertIn(f"Translate LLM round {number}: response", log)
            self.assertIn(f"Translate LLM round {number}: validated", log)
        self.assertIn("ids=[1]", log)
        self.assertIn("ids=[2]", log)
        self.assertIn("events=[#1:2]", log)
        self.assertIn("elapsed=", log)
        self.assertIn("sentence #1 status=split events=2", log)
        self.assertIn("event #1.1 id=", log)
        self.assertIn("event #1.2 id=", log)
        self.assertIn("time=pending", log)
        self.assertEqual(stdout.getvalue(), "")

    def test_verbose_event_logs_include_accepted_text_but_not_prompt_or_context(self):
        stderr, stdout = io.StringIO(), io.StringIO()
        with self.reply(self.good), patch("sys.stderr", stderr), patch("sys.stdout", stdout):
            bt.translate_events(self.transcript, self.ctx, self.llm, "private-prompt", )
        log = stderr.getvalue()
        self.assertIn('en="Hello, nice day!"', log)
        self.assertIn('zh="A natural whole translation."', log)
        self.assertIn('en="Hello," zh="\u4f60\u597d"', log)
        self.assertIn('en="nice day!" zh="\u7f8e\u597d\u7684\u4e00\u5929"', log)
        for event in self.segment.split_events:
            self.assertIn(f"id={event.id}", log)
        self.assertNotIn("private-prompt", log)
        self.assertEqual(stdout.getvalue(), "")
        snapshot = copy.deepcopy(self.transcript.to_json())
        cached_log = io.StringIO()
        with self.reply(lambda _: self.fail("log verbosity must not invalidate translation")), patch("sys.stderr", cached_log):
            self.assertFalse(bt.translate_events(self.transcript, self.ctx, self.llm, "private-prompt", ))
        self.assertEqual(snapshot, self.transcript.to_json())
        self.assertIn("Translate cache: event #1.1", cached_log.getvalue())
        self.assertIn("cache=hit", cached_log.getvalue())
        self.assertNotIn("Translate LLM round", cached_log.getvalue())

    def test_whole_fallback_logs_only_the_adopted_whole_event(self):
        def respond(payload):
            result = self.good(payload)
            result["items"][0]["split_events"][0]["en"] = "unaccepted-source-text"
            return result
        stderr = io.StringIO()
        with self.reply(respond), patch("sys.stderr", stderr):
            bt.translate_events(self.transcript, self.ctx, self.llm, "style", )
        log = stderr.getvalue()
        self.assertIn("sentence #1 status=whole_fallback events=1", log)
        self.assertEqual(log.count("event #1.1 id="), 1)
        self.assertNotIn("event #1.2", log)
        self.assertNotIn("unaccepted-source-text", log)

    def test_event_logs_escape_newlines_and_use_actual_language_codes_and_times(self):
        self.ctx = t.TranscriptContext.from_json("video.json", source_lang="en", target_lang="fr")
        self.segment.segment_timing = {}
        def respond(payload):
            return {"items": [{"id": 1, "fr": "Bonjour\njour!", "split_events": [
                {"en": self.segment.text, "fr": "Bonjour\njour!"}]}]}
        stderr = io.StringIO()
        with self.reply(respond), patch("sys.stderr", stderr):
            bt.translate_events(self.transcript, self.ctx, self.llm, "style", )
        event_line = next(line for line in stderr.getvalue().splitlines() if "event #1.1" in line)
        self.assertIn("time=0.000-4.000s", event_line)
        self.assertIn('fr="Bonjour\\njour!"', event_line)
        self.assertNotIn(" zh=", event_line)

    def test_retry_round_logs_only_the_failed_sentence(self):
        second = copy.deepcopy(self.segment)
        second.index = 2
        self.transcript.segments.append(second)
        def respond(payload):
            result = self.good(payload)
            if len(self.calls) == 1:
                result["items"][1]["zh"] = ""
            return result
        stderr = io.StringIO()
        with self.reply(respond), patch("sys.stderr", stderr):
            self.run_translation()
        log = stderr.getvalue()
        self.assertIn("round 1: request attempt=1/3 items=2 ids=[1,2]", log)
        self.assertIn("retry=true failed_ids=[2]", log)
        self.assertIn("round 2: request attempt=2/3 items=1 ids=[2]", log)
        self.assertIn("round 1: sentence #2 status=retry", log)
        self.assertIn("round 2: sentence #2 status=split", log)
        self.assertEqual(log.count("event #1.1 id="), 1)
        self.assertEqual(log.count("event #2.1 id="), 1)

    def test_cache_hit_logs_no_fake_model_round_and_logs_do_not_expose_content(self):
        secret = "private-test-content-that-must-not-appear-in-round-logs"
        stderr = io.StringIO()
        with self.reply(self.good), patch("sys.stderr", stderr):
            bt.translate_events(self.transcript, self.ctx, self.llm, secret, )
        self.assertNotIn(secret, stderr.getvalue())
        self.assertIn(self.segment.text, stderr.getvalue())
        self.assertIn("A natural whole translation.", stderr.getvalue())
        stderr = io.StringIO()
        with self.reply(lambda _: self.fail("cache must not call the LLM")), patch("sys.stderr", stderr):
            bt.translate_events(self.transcript, self.ctx, self.llm, secret, )
        self.assertNotIn("Translate LLM round", stderr.getvalue())
        self.assertIn("Translate cache: event #1.1", stderr.getvalue())
        self.assertIn('en="Hello," zh="\u4f60\u597d"', stderr.getvalue())

    def test_every_normal_event_is_info_with_complete_text_including_cached(self):
        for cached in (False, True):
            with self.subTest(cached=cached), self.reply(self.good), patch.object(bt, "log") as log:
                self.run_translation()
            events = [(call.args[0], call.args[1]) for call in log.call_args_list if " event #" in call.args[1]]
            self.assertEqual(len(events), 2)
            self.assertTrue(all(level == "INFO" for level, _ in events))
            self.assertTrue(all("status=split" in message for _, message in events))
            self.assertIn('en="Hello," zh="\u4f60\u597d"', events[0][1])
            self.assertIn('en="nice day!" zh="\u7f8e\u597d\u7684\u4e00\u5929"', events[1][1])
            self.assertEqual("cache=hit" in events[0][1], cached)

    def test_whole_fallback_event_is_warning_and_unsplit_event_is_info(self):
        def fallback(payload):
            result = self.good(payload)
            result["items"][0].pop("split_events")
            return result
        with self.reply(fallback), patch.object(bt, "log") as log:
            self.run_translation()
        events = [call for call in log.call_args_list if " event #" in call.args[1]]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].args[0], "WARNING")
        self.assertIn("status=whole_fallback", events[0].args[1])
        with self.reply(self.good), patch.object(bt, "log") as log:
            self.run_translation(split_enabled=False)
        events = [call for call in log.call_args_list if " event #" in call.args[1]]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].args[0], "INFO")
        self.assertIn("status=unsplit", events[0].args[1])

    def test_invalid_json_response_logs_failure_and_retries(self):
        calls = []
        def ask(session, content, validator=None, **kwargs):
            calls.append(content)
            answer = "invalid json" if len(calls) == 1 else json.dumps(self.good(json.loads(content)))
            return answer, validator(answer)
        stderr = io.StringIO()
        with patch.object(t.ChatSession, "ask_validated", autospec=True, side_effect=ask), patch("sys.stderr", stderr):
            self.run_translation()
        self.assertIn("round 1: error type=", stderr.getvalue())
        self.assertIn("round 2: request attempt=2/3", stderr.getvalue())
        self.assertEqual(len(calls), 2)

    def test_failed_sentence_status_is_warning_and_still_fails_the_stage(self):
        def respond(payload):
            return {"items": [{"id": 1, "zh": ""}]}
        with self.reply(respond), patch.object(bt, "log") as log, self.assertRaises(ValueError):
            self.run_translation()
        statuses = [call for call in log.call_args_list if "status=retry" in call.args[1] or "status=failed" in call.args[1]]
        self.assertEqual(len(statuses), 3)
        self.assertTrue(all(call.args[0] == "WARNING" for call in statuses))
        self.assertFalse(any(" event #" in call.args[1] for call in log.call_args_list))

    def test_interrupted_round_is_logged_and_cancellation_still_propagates(self):
        before = self.segment.to_json()
        stderr = io.StringIO()
        with patch.object(t.ChatSession, "ask_validated", side_effect=KeyboardInterrupt), \
             patch("sys.stderr", stderr), self.assertRaises(KeyboardInterrupt):
            self.run_translation()
        self.assertIn("round 1: request", stderr.getvalue())
        self.assertIn("round 1: aborted type=KeyboardInterrupt", stderr.getvalue())
        self.assertNotIn("round 2", stderr.getvalue())
        self.assertEqual(self.segment.to_json(), before)

    def test_translation_batches_share_prior_requests_and_translations_at_temperature_point_three(self):
        second = copy.deepcopy(self.segment)
        second.index = 2
        self.transcript.segments.append(second)
        self.llm.batch_size = 1
        requests, sessions = [], []
        def create(session, extra_kwargs):
            sessions.append(session)
            requests.append(copy.deepcopy(session.messages))
            payload = json.loads(session.messages[-1]["content"])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.good(payload))))])
        with patch.object(t.ChatSession, "_create_once", autospec=True, side_effect=create), patch("sys.stderr", io.StringIO()):
            self.run_translation()
        self.assertIs(sessions[0], sessions[1])
        self.assertEqual(sessions[0].temperature, .3)
        self.assertEqual([message["role"] for message in requests[1]], ["system", "user", "assistant", "user"])
        self.assertEqual(json.loads(requests[1][1]["content"])["items"][0]["id"], 1)
        self.assertEqual(json.loads(requests[1][2]["content"])["items"][0]["zh"], "A natural whole translation.")
        self.assertEqual(json.loads(requests[1][3]["content"])["items"][0]["id"], 2)

    def test_rag_batches_use_configured_default_and_full_glossary_stays_in_system_prompt(self):
        second = copy.deepcopy(self.segment)
        second.index = 2
        self.transcript.segments.append(second)
        context = [{"id": str(i), "source": "glossary", "text": f"term {i}"} for i in range(6)]
        retriever = Mock()
        retriever.retrieve_texts.return_value = [context, context]
        full_prompt = "style\nFULL GLOSSARY:\n" + "terminology rule\n" * 1000
        with self.reply(self.good), patch("sys.stderr", io.StringIO()):
            bt.translate_events(self.transcript, self.ctx, self.llm, full_prompt, retriever)
        retriever.retrieve_texts.assert_called_once_with([self.segment.text, second.text])
        for item in self.calls[0][0]["items"]:
            self.assertEqual(set(item), {"id", "en", "retrieved_context"})
            self.assertEqual(item["retrieved_context"], context)
        prompt = self.calls[0][1]
        self.assertTrue(prompt.startswith(full_prompt))
        self.assertIn(t._JSON_BATCH_FORMAT, prompt)
        self.assertLess(prompt.index(t._RETRIEVED_CONTEXT_RULES), prompt.index(t._JSON_FORMAT))

    def test_shared_session_retries_bad_json_without_duplicating_users_or_losing_history(self):
        requests, sessions = [], []
        def create(session, extra_kwargs):
            sessions.append(session)
            requests.append(copy.deepcopy(session.messages))
            content = "bad json" if len(requests) < 3 else json.dumps(self.good(json.loads(session.messages[-1]["content"])))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
        with patch.object(t.ChatSession, "_create_once", autospec=True, side_effect=create), patch("sys.stderr", io.StringIO()):
            self.run_translation()
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(session is sessions[0] for session in sessions))
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(requests[1], requests[2])
        self.sleep.assert_has_calls([call(3.0), call(6.0)])

    def test_separate_translation_runs_do_not_share_conversation_history(self):
        fresh = copy.deepcopy(self.transcript)
        sessions = []
        def create(session, extra_kwargs):
            self.assertEqual([message["role"] for message in session.messages], ["system", "user"])
            sessions.append(session)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.good(json.loads(session.messages[-1]["content"])))) )])
        with patch.object(t.ChatSession, "_create_once", autospec=True, side_effect=create), patch("sys.stderr", io.StringIO()):
            self.run_translation()
            bt.translate_events(fresh, self.ctx, self.llm, "style")
        self.assertIsNot(sessions[0], sessions[1])

    def test_shared_session_does_not_repeat_a_context_length_failure(self):
        with patch.object(t.ChatSession, "_create_once", side_effect=RuntimeError("maximum context length exceeded")) as request, \
             patch("sys.stderr", io.StringIO()), self.assertRaises(ValueError):
            self.run_translation()
        request.assert_called_once()
        self.sleep.assert_not_called()

    def test_usage_triggers_local_compaction_before_next_batch(self):
        second = copy.deepcopy(self.segment)
        second.index = 2
        self.transcript.segments.append(second)
        self.llm.batch_size = 1
        requests = []
        def create(session, extra_kwargs):
            requests.append(copy.deepcopy(session.messages))
            payload = json.loads(session.messages[-1]["content"])
            return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=8100), choices=[
                SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.good(payload))))])
        retriever = Mock()
        retriever.retrieve_texts.side_effect = [[[{"text": "old evidence"}]], [[{"text": "current evidence"}]]]
        with patch.object(t.ChatSession, "_create_once", autospec=True, side_effect=create), \
             patch.object(self.llm, "cfg", return_value={"model_info": [
                 {"id": self.llm.model_name(), "context_length": 10000}]}):
            self.run_translation(retriever=retriever)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0][0], requests[1][0])
        self.assertNotIn("retrieved_context", requests[1][1]["content"])
        self.assertEqual(json.loads(requests[1][2]["content"])["items"],
                         [{"id": 1, "zh": "A natural whole translation."}])
        self.assertIn("current evidence", requests[1][-1]["content"])
        self.assertEqual(len(self.segment.split_events), 2)

    def test_provider_overflow_retries_once_with_same_batch_without_history(self):
        second = copy.deepcopy(self.segment)
        second.index = 2
        self.transcript.segments.append(second)
        self.llm.batch_size = 1
        requests = []
        def create(session, extra_kwargs):
            requests.append(copy.deepcopy(session.messages))
            if len(session.messages) > 2:
                raise RuntimeError("maximum context length exceeded")
            payload = json.loads(session.messages[-1]["content"])
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.good(payload))))])
        with patch.object(t.ChatSession, "_create_once", autospec=True, side_effect=create):
            self.run_translation()
        self.assertEqual([len(r) for r in requests], [2, 4, 2])
        self.assertEqual(requests[2], [requests[1][0], requests[1][-1]])

    def test_rag_and_llm_remain_interleaved_with_each_batch_checkpoint(self):
        second = copy.deepcopy(self.segment)
        second.index = 2
        self.transcript.segments.append(second)
        self.llm.batch_size = 1
        order = []
        retriever = Mock()

        def retrieve(texts):
            order.append("rag")
            return [[] for _ in texts]

        def respond(payload):
            order.append("llm")
            return self.good(payload)

        retriever.retrieve_texts.side_effect = retrieve
        with self.reply(respond):
            self.run_translation(retriever=retriever, checkpoint=lambda: order.append("checkpoint"))
        self.assertEqual(order, ["rag", "llm", "checkpoint", "rag", "llm", "checkpoint"])

    def test_later_retrieval_failure_does_not_discard_completed_batch_checkpoint(self):
        second = copy.deepcopy(self.segment)
        second.index = 2
        self.transcript.segments.append(second)
        self.llm.batch_size = 1
        retriever = Mock()
        retriever.retrieve_texts.side_effect = [[[]], RuntimeError("retrieval unavailable")]
        saved = []
        with self.reply(self.good), self.assertRaisesRegex(RuntimeError, "retrieval unavailable"):
            self.run_translation(retriever=retriever, checkpoint=lambda: saved.append(copy.deepcopy(self.transcript.to_json())))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["segments"][0]["translation"], "A natural whole translation.")
        self.assertFalse(second.translation)
        self.assertEqual(len(self.calls), 1)

    def test_legacy_iso_key_normalization_and_target_label_cleanup_preserve_source(self):
        def respond(payload):
            return {"items": [{"id": 1, "ZH": "SRC: TGT: Whole translation.", "split_events": [
                {"EN": "Hello,", "ZH": "ZH: First part"}, {"EN": "nice day!", "ZH": "TGT: Second part"}]}]}

        with self.reply(respond):
            self.run_translation()
        self.assertEqual(self.segment.translation, "Whole translation.")
        self.assertEqual([e.zh for e in self.segment.split_events], ["First part", "Second part"])
        self.assertEqual([e.en for e in self.segment.split_events], ["Hello,", "nice day!"])

    def test_label_only_whole_translation_is_still_empty(self):
        with self.reply(lambda _: {"items": [{"id": 1, "zh": "ZH: TGT: "}]}), self.assertRaises(ValueError):
            self.run_translation()
        self.assertFalse(self.segment.translation)
        self.assertEqual(len(self.calls), 3)


if __name__ == "__main__":
    unittest.main()
