import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import translate_srt as t
from fakes import FakeLLM


class TargetProofreadTests(unittest.TestCase):
    def setUp(self):
        sleep = patch.object(t.time, "sleep")
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)
        self.ctx = t.TranscriptContext.from_json("video.json", source_lang="en", target_lang="zh")
        self.segment = t.TranscriptSegment(1, 0, 2, "Hello world.", translation="Whole translation", event_plan_key="semantic-plan")
        self.segment.split_events = [t.SplitEvent(0, 1, "Hello", "first", "one", (0, 6)),
                                     t.SplitEvent(1, 2, "world.", "second", "two", (6, 12))]
        self.transcript = t.Transcript("video.json", "en", [self.segment])
        self.calls = []

    def response(self, payload):
        return {"items": [{"id": row["id"], "zh": "Whole revised translation.", "split_events": [
            {"id": e["id"], "zh": "revised " + e["id"]} for e in row["split_events"]]} for row in payload["items"]]}

    def reply(self, responder):
        def ask(session, content, validator=None, **kwargs):
            payload = json.loads(content)
            self.calls.append((copy.deepcopy(payload), session.system_prompt))
            answer = json.dumps(responder(payload))
            return answer, validator(answer)
        return patch.object(t.ChatSession, "ask_validated", autospec=True, side_effect=ask)

    def run_stage(self):
        return t.proofread_target_events(self.transcript, self.ctx, FakeLLM(), "style")

    def add_segment(self):
        second = copy.deepcopy(self.segment)
        second.index = 2
        self.transcript.segments.append(second)
        return second

    def completion(self, payload):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.response(payload))))])

    def test_updates_only_target_and_caches_natural_nonidentical_join(self):
        before = [(e.en, e.id, e.source_range, e.start, e.end) for e in self.segment.split_events]
        with self.reply(self.response):
            self.assertTrue(self.run_stage())
        self.assertEqual(before, [(e.en, e.id, e.source_range, e.start, e.end) for e in self.segment.split_events])
        self.assertNotEqual(self.segment.translation, "".join(e.zh for e in self.segment.split_events))
        with self.reply(lambda _: self.fail("valid proofreading must be cached")):
            self.assertFalse(self.run_stage())

    def test_source_time_and_event_id_changes_are_never_applied(self):
        before = self.segment.to_json()
        for kind in ("source", "time", "unknown", "duplicate", "missing", "empty"):
            def respond(payload):
                response = self.response(payload)
                row = response["items"][0]
                if kind == "source":
                    row["en"] = "Changed source"
                elif kind == "time":
                    row["split_events"][0]["start"] = 9
                elif kind == "unknown":
                    row["split_events"][0]["id"] = "unknown"
                elif kind == "duplicate":
                    row["split_events"][1]["id"] = "one"
                elif kind == "missing":
                    row["split_events"].pop()
                else:
                    row["split_events"][0]["zh"] = ""
                return response
            with self.subTest(kind=kind), self.reply(respond), patch("sys.stderr", io.StringIO()):
                self.assertFalse(self.run_stage())
            self.assertEqual(self.segment.to_json(), before)

    def test_cancellation_is_not_a_translation_fallback(self):
        before = self.segment.to_json()
        with patch.object(t.ChatSession, "ask_validated", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.run_stage()
        self.assertEqual(self.segment.to_json(), before)

    def test_bad_translation_is_not_accepted_as_a_proofread_input(self):
        self.segment.split_events[0].zh = ""
        with patch.object(t.ChatSession, "ask_validated") as ask, self.assertRaises(ValueError):
            self.run_stage()
        ask.assert_not_called()

    def test_shared_conversation_uses_event_budget_and_temperature_point_two(self):
        self.add_segment()
        llm = FakeLLM()
        llm.batch_size = 3
        requests, sessions = [], []

        def create(session, extra_kwargs):
            sessions.append(session)
            requests.append(copy.deepcopy(session.messages))
            return self.completion(json.loads(session.messages[-1]["content"]))

        with patch.object(t.ChatSession, "_create_once", autospec=True, side_effect=create):
            self.assertTrue(t.proofread_target_events(self.transcript, self.ctx, llm, "style"))
        self.assertEqual(len(requests), 2)
        self.assertIs(sessions[0], sessions[1])
        self.assertEqual(sessions[0].temperature, .2)
        self.assertEqual([m["role"] for m in requests[1]], ["system", "user", "assistant", "user"])
        self.assertEqual(json.loads(requests[0][-1]["content"])["items"][0]["id"], 1)
        self.assertEqual(json.loads(requests[1][-1]["content"])["items"][0]["id"], 2)
        self.assertEqual(json.loads(requests[1][2]["content"])["items"][0]["zh"], "Whole revised translation.")

    def test_one_sentence_larger_than_event_budget_remains_atomic(self):
        llm = FakeLLM()
        llm.batch_size = 1
        with self.reply(self.response):
            self.assertTrue(t.proofread_target_events(self.transcript, self.ctx, llm, "style"))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.calls[0][0]["items"][0]["split_events"]), 2)

    def test_rag_uses_legacy_event_queries_and_still_validates_cache_input(self):
        self.add_segment()
        events = [e for segment in self.transcript.segments for e in segment.split_events]
        queries = ["ASR correction glossary proofread source-language proper names terminology\n"
                   f"Source: {e.en}\nTarget: {e.zh}" for e in events]
        contexts = [[{"id": str(i), "source": "glossary", "text": f"term {i}"}] for i in range(4)]
        retriever = Mock()
        retriever.retrieve_texts.return_value = contexts
        prompt = "style\nFULL GLOSSARY:\n" + "terminology rule\n" * 100
        with self.reply(self.response):
            self.assertTrue(t.proofread_target_events(self.transcript, self.ctx, FakeLLM(), prompt,
                                                    retriever, retrieval_top_k=5))
        retriever.retrieve_texts.assert_called_once_with(queries, top_k=5)
        payload, system = self.calls[0]
        self.assertTrue(system.startswith(prompt))
        self.assertLess(system.index(t._RETRIEVED_CONTEXT_RULES), system.index(t._JSON_FORMAT))
        actual = [e["retrieved_context"] for row in payload["items"] for e in row["split_events"]]
        self.assertEqual(actual, contexts)
        self.assertTrue(all(set(row) == {"id", "en", "zh", "split_events"} for row in payload["items"]))
        retriever.reset_mock()
        with self.reply(lambda _: self.fail("valid proofreading must be cached")):
            self.assertFalse(t.proofread_target_events(self.transcript, self.ctx, FakeLLM(), prompt,
                                                     retriever, retrieval_top_k=5))
        retriever.retrieve_texts.assert_called_once()

    def test_context_limit_splits_batch_without_retaining_oversized_user_message(self):
        self.add_segment()
        requests, sessions = [], []

        def create(session, extra_kwargs):
            sessions.append(session)
            requests.append(copy.deepcopy(session.messages))
            payload = json.loads(session.messages[-1]["content"])
            if len(payload["items"]) > 1:
                raise RuntimeError("maximum context length exceeded")
            return self.completion(payload)

        with patch.object(t.ChatSession, "_create_once", autospec=True, side_effect=create):
            self.assertTrue(self.run_stage())
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(s is sessions[0] for s in sessions))
        self.assertEqual([m["role"] for m in requests[1]], ["system", "user"])
        self.assertEqual([m["role"] for m in requests[2]], ["system", "user", "assistant", "user"])
        self.assertTrue(all(s.translation == "Whole revised translation." for s in self.transcript.segments))
        self.sleep.assert_not_called()

    def test_oversized_sentence_retries_without_rag_but_keeps_full_glossary(self):
        retriever = Mock()
        retriever.retrieve_texts.return_value = [[{"text": "large context"}], [{"text": "large context"}]]
        requests, sessions = [], []

        def create(session, extra_kwargs):
            sessions.append(session)
            requests.append(copy.deepcopy(session.messages))
            payload = json.loads(session.messages[-1]["content"])
            if any(e.get("retrieved_context") for row in payload["items"] for e in row["split_events"]):
                raise RuntimeError("maximum context length exceeded")
            return self.completion(payload)

        with patch.object(t.ChatSession, "_create_once", autospec=True, side_effect=create):
            self.assertTrue(t.proofread_target_events(self.transcript, self.ctx, FakeLLM(), "FULL GLOSSARY", retriever))
        self.assertEqual(len(requests), 2)
        self.assertIs(sessions[0], sessions[1])
        self.assertTrue(all(len(messages) == 2 for messages in requests))
        self.assertTrue(all(messages[0]["content"].startswith("FULL GLOSSARY") for messages in requests))
        events = json.loads(requests[1][-1]["content"])["items"][0]["split_events"]
        self.assertTrue(all("retrieved_context" not in e for e in events))
        self.sleep.assert_not_called()

    def test_retries_bad_json_three_times_then_keeps_original_targets(self):
        before = self.segment.to_json()
        requests = []

        def create(session, extra_kwargs):
            requests.append(copy.deepcopy(session.messages))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="bad json"))])

        with patch.object(t.ChatSession, "_create_once", autospec=True, side_effect=create):
            self.assertFalse(self.run_stage())
        self.assertEqual(len(requests), 3)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(requests[1], requests[2])
        self.sleep.assert_has_calls([call(3.0), call(6.0)])
        self.assertEqual(self.segment.to_json(), before)

    def test_separate_runs_start_new_conversations(self):
        fresh = copy.deepcopy(self.transcript)
        sessions = []

        def create(session, extra_kwargs):
            sessions.append(session)
            self.assertEqual([m["role"] for m in session.messages], ["system", "user"])
            return self.completion(json.loads(session.messages[-1]["content"]))

        with patch.object(t.ChatSession, "_create_once", autospec=True, side_effect=create):
            self.run_stage()
            t.proofread_target_events(fresh, self.ctx, FakeLLM(), "style")
        self.assertIsNot(sessions[0], sessions[1])

    def test_event_rag_and_proofread_requests_remain_interleaved(self):
        self.add_segment()
        llm = FakeLLM()
        llm.batch_size = 2
        retriever = Mock()
        order = []

        def retrieve(texts, top_k):
            order.append("rag")
            return [[] for _ in texts]

        def respond(payload):
            order.append("llm")
            return self.response(payload)

        retriever.retrieve_texts.side_effect = retrieve
        with self.reply(respond):
            t.proofread_target_events(self.transcript, self.ctx, llm, "style", retriever)
        self.assertEqual(order, ["rag", "llm", "rag", "llm"])

    def test_legacy_language_key_case_and_target_labels_do_not_edit_source_or_time(self):
        before = [(e.en, e.id, e.start, e.end) for e in self.segment.split_events]

        def respond(payload):
            rows = self.response(payload)
            for row in rows["items"]:
                row["ZH"] = "ZH: " + row.pop("zh")
                for event in row["split_events"]:
                    event["ZH"] = "TGT: " + event.pop("zh")
            return rows

        with self.reply(respond):
            self.assertTrue(self.run_stage())
        self.assertEqual(self.segment.translation, "Whole revised translation.")
        self.assertEqual([e.zh for e in self.segment.split_events], ["revised one", "revised two"])
        self.assertEqual(before, [(e.en, e.id, e.start, e.end) for e in self.segment.split_events])

    def test_duplicate_target_aliases_and_label_only_targets_remain_invalid(self):
        row = self.response({"items": [{"id": 1, "split_events": [{"id": "one"}, {"id": "two"}]}]})["items"][0]
        for changed in ({**row, "ZH": "conflicting value"}, {**row, "zh": "ZH: "}):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                t.validate_target_events(changed, self.segment, self.ctx)
