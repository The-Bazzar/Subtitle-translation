import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import translate_srt as t
from fakes import FakeLLM
from subtitle_translation import cli, windowed_mfa
from subtitle_translation.source_review import compose_adopted_text
from subtitle_translation.artifacts import text_sha256


class TranslateRerunTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.source = self.root / "video.json"
        self.template = self.root / "template.ass"
        self.template.write_text("[Script Info]\nTitle: Test\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n", encoding="utf-8")
        segment = t.TranscriptSegment(1, 0, 4, "Hello, nice day!", words=[
            t.TranscriptWord("Hello,", 0, .8), t.TranscriptWord("nice", 1, 2), t.TranscriptWord("day!", 2.5, 4)])
        t.save_transcript(t.Transcript(str(self.source), "en", [segment]), str(self.source))
        self.counts = {"translate": 0, "proofread": 0}
        self.env = {"TRANSLATE_PROVIDER": "fake", "MFA_ENABLED": "0", "PROOFREAD": "1",
                    "PIPELINE_SKIP_SCENE_DETECTION": "1", "PIPELINE_SKIP_KNOWLEDGE": "1", "FORCE_REBUILD": "0"}

    def ask(self, session, content, validator=None, **kwargs):
        items = json.loads(content)["items"]
        proofread = "TARGET-ONLY PROOFREADING" in session.system_prompt
        stage = "proofread" if proofread else "translate"
        self.counts[stage] += 1
        label = f"{stage} {self.counts[stage]}"
        rows = []
        for item in items:
            events = ([{"id": e["id"], "zh": label + " part"} for e in item["split_events"]] if proofread
                      else [{"en": "Hello,", "zh": label + " first"}, {"en": "nice day!", "zh": label + " second"}])
            rows.append({"id": item["id"], "zh": label, "split_events": events})
        answer = json.dumps({"items": rows})
        return answer, validator(answer)

    def patches(self):
        return (patch.object(t, "load_env", return_value=self.env),
                patch.object(t, "translate_llm_from_env", return_value=FakeLLM()),
                patch.object(t, "proofread_llm_from_env", return_value=FakeLLM()),
                patch.object(t, "load_prompt", return_value="style"),
                patch.object(t.ChatSession, "ask_validated", autospec=True, side_effect=self.ask))

    def test_explicit_translate_twice_retranslates_and_reproofreads_without_reading_old_output(self):
        original = self.source.read_bytes()
        env, translator, proofreader, prompt, ask = self.patches()
        with env, translator, proofreader, prompt, ask, patch("sys.stderr", io.StringIO()), \
             patch("sys.stdout", io.StringIO()), patch.object(t, "load_transcript", wraps=t.load_transcript) as load:
            for _ in range(2):
                self.assertEqual(cli.main(["translate", str(self.source), "--template", str(self.template)]), 0)
        self.assertEqual(self.counts, {"translate": 2, "proofread": 2})
        self.assertTrue(all(Path(call.args[0]).resolve() == self.source for call in load.call_args_list))
        saved = t.load_transcript(str(self.root / "video.beautified.json"))
        self.assertEqual(saved.segments[0].translation, "proofread 2")
        self.assertTrue(all(event.zh == "proofread 2 part" for event in saved.segments[0].split_events))
        self.assertEqual(self.source.read_bytes(), original)
        self.assertTrue((self.root / "video.en.ass").is_file())
        self.assertFalse((self.root / "video.en.proofread.ass").exists())
        self.assertEqual(saved.raw["subtitle_languages"], {"source": "en", "target": "zh"})

    def test_mfa_explicit_rerun_bypasses_text_and_timed_results_without_forcing_source_review(self):
        self.env["MFA_ENABLED"] = "1"
        items = [{"id": 1, "text": "Hello, nice day!", "decision": "keep", "source_refs": ["whisperx:1"], "input_range": [0, 4]}]
        text, ranges = compose_adopted_text(items, "en")
        review = {"language": "en", "text": text, "items": items, "source_map": ranges,
                  "text_sha256": text_sha256(text), "generation": "fixture"}
        def timed(transcript, *args):
            self.assertTrue(args[8])
            result = copy.deepcopy(transcript)
            for segment in result.segments:
                segment.segment_timing["quality"] = "aligned"
                for i, event in enumerate(segment.split_events):
                    event.start, event.end = i * 2, (i + 1) * 2
            return result, None, None
        env, translator, proofreader, prompt, ask = self.patches()
        with env, translator, proofreader, prompt, ask, patch("sys.stderr", io.StringIO()), patch("sys.stdout", io.StringIO()), \
             patch.object(t, "prepare_mfa_review", return_value=(review, self.root / "video.wav", None)) as prepare, \
             patch.object(windowed_mfa, "load_text_checkpoint", wraps=windowed_mfa.load_text_checkpoint) as checkpoint, \
             patch.object(windowed_mfa, "finish_timing", side_effect=timed):
            for _ in range(2):
                self.assertEqual(cli.main(["translate", str(self.source), "--template", str(self.template)]), 0)
        self.assertEqual(self.counts, {"translate": 2, "proofread": 2})
        self.assertTrue(all(call.args[3] for call in checkpoint.call_args_list))
        self.assertTrue(all(not call.args[4].force for call in prepare.call_args_list))

    def test_only_modes_do_not_force_a_full_translation(self):
        for flag in ("--only-scenes", "--only-glossary", "--only-output-path"):
            with self.subTest(flag=flag), patch.object(t, "load_env", return_value=self.env), \
                 patch.object(t, "_run_translate_command", return_value=0) as run, patch("sys.stderr", io.StringIO()):
                self.assertEqual(cli.main(["translate", str(self.source), flag]), 0)
            self.assertFalse(run.call_args.args[0].rerun_translation)

    def test_internal_translate_entry_keeps_existing_cache_policy(self):
        with patch.object(t, "load_env", return_value=self.env), patch.object(t, "_run_translate_command", return_value=0) as run, \
             patch("sys.stderr", io.StringIO()):
            self.assertEqual(t.main([str(self.source)]), 0)
        self.assertFalse(run.call_args.args[0].rerun_translation)

    def test_disabled_proofreading_also_uses_neutral_source_ass_name(self):
        self.env["PROOFREAD"] = "0"
        legacy = self.root / "video.en.proofread.ass"
        legacy.write_text("untouched old output", encoding="utf-8")
        env, translator, proofreader, prompt, ask = self.patches()
        with env, translator, proofreader, prompt, ask, patch("sys.stderr", io.StringIO()), patch("sys.stdout", io.StringIO()):
            self.assertEqual(cli.main(["translate", str(self.source), "--template", str(self.template)]), 0)
        self.assertEqual(self.counts, {"translate": 1, "proofread": 0})
        self.assertTrue((self.root / "video.en.ass").is_file())
        self.assertEqual(legacy.read_text(encoding="utf-8"), "untouched old output")
