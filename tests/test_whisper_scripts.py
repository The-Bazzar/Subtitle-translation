import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class WhisperScriptTests(unittest.TestCase):
    def test_whisper_scripts_support_hugging_face_proxy(self):
        expectations = {
            "whisper.ps1": [
                r"Get-EnvValue 'HF_PROXY' \(Get-EnvValue 'YTDLP_PROXY' ''\)",
                r'\$env:HTTP_PROXY\s*=\s*\$HfProxy',
                r'\$env:HTTPS_PROXY\s*=\s*\$HfProxy',
            ],
            "whisper.sh": [
                r'HF_PROXY="\$\{HF_PROXY:-\$\{YTDLP_PROXY:-\}\}"',
                r'export HTTP_PROXY="\$HF_PROXY"',
                r'export HTTPS_PROXY="\$HF_PROXY"',
            ],
        }
        for script, patterns in expectations.items():
            content = (ROOT / script).read_text(encoding="utf-8")
            for pattern in patterns:
                with self.subTest(script=script, pattern=pattern):
                    self.assertRegex(content, pattern)

    def test_windows_whisper_uses_configured_ffmpeg(self):
        content = (ROOT / "whisper.ps1").read_text(encoding="utf-8")
        self.assertRegex(content, r"Get-EnvValue 'FFMPEG_PATH_WIN'\s+'ffmpeg'")
        self.assertRegex(content, r'& \$Ffmpeg -i \$VideoAbs')


if __name__ == "__main__":
    unittest.main()
