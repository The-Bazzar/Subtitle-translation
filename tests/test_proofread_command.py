import copy
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import translate_srt as t
from fakes import FakeLLM
from subtitle_translation import cli, proofread
from subtitle_translation.artifacts import atomic_write_json, file_sha256, json_sha256


class ProofreadCommandTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.path = self.root / "video.beautified.json"
        self.asr = self.root / "video.json"
        self.asr.write_text('{"language":"en","segments":[{"text":"Hello world.","start":0,"end":2}]}', encoding="utf-8")
        segment = t.TranscriptSegment(1, 0, 2, "Hello world.", translation="Original whole",
                                      event_plan_key="plan", translation_key="translation", proofread_key="old-proofread")
        segment.split_events = [t.SplitEvent(0, 1, "Hello", "Original first", "one", (0, 6)),
                                t.SplitEvent(1, 2, "world.", "Original second", "two", (6, 12))]
        self.payload = t.Transcript(str(self.path), "en", [segment]).to_json()
        self.payload["subtitle_languages"] = {"source": "en", "target": "zh"}
        self.payload["custom_root"] = {"preserve": True}
        row = self.payload["segments"][0]
        row["custom_segment"] = {"preserve": [1, 2]}
        row["start"] = row["split_events"][0]["start"] = .123456
        row["split_events"][0]["custom_event"] = {"preserve": "annotation"}
        atomic_write_json(self.path, self.payload)
        self.template = self.root / "template.ass"
        self.template.write_text("[Script Info]\nTitle: Fixture\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n", encoding="utf-8")
        (self.root / "glossary.md").write_text("# Full glossary\nFixture terminology rule\n", encoding="utf-8")
        self.env = {"PROOFREAD_PROVIDER": "fake", "PROOFREAD": "0", "MFA_ENABLED": "1", "LOG_LEVEL": "INFO"}
        self.calls = []
        self.models = []
        self.resolve_llm = t.proofread_llm_from_env
        self.before_reply = None
        for mock in (patch.object(t, "load_env", side_effect=lambda _: self.env),
                     patch.object(t, "proofread_llm_from_env", return_value=FakeLLM()),
                     patch.object(t, "load_prompt", return_value="proofread style"),
                     patch.object(t.ChatSession, "ask_validated", autospec=True, side_effect=self.reply),
                     patch.object(t.time, "sleep"), patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO())):
            mock.start()
            self.addCleanup(mock.stop)

    def reply(self, session, content, validator=None, **kwargs):
        payload = json.loads(content)
        self.calls.append((payload, session.system_prompt))
        self.models.append((session.llm.provider, session.llm.model_name()))
        if self.before_reply:
            self.before_reply()
        rows = []
        for row in payload["items"]:
            target = next(key for key in ("zh", "fr", "ja") if key in row)
            rows.append({"id": row["id"], target: "Revised whole", "split_events": [
                {"id": event["id"], target: "Revised " + event["id"]} for event in row["split_events"]]})
        answer = json.dumps({"items": rows})
        return answer, validator(answer)

    def run_command(self, *extra):
        return cli.main(["proofread", str(self.path), "--template", str(self.template), *extra])

    def test_explicit_command_runs_with_switch_off_without_translation_or_mfa(self):
        asr = self.asr.read_bytes()
        with patch("subtitle_translation.bilingual_translation.translate_events") as translate, \
             patch.object(t, "build_glossary") as glossary, patch.object(t, "prepare_mfa_review") as mfa, \
             patch.object(t, "translate_description") as metadata:
            self.assertEqual(self.run_command(), 0)
        for stage in (translate, glossary, mfa, metadata):
            stage.assert_not_called()
        after = json.loads(self.path.read_text(encoding="utf-8"))
        row = after["segments"][0]
        self.assertEqual(row["translation"], "Revised whole")
        self.assertEqual([e["zh"] for e in row["split_events"]], ["Revised one", "Revised two"])
        normalized = copy.deepcopy(after)
        for field in ("translation", "proofread_key"):
            normalized["segments"][0][field] = self.payload["segments"][0][field]
        for original, event in zip(self.payload["segments"][0]["split_events"], normalized["segments"][0]["split_events"]):
            event["zh"] = original["zh"]
        self.assertEqual(normalized, self.payload)
        self.assertEqual(self.asr.read_bytes(), asr)
        for suffix in (".en.ass", ".zh.ass", ".en-zh.ass", ".split.en.srt", ".split.zh.srt"):
            self.assertTrue((self.root / ("video" + suffix)).is_file())
        self.assertFalse((self.root / "video.en.proofread.ass").exists())
        self.assertIn("Fixture terminology rule", self.calls[0][1])
        self.assertIn("Revised one", (self.root / "video.zh.ass").read_text(encoding="utf-8"))
        self.assertNotIn("Revised one", (self.root / "video.en.ass").read_text(encoding="utf-8"))
        self.assertNotIn("\x1b", (self.root / "subtitle-translation.log").read_text(encoding="utf-8"))

    def test_each_explicit_run_reproofreads_even_when_key_matches(self):
        self.assertEqual(self.run_command(), 0)
        self.assertEqual(self.run_command(), 0)
        self.assertEqual(len(self.calls), 2)

    def test_standalone_proofread_uses_reduced_default_and_preserves_explicit_batch(self):
        for value, expected in ((None, 20), ("", 20), ("50", 50)):
            if value is None:
                self.env.pop("TRANSLATE_BATCH_SIZE", None)
            else:
                self.env["TRANSLATE_BATCH_SIZE"] = value
            with self.subTest(value=value), patch.object(t, "proofread_llm_from_env", return_value=FakeLLM()) as factory:
                self.assertEqual(self.run_command(), 0)
            self.assertEqual(factory.call_args.args[2], expected)

    def test_dedicated_proofreader_does_not_require_a_translation_provider(self):
        self.env["PROOFREAD_MODEL"] = "dedicated-model"
        self.assertNotIn("TRANSLATE_PROVIDER", self.env)
        with patch.object(t, "proofread_llm_from_env", side_effect=self.resolve_llm), \
             patch.object(t.LLMConfig, "cfg", return_value={"default_model": "unused"}):
            self.assertEqual(self.run_command(), 0)
        self.assertEqual(self.models, [("fake", "dedicated-model")])

    def test_exhausted_proofread_requests_keep_existing_targets_and_warn_per_event(self):
        with patch.object(t.ChatSession, "ask_validated", side_effect=RuntimeError("synthetic failure")) as request:
            self.assertEqual(self.run_command(), 0)
        self.assertEqual(request.call_count, 3)
        after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(after["segments"], self.payload["segments"])
        log = (self.root / "subtitle-translation.log").read_text(encoding="utf-8")
        self.assertIn("WARNING", log)
        self.assertEqual(log.count("status=kept_original"), 2)
        self.assertIn("Original first", (self.root / "video.zh.ass").read_text(encoding="utf-8"))

    def test_save_as_keeps_input_and_uses_separate_json_and_ass_arguments(self):
        before = self.path.read_bytes()
        output = self.root / "reviewed.beautified.json"
        ass = self.root / "custom.ass"
        self.assertEqual(self.run_command("--output-json", str(output), "--output", str(ass)), 0)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["segments"][0]["translation"], "Revised whole")
        self.assertTrue((self.root / "reviewed.en.ass").is_file())
        self.assertTrue(ass.is_file())

    def test_incomplete_malformed_or_pending_inputs_fail_before_request(self):
        cases = []
        for kind in ("raw", "pending", "nan", "missing_target", "numeric_target", "bad_source", "duplicate_id", "bad_pipeline"):
            payload = copy.deepcopy(self.payload)
            row = payload["segments"][0]
            if kind == "raw": payload.pop("pipeline")
            elif kind == "pending": row["split_events"][0]["timing_status"] = "pending"
            elif kind == "nan": row["start"] = float("nan")
            elif kind == "missing_target": row.pop("translation")
            elif kind == "numeric_target": row["split_events"][0]["zh"] = 10
            elif kind == "bad_source": row["split_events"][0]["en"] = "Changed"
            elif kind == "duplicate_id": payload["segments"].append(copy.deepcopy(row))
            else: payload["pipeline"] = []
            cases.append((kind, payload))
        for kind, payload in cases:
            with self.subTest(kind=kind):
                atomic_write_json(self.path, payload)
                before = self.path.read_bytes()
                self.assertEqual(self.run_command(), 1)
                self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.calls)
        self.assertFalse((self.root / "video.en-zh.ass").exists())

    def test_ass_srt_and_original_asr_are_not_accepted_as_inputs(self):
        for path in (self.asr, self.root / "video.ass", self.root / "video.srt"):
            with self.subTest(path=path):
                self.assertEqual(cli.main(["proofread", str(path), "--template", str(self.template)]), 1)
        self.assertFalse(self.calls)

    def test_invalid_output_or_raw_asr_destination_is_rejected_before_request(self):
        before = self.asr.read_bytes()
        for options in (("--output-json", str(self.asr)), ("--output", str(self.path)),
                        ("--output-json", str(self.root / "bad.ass")), ("--output", str(self.root / "video.en.ass"))):
            with self.subTest(options=options):
                self.assertEqual(self.run_command(*options), 1)
        self.assertEqual(self.asr.read_bytes(), before)
        self.assertFalse(self.calls)

    def test_saved_languages_override_changed_env_but_explicit_mismatch_is_an_error(self):
        self.env.update(SOURCE_LANG="de", TARGET_LANG="ja")
        self.assertEqual(self.run_command(), 0)
        self.assertIn("en", self.calls[0][0]["items"][0])
        self.assertIn("zh", self.calls[0][0]["items"][0])
        before = len(self.calls)
        self.assertEqual(self.run_command("--target-lang", "fr"), 1)
        self.assertEqual(len(self.calls), before)

    def test_legacy_working_json_can_use_explicit_target_language(self):
        self.payload.pop("subtitle_languages")
        atomic_write_json(self.path, self.payload)
        self.assertEqual(self.run_command("--lang", "en", "--target-lang", "fr"), 0)
        after = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(after["subtitle_languages"], {"source": "en", "target": "fr"})
        self.assertTrue((self.root / "video.en-fr.ass").is_file())

    def test_cancellation_leaves_json_and_existing_exports_untouched(self):
        before = self.path.read_bytes()
        ass = self.root / "video.zh.ass"
        ass.write_text("existing subtitles", encoding="utf-8")
        with patch.object(t.ChatSession, "ask_validated", side_effect=KeyboardInterrupt), self.assertRaises(KeyboardInterrupt):
            self.run_command()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(ass.read_text(encoding="utf-8"), "existing subtitles")
        self.assertFalse(list(self.root.glob(".proofread-*")))

    def test_input_changes_during_request_are_not_overwritten(self):
        changed = copy.deepcopy(self.payload)
        changed["custom_root"] = {"edited": True}
        self.before_reply = lambda: atomic_write_json(self.path, changed)
        self.assertEqual(self.run_command(), 1)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), changed)
        self.assertFalse((self.root / "video.en-zh.ass").exists())
        self.assertFalse(list(self.root.glob(".proofread-*")))

    def test_save_as_destination_changes_are_not_overwritten(self):
        output = self.root / "reviewed.json"
        atomic_write_json(output, self.payload)
        changed = {**self.payload, "custom_root": "changed"}
        self.before_reply = lambda: atomic_write_json(output, changed)
        self.assertEqual(self.run_command("--output-json", str(output)), 1)
        self.assertEqual(json.loads(output.read_text(encoding="utf-8")), changed)

    def test_export_failure_does_not_publish_json_or_partial_staged_exports(self):
        before = self.path.read_bytes()
        with patch.object(t, "write_ass", side_effect=ValueError("bad export")):
            self.assertEqual(self.run_command(), 1)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.root / "video.split.en.srt").exists())
        self.assertFalse(list(self.root.glob(".proofread-*")))

    def test_publish_failure_restores_existing_exports_and_json(self):
        before = self.path.read_bytes()
        source, target = self.root / "video.en.ass", self.root / "video.zh.ass"
        source.write_text("old source", encoding="utf-8")
        target.write_text("old target", encoding="utf-8")
        replace = os.replace
        failed = False
        def fail_once(staged, output):
            nonlocal failed
            if Path(output) == target and not failed:
                failed = True
                raise OSError("synthetic publish failure")
            return replace(staged, output)
        with patch.object(proofread.os, "replace", side_effect=fail_once):
            self.assertEqual(self.run_command(), 1)
        self.assertEqual(source.read_text(encoding="utf-8"), "old source")
        self.assertEqual(target.read_text(encoding="utf-8"), "old target")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.root / "video.split.en.srt").exists())

    def test_inplace_update_refreshes_only_a_valid_existing_timed_projection_manifest(self):
        marker = self.root / ".asr_work" / "text_events" / (json_sha256(str(self.path)) + ".timed.json")
        marker.parent.mkdir(parents=True)
        atomic_write_json(marker, {"key": "keep-timing-key", "sha256": file_sha256(self.path)})
        checkpoint = marker.parent / "translated.json"
        checkpoint.write_text("unchanged text checkpoint", encoding="utf-8")
        self.assertEqual(self.run_command(), 0)
        after = json.loads(marker.read_text(encoding="utf-8"))
        self.assertEqual(after, {"key": "keep-timing-key", "sha256": file_sha256(self.path)})
        self.assertEqual(checkpoint.read_text(encoding="utf-8"), "unchanged text checkpoint")

    def test_frozen_source_mutation_is_never_published(self):
        before = self.path.read_bytes()
        def mutate(transcript, *args, **kwargs):
            transcript.segments[0].split_events[0].start = .5
        with patch.object(t, "proofread_target_events", side_effect=mutate):
            self.assertEqual(self.run_command(), 1)
        self.assertEqual(self.path.read_bytes(), before)
