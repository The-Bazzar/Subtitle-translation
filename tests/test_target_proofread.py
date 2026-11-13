import copy
import io
import json
import unittest
from unittest.mock import patch

import translate_srt as t
from fakes import FakeLLM


class TargetProofreadTests(unittest.TestCase):
    def setUp(self):
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
        return t.proofread_target_events(self.transcript, self.ctx, FakeLLM(), "style", )

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
