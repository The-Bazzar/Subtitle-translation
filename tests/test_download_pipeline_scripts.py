import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def read_script(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class DownloadPipelineScriptTests(unittest.TestCase):
    def test_download_scripts_emit_edit_and_render_video_paths(self):
        expectations = {
            "download.ps1": [
                r'Write-Output\s+"OUTPUT_VIDEO=',
                r'Write-Output\s+"OUTPUT_RENDER_VIDEO=',
            ],
            "download.sh": [
                r'echo\s+"OUTPUT_VIDEO=',
                r'echo\s+"OUTPUT_RENDER_VIDEO=',
            ],
        }

        for script, patterns in expectations.items():
            content = read_script(script)
            for pattern in patterns:
                with self.subTest(script=script, pattern=pattern):
                    self.assertRegex(content, pattern)

    def test_download_scripts_reencode_edit_video_with_frame_pipe_defaults(self):
        expectations = {
            "download.ps1": [
                r'yuv4mpegpipe',
                r'pipe:0',
                r'hevc_nvenc',
                r'aresample=async=1',
                r'original\$OriginalExt|\.original\.',
            ],
            "download.sh": [
                r'yuv4mpegpipe',
                r'pipe:0|-i -',
                r'hevc_nvenc',
                r'aresample=async=1',
                r'\.original\.',
            ],
        }

        for script, patterns in expectations.items():
            content = read_script(script)
            for pattern in patterns:
                with self.subTest(script=script, pattern=pattern):
                    self.assertRegex(content, pattern)
            with self.subTest(script=script, assertion="no_hwaccel_decode"):
                self.assertNotIn("hwaccel", content.lower())

    def test_pipeline_scripts_use_render_video_for_burn(self):
        expectations = {
            "pipeline.ps1": [
                r'OUTPUT_RENDER_VIDEO=',
                r'\$RenderVideoPath',
                r'VideoPath\s*=\s*\$RenderVideoPath',
            ],
            "pipeline.sh": [
                r'OUTPUT_RENDER_VIDEO=',
                r'RENDER_VIDEO_PATH=',
                r'"\$RENDER_VIDEO_PATH"',
            ],
        }

        for script, patterns in expectations.items():
            content = read_script(script)
            for pattern in patterns:
                with self.subTest(script=script, pattern=pattern):
                    self.assertRegex(content, pattern)


if __name__ == "__main__":
    unittest.main()
