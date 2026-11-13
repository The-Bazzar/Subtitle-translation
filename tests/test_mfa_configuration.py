import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import batch_runtime
import translate_srt as t
from subtitle_translation import pipeline
from subtitle_translation.config import ProjectConfig, resolve_mfa_enabled


class MfaConfigurationTests(unittest.TestCase):
    def test_packaged_configuration_enables_mfa_by_default(self):
        template = Path(t.__file__).parent / "subtitle_translation/examples/.env.example"
        lines = template.read_text(encoding="utf-8").splitlines()
        self.assertIn("MFA_ENABLED=1", lines)
        self.assertNotIn("MFA_ENABLED=0", lines)

    def test_env_flag_defaults_and_cli_precedence(self):
        self.assertFalse(resolve_mfa_enabled(None, {}))
        for value in ("1", "true", "YES", "on"):
            self.assertTrue(resolve_mfa_enabled(None, {"MFA_ENABLED": value}))
            self.assertFalse(resolve_mfa_enabled(False, {"MFA_ENABLED": value}))
        for value in ("0", "false", "no", "off", ""):
            self.assertFalse(resolve_mfa_enabled(None, {"MFA_ENABLED": value}))
            self.assertTrue(resolve_mfa_enabled(True, {"MFA_ENABLED": value}))
        with self.assertRaises(ValueError):
            resolve_mfa_enabled(None, {"MFA_ENABLED": "typo"})
        self.assertFalse(resolve_mfa_enabled(False, {"MFA_ENABLED": "typo"}))

    def test_parsers_preserve_unset_state_and_reject_conflicting_switches(self):
        for parser, args in ((pipeline.build_parser(), ["url"]), (batch_runtime.build_parser(), ["url"])):
            self.assertIsNone(parser.parse_args(args).mfa)
            for flag in ("--mfa", "--no-mfa"):
                with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                    parser.parse_args([*args, flag])

    def test_pipeline_env_and_no_mfa_are_reflected_in_dry_plan(self):
        for value, expected in (("1", True), ("0", False)):
            args = pipeline.build_parser().parse_args(["url"])
            with patch.object(ProjectConfig, "load", return_value=ProjectConfig(Path.cwd(), {"MFA_ENABLED": value, "DRY_RUN": "1"})), \
                 patch("sys.stdout", io.StringIO()) as output:
                self.assertEqual(pipeline.run_pipeline(args), 0)
            self.assertEqual(" -> mfa" in output.getvalue(), expected)

    def test_batch_env_and_override_are_reflected_in_stage_plan(self):
        values = {"MFA_ENABLED": "1"}
        self.assertIn("mfa", batch_runtime.batch_stage_plan(False, values))
        self.assertNotIn("mfa", batch_runtime.batch_stage_plan(False, values, use_mfa=False))
        plan = batch_runtime.batch_stage_plan(False, values)
        self.assertLess(plan.index("translate"), plan.index("mfa"))
        self.assertNotIn("source_events", plan)

    def test_translate_resolves_env_before_any_semantic_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.json"
            t.save_transcript(t.Transcript(str(path), "en", [t.TranscriptSegment(1, 0, 1, "Hello.")]), str(path))
            for value, expected in (("1", True), ("0", False)):
                seen = []
                def prepare(*args):
                    seen.append("mfa")
                    raise ValueError("preflight fixture")
                with patch.object(t, "load_env", return_value={"MFA_ENABLED": "1", "TRANSLATE_PROVIDER": "fake"}), \
                     patch.object(t, "prepare_mfa_review", side_effect=prepare), \
                     patch.object(t, "load_or_create_beautified", side_effect=ValueError("non-mfa path")), \
                     patch("sys.stderr", io.StringIO()):
                    try:
                        t.main([str(path)], stage_overrides={"MFA_ENABLED": value, })
                    except ValueError:
                        pass
                self.assertEqual(bool(seen), expected)


if __name__ == "__main__":
    unittest.main()
