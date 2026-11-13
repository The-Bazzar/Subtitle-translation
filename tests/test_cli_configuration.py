import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import batch_runtime
import translate_srt as t
from subtitle_translation import cli, pipeline, stages
from subtitle_translation.config import ProjectConfig


class CliConfigurationTests(unittest.TestCase):
    def test_invalid_numeric_settings_fail_before_model_or_media_work(self):
        for values in ({"SCENE_THRESHOLD": "nan"}, {"SCENE_THRESHOLD": "2"},
                       {"TRANSLATE_BATCH_SIZE": "0"}, {"TRANSLATE_CONTEXT_WINDOW": "-1"},
                       {"SCENE_MIN_INTERVAL_FRAMES": "-1"}):
            with self.subTest(values=values), patch.object(t, "load_env", return_value={}), \
                 patch("sys.stderr", io.StringIO()), patch.object(t, "load_transcript") as read, self.assertRaises(SystemExit) as error:
                t.main(["missing.json"], stage_overrides=values)
            self.assertEqual(error.exception.code, 2)
            read.assert_not_called()
    def test_help_prints_all_real_subcommand_help_without_stage_execution(self):
        output = io.StringIO()
        with patch("sys.stdout", output), patch.object(stages, "download_video") as download, \
             patch.object(stages, "transcribe_video") as whisper, patch.object(t, "load_env", return_value={}):
            self.assertEqual(cli.main(["help"]), 0)
        for command in cli.COMMANDS:
            if command != "help":
                self.assertIn(f"usage: subtitle-translation {command}", output.getvalue())
        download.assert_not_called()
        whisper.assert_not_called()

    def test_old_switches_and_tuning_parameters_are_rejected_by_public_parsers(self):
        for switch, value in (("--mfa", []), ("--no-mfa", []), ("--proofread", []),
                              ("--no-proofread", []), ("--skip-knowledge", []), ("--skip-burn", []),
                              ("--no-split", []), ("--force", []), ("--quiet", []), ("--dry-run", []),
                              ("--batch-size", ["8"]), ("--scene-threshold", ["0.2"])):
            with self.subTest(switch=switch), patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    t.main(["missing.json", switch, *value])
                self.assertEqual(caught.exception.code, 2)
        for parser, argv in ((pipeline.build_parser(), ["url"]), (batch_runtime.build_parser(), ["url"])):
            for flag in ("--mfa", "--no-proofread", "--dry-run", "--skip-burn"):
                with self.subTest(flag=flag), patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                    parser.parse_args([*argv, flag])

    def test_translate_tuning_and_switches_are_read_from_env(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "video.json"
            source.write_text(json.dumps({"language": "en", "segments": [{"text": "Hello.", "start": 0, "end": 1}]}))
            seen = []
            def capture(ctx, source, video, options, skip, force, **kwargs):
                seen.append((ctx.source_lang_code, ctx.target_lang_code, options.scene_threshold,
                             options.min_scene_interval_frames, skip, force))
                raise ValueError("stop before actual stage")
            with patch.object(t, "load_env", return_value={}), patch.object(t, "load_or_create_beautified", side_effect=capture):
                with self.assertRaises(ValueError):
                    t.main([str(source), "--lang", "en", "--target-lang", "ja"], stage_overrides={
                        "SCENE_THRESHOLD": "0.22", "SCENE_MIN_INTERVAL_FRAMES": "7", "FORCE_REBUILD": "1",
                        "PIPELINE_SKIP_SCENE_DETECTION": "1", })
            self.assertEqual(seen, [("en", "ja", .22, 7, True, True)])

    def test_pipeline_forwards_config_snapshot_not_removed_cli_switches(self):
        args = pipeline.build_parser().parse_args(["url", "--lang", "en", "--target-lang", "zh"])
        config = ProjectConfig(Path.cwd(), {"MFA_ENABLED": "1", "PROOFREAD": "0", "TRANSLATE_BATCH_SIZE": "8"})
        with patch.object(pipeline.translate_srt, "main", return_value=0) as main:
            self.assertTrue(pipeline._translate_main(Path("video.json"), Path("video.mkv"), config, args).success)
        argv = main.call_args.args[0]
        self.assertNotIn("--mfa", argv)
        self.assertNotIn("--no-proofread", argv)
        self.assertNotIn("--batch-size", argv)
        self.assertEqual(main.call_args.kwargs["stage_overrides"], config.values)

    def test_only_scenes_remains_available_without_loading_a_model(self):
        with tempfile.TemporaryDirectory() as directory:
            source, video = Path(directory) / "video.json", Path(directory) / "video.mkv"
            source.write_text(json.dumps({"language": "en", "segments": [{"text": "Hello.", "start": 0, "end": 1}]}))
            video.write_bytes(b"fixture")
            with patch.object(t, "load_env", return_value={}), patch.object(t, "get_frame_rate", return_value=24), \
                 patch.object(t, "get_scene_changes", return_value=[.5]), patch.object(t, "translate_llm_from_env") as model, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertIsNone(t.main([str(source), "--video", str(video), "--only-scenes"], stage_overrides={
                    "PIPELINE_SKIP_SCENE_DETECTION": "1", }))
            model.assert_not_called()
            self.assertTrue(source.with_suffix(".scenes.json").is_file())

    def test_standalone_whisper_and_burn_no_longer_accept_tuning(self):
        for function, arguments in ((stages.main_whisper, ["video", "--model", "small"]),
                                    (stages.main_burn, ["video", "subtitle", "--backend", "mpv"])):
            with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as caught:
                function(arguments)
            self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
