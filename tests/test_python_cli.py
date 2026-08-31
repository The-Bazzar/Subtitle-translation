import pathlib
import tempfile
import unittest
from unittest import mock

from subtitle_translation.config import ProjectConfig
from subtitle_translation.process import CommandResult


class PythonCliTests(unittest.TestCase):
    def test_parser_exposes_all_public_commands(self):
        from subtitle_translation.cli import build_parser

        parser = build_parser()
        command_names = set(parser._subparsers._group_actions[0].choices)
        self.assertEqual(
            command_names,
            {
                "pipeline",
                "batch",
                "translate",
                "merge-ass",
                "download",
                "prepare-video",
                "whisper",
                "burn",
                "init",
            },
        )

    def test_translate_dispatches_arguments_without_shell_wrappers(self):
        from subtitle_translation import cli

        with mock.patch.object(cli.translate_srt, "main", return_value=7) as translate:
            result = cli.main(["translate", "input.json", "--no-split"])

        self.assertEqual(result, 7)
        translate.assert_called_once_with(["input.json", "--no-split"])

    def test_command_detection_does_not_consume_a_url_named_like_a_command(self):
        from subtitle_translation import cli

        with mock.patch.object(cli.pipeline, "main", return_value=0) as pipeline_main:
            result = cli.main(["pipeline", "translate"])

        self.assertEqual(result, 0)
        pipeline_main.assert_called_once_with(["translate"])

    def test_merge_ass_dispatches_arguments_without_shell_wrappers(self):
        from subtitle_translation import cli

        with mock.patch.object(cli.merge_ass, "main", return_value=0) as merge:
            result = cli.main(["merge-ass", "zh.ass", "en.ass"])

        self.assertEqual(result, 0)
        merge.assert_called_once_with(["zh.ass", "en.ass"])

    def test_project_config_prefers_process_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / ".env").write_text("TARGET_LANG=ja\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"TARGET_LANG": "de"}, clear=False):
                config = ProjectConfig.load(root)

        self.assertEqual(config.get("TARGET_LANG"), "de")

    def test_project_config_uses_project_directory_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / ".env").write_text("TARGET_LANG=ja\n", encoding="utf-8")
            with mock.patch.dict(
                "os.environ",
                {"SUBTITLE_TRANSLATION_PROJECT_DIR": str(root)},
                clear=False,
            ):
                config = ProjectConfig.load()

        self.assertEqual(config.project_dir, root.resolve())
        self.assertEqual(config.get("TARGET_LANG"), "ja")

    def test_project_config_separates_config_and_output_directories(self):
        with tempfile.TemporaryDirectory() as config_directory, tempfile.TemporaryDirectory() as output_directory:
            config_root = pathlib.Path(config_directory)
            output_root = pathlib.Path(output_directory)
            (config_root / ".env").write_text("TARGET_LANG=ja\n", encoding="utf-8")

            config = ProjectConfig.load(config_root, output_root)

        self.assertEqual(config.project_dir, config_root.resolve())
        self.assertEqual(config.output_dir, output_root.resolve())
        self.assertEqual(config.get("TARGET_LANG"), "ja")

    def test_pipeline_translation_output_uses_requested_language_suffixes(self):
        from argparse import Namespace

        from subtitle_translation import pipeline

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            json_path = root / "video.json"
            json_path.write_text('{"language":"fr","segments":[]}', encoding="utf-8")
            args = Namespace(
                source_lang="fr",
                target_lang="de",
                skip_beautify=False,
                skip_knowledge=False,
                no_proofread=False,
                no_split=False,
                quiet=True,
                aggressive=False,
                no_scene_snap=False,
                batch_size=None,
                split_max_chars=None,
                split_max_duration=None,
                split_context_window=None,
                scene_threshold=None,
                snap_frames=None,
                end_offset_frames=None,
                min_scene_interval_frames=None,
                min_duration=None,
                min_gap=None,
                max_gap_merge=None,
            )
            config = ProjectConfig(root, {})
            with mock.patch.object(pipeline.translate_srt, "main", return_value=0):
                result = pipeline._translate_main(json_path, None, config, args)

        self.assertTrue(result.success)
        self.assertEqual(result.outputs["ass"], str(root / "video.fr-de.ass"))

    def test_download_stage_returns_structured_video_path(self):
        from subtitle_translation import stages

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            config = ProjectConfig(root, {})

            def fake_run(args, **kwargs):
                output_template = pathlib.Path(args[args.index("-o") + 1])
                output_template.parent.mkdir(parents=True, exist_ok=True)
                output_template.with_name(output_template.name.replace("%(ext)s", "mp4")).write_bytes(b"video")
                return CommandResult(tuple(args), 0)

            with mock.patch("subtitle_translation.config.ProjectConfig.resolve_tool", return_value="yt-dlp"), mock.patch(
                "subtitle_translation.stages.capture_command",
                return_value=CommandResult(("yt-dlp", "--get-title"), 0, "A title\n"),
            ), mock.patch("subtitle_translation.stages.run_command", side_effect=fake_run):
                result = stages.download_video("https://example.invalid/video", config)

        self.assertTrue(result.success)
        self.assertEqual(pathlib.Path(result.outputs["render_video"]).suffix, ".mp4")
        self.assertTrue(pathlib.Path(result.outputs["render_video"]).name.endswith(".original.mp4"))

    def test_download_stage_writes_to_invocation_directory(self):
        from subtitle_translation import stages

        with tempfile.TemporaryDirectory() as config_directory, tempfile.TemporaryDirectory() as output_directory:
            config_root = pathlib.Path(config_directory)
            output_root = pathlib.Path(output_directory)
            config = ProjectConfig(config_root, {}, output_root)

            def fake_run(args, **kwargs):
                output_template = pathlib.Path(args[args.index("-o") + 1])
                output_template.parent.mkdir(parents=True, exist_ok=True)
                output_template.with_name(output_template.name.replace("%(ext)s", "mkv")).write_bytes(b"video")
                return CommandResult(tuple(args), 0)

            with mock.patch("subtitle_translation.config.ProjectConfig.resolve_tool", return_value="yt-dlp"), mock.patch(
                "subtitle_translation.stages.capture_command",
                return_value=CommandResult(("yt-dlp", "--get-title"), 0, "Output title\n"),
            ), mock.patch("subtitle_translation.stages.run_command", side_effect=fake_run):
                result = stages.download_video("https://example.invalid/video", config)

        output_path = pathlib.Path(result.outputs["render_video"])
        self.assertTrue(result.success)
        self.assertEqual(output_path.parent.parent, output_root.resolve())
        self.assertFalse((config_root / "Output title").exists())

    def test_pipeline_honors_pipeline_skip_burn(self):
        from subtitle_translation import pipeline
        from subtitle_translation.stages import StageResult

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            render_video = root / "video.original.mkv"
            edit_video = root / "video.mkv"
            json_path = root / "video.json"
            ass_path = root / "video.en-zh.ass"
            config = ProjectConfig(root, {"PIPELINE_SKIP_BURN": "1"}, root)
            args = pipeline.build_parser().parse_args(["https://example.invalid/video"])

            with mock.patch.object(pipeline.ProjectConfig, "load", return_value=config), mock.patch.object(
                pipeline, "download_video", return_value=StageResult.ok(render_video=str(render_video))
            ), mock.patch.object(
                pipeline, "prepare_video", return_value=StageResult.ok(edit_video=str(edit_video))
            ), mock.patch.object(
                pipeline, "transcribe_video", return_value=StageResult.ok(json=str(json_path))
            ), mock.patch.object(
                pipeline, "_translate_main", return_value=StageResult.ok(ass=str(ass_path))
            ), mock.patch.object(pipeline, "burn_video") as burn, mock.patch.object(
                pipeline, "emit_bell"
            ):
                code = pipeline.run_pipeline(args)

            self.assertEqual(code, 0)
            burn.assert_not_called()

    def test_prepare_stage_uses_argument_vector_and_structured_output(self):
        from subtitle_translation import stages

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            original = root / "sample.original.mkv"
            original.write_bytes(b"original")
            config = ProjectConfig(root, {})

            def fake_run(args, **kwargs):
                pathlib.Path(args[-1]).write_bytes(b"edit")
                return CommandResult(tuple(args), 0)

            with mock.patch("subtitle_translation.config.ProjectConfig.resolve_tool", return_value="ffmpeg"), mock.patch(
                "subtitle_translation.stages._has_nvidia", return_value=False
            ), mock.patch("subtitle_translation.stages.run_command", side_effect=fake_run) as run:
                result = stages.prepare_video(original, config)

        self.assertTrue(result.success)
        self.assertEqual(pathlib.Path(result.outputs["edit_video"]).name, "sample.mkv")
        command = run.call_args.args[0]
        self.assertIsInstance(command, list)
        self.assertIn("-map_metadata", command)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_init_copies_packaged_examples_without_overwriting(self):
        from subtitle_translation import cli

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / ".env").write_text("TARGET_LANG=ja\n", encoding="utf-8")
            self.assertEqual(cli.main(["init", "--directory", str(root)]), 0)
            self.assertEqual((root / ".env").read_text(encoding="utf-8"), "TARGET_LANG=ja\n")
            self.assertTrue((root / "providers.json").is_file())
            self.assertTrue((root / "template.ass").is_file())


if __name__ == "__main__":
    unittest.main()
