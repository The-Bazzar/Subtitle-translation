import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def read_script(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class BurnScriptTests(unittest.TestCase):
    def test_burn_scripts_default_to_source_bitrate_and_aac_audio(self):
        expectations = {
            "ffmpeg-burn.ps1": [
                r'\$Ovcopts\s*=\s*"source-bitrate"',
                r'\$Oac\s*=\s*"aac"',
            ],
            "mpv-burn.ps1": [
                r'\$Ovcopts\s*=\s*"source-bitrate"',
                r'\$Oac\s*=\s*"aac"',
            ],
            "ffmpeg-burn.sh": [
                r'OVCOPTS="source-bitrate"',
                r'OAC="aac"',
            ],
            "mpv-burn.sh": [
                r'OVCOPTS="source-bitrate"',
                r'OAC="aac"',
            ],
            "pipeline.ps1": [
                r'\$Ovcopts\s*=\s*"source-bitrate"',
                r'\$Oac\s*=\s*"aac"',
                r"Merge-EnvDefault 'BURN_OVCOPTS' '' 'source-bitrate'",
                r"Merge-EnvDefault 'BURN_OAC' '' 'aac'",
            ],
            "pipeline.sh": [
                r'BURN_OVCOPTS="\$\{BURN_OVCOPTS:-source-bitrate\}"',
                r'BURN_OAC="\$\{BURN_OAC:-aac\}"',
            ],
        }

        for script, patterns in expectations.items():
            content = read_script(script)
            for pattern in patterns:
                with self.subTest(script=script, pattern=pattern):
                    self.assertRegex(content, pattern)

    def test_burn_scripts_probe_source_video_bitrate(self):
        for script in ("ffmpeg-burn.ps1", "ffmpeg-burn.sh", "mpv-burn.ps1", "mpv-burn.sh"):
            content = read_script(script).lower()
            with self.subTest(script=script):
                self.assertIn("ffprobe", content)
                self.assertIn("source-bitrate", content)
                self.assertIn("maxrate", content)
                self.assertIn("bufsize", content)


if __name__ == "__main__":
    unittest.main()
