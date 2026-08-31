import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import batch_runtime
from batch_scheduler import BatchTask, ResourceLimits
from subtitle_translation.stages import StageResult


ROOT = pathlib.Path(__file__).resolve().parents[1]


class PythonCliSmokeTests(unittest.TestCase):
    def test_module_cli_help_and_dry_run_are_dependency_free(self):
        help_result = subprocess.run(
            [sys.executable, "-m", "subtitle_translation", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        dry_run = subprocess.run(
            [sys.executable, "-m", "subtitle_translation", "batch", "--dry-run", "url"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        self.assertNotIn("\a", dry_run.stdout + dry_run.stderr)

    def test_direct_stage_runner_returns_structured_paths(self):
        root = pathlib.Path(self._testMethodName)
        env = {"FFMPEG_PATH_WIN": "ffmpeg"}
        with mock.patch(
            "batch_runtime.download_video",
            return_value=StageResult.ok(render_video="render.mkv"),
        ), mock.patch(
            "batch_runtime.prepare_video",
            return_value=StageResult.ok(edit_video="edit.mkv"),
        ), mock.patch(
            "batch_runtime.extract_audio",
            return_value=StageResult.ok(wav="audio.wav"),
        ):
            runners = batch_runtime.create_platform_runners(root, env)
            self.assertEqual(asyncio_run(runners.download("url")), "render.mkv")
            self.assertEqual(asyncio_run(runners.prepare("render.mkv")), "edit.mkv")
            self.assertEqual(asyncio_run(runners.extract_audio("edit.mkv")), "audio.wav")

    def test_resource_limits_are_automatic_and_nvenc_is_fixed(self):
        limits = ResourceLimits.detect(logical_cpus=32)
        self.assertEqual(limits.cpu_io, 8)
        self.assertEqual(limits.nvenc, 4)

    def test_batch_postprocess_reuses_glossary_cache(self):
        task, root, temporary_directory = self._postprocess_task()
        self.addCleanup(temporary_directory.cleanup)
        calls = []

        def fake_main(arguments):
            calls.append(list(arguments))
            if "--only-beautify" in arguments:
                pathlib.Path(arguments[arguments.index("--beautified-json") + 1]).write_text(
                    "{}", encoding="utf-8"
                )
            elif "--only-glossary" not in arguments:
                (root / "video.en-zh.ass").write_text("ass", encoding="utf-8")
            return 0

        with mock.patch.dict("os.environ", {}, clear=False), mock.patch.object(
            batch_runtime.translate_srt, "main", side_effect=fake_main
        ):
            runner = batch_runtime.create_platform_postprocess_runner(
                root,
                {"SOURCE_LANG": "en", "TARGET_LANG": "zh"},
            )
            asyncio_run(runner(task))

        self.assertEqual(len(calls), 3)
        self.assertIn("--reuse-glossary", calls[1])
        self.assertEqual(task.ass_path, root / "video.en-zh.ass")

    def test_batch_postprocess_honors_beautify_and_knowledge_skips(self):
        task, root, temporary_directory = self._postprocess_task()
        self.addCleanup(temporary_directory.cleanup)
        calls = []

        def fake_main(arguments):
            calls.append(list(arguments))
            if "--only-beautify" in arguments:
                pathlib.Path(arguments[arguments.index("--beautified-json") + 1]).write_text(
                    "{}", encoding="utf-8"
                )
            else:
                (root / "video.en-zh.ass").write_text("ass", encoding="utf-8")
            return 0

        env = {
            "SOURCE_LANG": "en",
            "TARGET_LANG": "zh",
            "PIPELINE_SKIP_BEAUTIFY": "1",
            "PIPELINE_SKIP_KNOWLEDGE": "1",
        }
        with mock.patch.dict("os.environ", {}, clear=False), mock.patch.object(
            batch_runtime.translate_srt, "main", side_effect=fake_main
        ):
            runner = batch_runtime.create_platform_postprocess_runner(root, env)
            asyncio_run(runner(task))

        self.assertEqual(len(calls), 2)
        self.assertIn("--skip-beautify", calls[0])
        self.assertFalse(any("--only-glossary" in call for call in calls))

    def test_batch_skip_translate_requires_existing_bilingual_ass(self):
        task, root, temporary_directory = self._postprocess_task()
        self.addCleanup(temporary_directory.cleanup)
        ass_path = root / "video.en-zh.ass"
        ass_path.write_text("ass", encoding="utf-8")
        env = {
            "SOURCE_LANG": "en",
            "TARGET_LANG": "zh",
            "PIPELINE_SKIP_TRANSLATE": "1",
        }
        with mock.patch.dict("os.environ", {}, clear=False), mock.patch.object(
            batch_runtime.translate_srt, "main"
        ) as translate:
            runner = batch_runtime.create_platform_postprocess_runner(root, env)
            asyncio_run(runner(task))

        translate.assert_not_called()
        self.assertEqual(task.ass_path, ass_path)

    @staticmethod
    def _postprocess_task():
        temporary_directory = tempfile.TemporaryDirectory()
        root = pathlib.Path(temporary_directory.name)
        edit_video = root / "video.mkv"
        json_path = root / "video.json"
        candidate_path = root / ".video.beautified.candidate.json"
        edit_video.write_bytes(b"edit")
        json_path.write_text('{"language":"en","segments":[]}', encoding="utf-8")
        task = BatchTask(
            index=1,
            url="url",
            edit_video_path=edit_video,
            json_path=json_path,
            beautified_candidate_path=candidate_path,
            detected_language="en",
        )
        return task, root, temporary_directory


def asyncio_run(awaitable):
    import asyncio

    return asyncio.run(awaitable)


if __name__ == "__main__":
    unittest.main()
