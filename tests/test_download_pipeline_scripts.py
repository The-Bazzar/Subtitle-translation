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

    def test_download_scripts_reencode_edit_video_with_encoder_preference(self):
        expectations = {
            "download.ps1": [
                r'Test-NvidiaAvailable',
                r'Test-FfmpegEncoder',
                r'Test-NonEmptyFile',
                r'h264_nvenc',
                r"'-preset',\s*'p7'",
                r"'-cq',\s*'19'",
                r'libx264',
                r"'-crf',\s*'19'",
                r'aresample=async=1:out_sample_fmt=s16',
                r"'-c:a',\s*'flac'",
                r'original\$OriginalExt|\.original\.',
                r'\$FolderName\.mkv',
            ],
            "download.sh": [
                r'nvidia_available',
                r'ffmpeg_encoder_available',
                r'h264_nvenc',
                r'-preset p7',
                r'-cq 19',
                r'libx264',
                r'-crf 19',
                r'aresample=async=1:out_sample_fmt=s16',
                r'-c:a flac',
                r'\.original\.',
                r'\$FOLDER_NAME\.mkv',
            ],
        }

        for script, patterns in expectations.items():
            content = read_script(script)
            for pattern in patterns:
                with self.subTest(script=script, pattern=pattern):
                    self.assertRegex(content, pattern)
            with self.subTest(script=script, assertion="no_hwaccel_decode"):
                self.assertNotIn("hwaccel", content.lower())
            with self.subTest(script=script, assertion="no_frame_pipe"):
                self.assertNotIn("yuv4mpegpipe", content)
                self.assertNotIn("pipe:0", content)
                self.assertNotIn("Invoke-FfmpegFramePipeAttempt", content)

    def test_download_scripts_surface_ffmpeg_commands_and_diagnostics(self):
        expectations = {
            "download.ps1": [
                r'ffmpeg cmd:',
            ],
            "download.sh": [
                r'ffmpeg cmd:',
            ],
        }

        for script, patterns in expectations.items():
            content = read_script(script)
            for pattern in patterns:
                with self.subTest(script=script, pattern=pattern):
                    self.assertRegex(content, pattern)

    def test_download_scripts_accept_nonempty_nvenc_output_after_nonzero_ffmpeg_exit(self):
        expectations = {
            "download.ps1": [
                r"\$attempt\.Name\s+-eq\s+'h264_nvenc'",
                r'Test-NonEmptyFile\s+-Path\s+\$OutputPath',
                r'返回 exit=\$lastExitCode',
                r'非 0B 文件',
            ],
            "download.sh": [
                r'\[ "\$label" = "h264_nvenc" \]',
                r'\[ -s "\$output_path" \]',
                r'返回非零退出码',
                r'非 0B 文件',
            ],
        }

        for script, patterns in expectations.items():
            content = read_script(script)
            for pattern in patterns:
                with self.subTest(script=script, pattern=pattern):
                    self.assertRegex(content, pattern)

            with self.subTest(script=script, assertion="no_probe_gate"):
                self.assertNotIn("ffprobe 校验", content)
                self.assertNotIn("stream=codec_type", content)
                self.assertNotIn("format=duration", content)

    def test_download_scripts_reuse_existing_original_mkv_for_metadata_refresh(self):
        expectations = {
            "download.ps1": [
                r'\$ExistingOriginalMkv\s*=\s*Join-Path\s+\$FolderName\s+"\$FolderName\.original\.mkv"',
                r'\$HasExistingOriginalMkv\s*=\s*Test-Path\s+\$ExistingOriginalMkv\s+-PathType\s+Leaf',
                r"'--skip-download'",
                r'if\s*\(\$HasExistingOriginalMkv\)',
                r'使用已有原片',
                r'Move-Item\s+-LiteralPath\s+\$OriginalVideoAbs\s+-Destination\s+\$RenderVideoPath\s+-Force',
            ],
            "download.sh": [
                r'EXISTING_ORIGINAL_MKV="\$FOLDER_NAME/\$FOLDER_NAME\.original\.mkv"',
                r'HAS_EXISTING_ORIGINAL_MKV=true',
                r'--skip-download',
                r'使用已有原片',
                r'mv -f "\$ORIGINAL_VIDEO_PATH" "\$RENDER_VIDEO_PATH"',
            ],
        }

        for script, patterns in expectations.items():
            content = read_script(script)
            for pattern in patterns:
                with self.subTest(script=script, pattern=pattern):
                    self.assertRegex(content, pattern)

    def test_download_scripts_do_not_forward_ffmpeg_stderr(self):
        for script in ("download.ps1", "download.sh"):
            content = read_script(script)
            with self.subTest(script=script):
                self.assertNotIn("BeginErrorReadLine", content)
                self.assertNotIn("ErrorDataReceived", content)
                self.assertNotIn("[ffmpeg decode]", content)
                self.assertNotIn("[ffmpeg encode]", content)
                self.assertNotRegex(content, r'2>\s*>\(')

    def test_download_ps1_handles_missing_url_in_script_body(self):
        content = read_script("download.ps1")
        self.assertNotRegex(
            content,
            r'\[Parameter\(\s*Mandatory[^)]*Position\s*=\s*0[^)]*HelpMessage\s*=\s*"YouTube video URL"\)\]\s*\r?\n\s*\[string\]\$Url',
        )
        self.assertRegex(content, r'if\s*\(\$Help\s*-or\s*\(-not\s*\$Url\)\)')

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
