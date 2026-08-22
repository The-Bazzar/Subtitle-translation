import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class CompatibilityWrapperTests(unittest.TestCase):
    def test_download_and_prepare_are_separate_cli_commands(self):
        download = (ROOT / "download.ps1").read_text(encoding="utf-8")
        prepare = (ROOT / "prepare-video.ps1").read_text(encoding="utf-8")
        self.assertIn("-m subtitle_translation download", download)
        self.assertIn("-m subtitle_translation prepare-video", prepare)
        self.assertNotIn("prepare-video", download.split("-m subtitle_translation download", 1)[1])

    def test_pipeline_wrapper_does_not_reimplement_stage_order(self):
        for suffix in ("ps1", "sh"):
            content = (ROOT / f"pipeline.{suffix}").read_text(encoding="utf-8")
            self.assertIn("-m subtitle_translation pipeline", content)
            self.assertNotIn("OUTPUT_RENDER_VIDEO", content)
            self.assertNotIn("OUTPUT_VIDEO", content)

    def test_whisper_wrapper_routes_only_to_json_stage(self):
        for suffix in ("ps1", "sh"):
            content = (ROOT / f"whisper.{suffix}").read_text(encoding="utf-8")
            self.assertIn("-m subtitle_translation whisper", content)
            self.assertNotIn("--output_format", content)


if __name__ == "__main__":
    unittest.main()
