import pathlib
import tempfile
import unittest
from unittest import mock

from subtitle_translation.config import ProjectConfig
from subtitle_translation.process import CommandResult
from subtitle_translation.stages import burn_video


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


class BurnStageTests(unittest.TestCase):
    def test_wrappers_forward_only_project_arguments(self):
        for name in ("ffmpeg-burn", "mpv-burn"):
            for suffix in ("ps1", "sh"):
                content = (SCRIPTS / f"{name}.{suffix}").read_text(encoding="utf-8")
                with self.subTest(wrapper=f"{name}.{suffix}"):
                    self.assertIn("burn", content)
                    self.assertNotIn("--backend", content)

    def test_mpv_stage_builds_an_argument_vector(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            video = root / "video.mkv"
            subtitle = root / "video.ass"
            video.write_bytes(b"video")
            subtitle.write_text("ass", encoding="utf-8")
            with mock.patch("subtitle_translation.stages.ProjectConfig.resolve_tool", return_value="mpv"), mock.patch("subtitle_translation.stages._source_bitrate_kbps", return_value=1000), mock.patch(
                "subtitle_translation.stages.run_command",
                return_value=CommandResult(("mpv",), 0),
            ) as run:
                result = burn_video(
                    video,
                    subtitle,
                    ProjectConfig(root, {"MPV_PATH_WIN": "mpv"}),
                    backend="mpv",
                    dry_run=True,
                )
        self.assertTrue(result.success)
        self.assertFalse(run.called)


if __name__ == "__main__":
    unittest.main()
