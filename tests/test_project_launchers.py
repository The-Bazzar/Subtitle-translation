import pathlib
import re
import shutil
import subprocess
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
BASH = shutil.which("bash")


def read(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


class ProjectLauncherTests(unittest.TestCase):
    def test_python_cli_is_the_single_implementation(self):
        for name in (
            "pipeline.ps1", "pipeline.sh", "download.ps1", "download.sh",
            "prepare-video.ps1", "prepare-video.sh", "whisper.ps1", "whisper.sh",
            "ffmpeg-burn.ps1", "ffmpeg-burn.sh", "mpv-burn.ps1", "mpv-burn.sh",
            "translate_srt.ps1", "translate_srt.sh", "merge_ass.ps1", "merge_ass.sh",
            "batch.ps1", "batch.sh",
        ):
            with self.subTest(name=name):
                content = read(name)
                self.assertIn("-m subtitle_translation", content)
                self.assertNotIn("translate_srt.py", content)
                self.assertNotIn("batch_runtime.py", content)

    def test_wrapper_pairs_have_matching_stage_commands(self):
        expected = {
            "pipeline": "pipeline",
            "download": "download",
            "prepare-video": "prepare-video",
            "whisper": "whisper",
            "ffmpeg-burn": "burn",
            "mpv-burn": "burn",
            "translate_srt": "translate",
            "merge_ass": "merge-ass",
            "batch": "batch",
        }
        for name, command in expected.items():
            with self.subTest(name=name):
                for content in (read(f"{name}.ps1"), read(f"{name}.sh")):
                    self.assertIn("-m subtitle_translation", content)
                    self.assertIn("--project-dir", content)
                    self.assertRegex(content, rf"\b{re.escape(command)}\b")

    def test_mpv_wrappers_select_mpv_backend(self):
        self.assertIn("--backend mpv", read("mpv-burn.ps1"))
        self.assertIn("--backend mpv", read("mpv-burn.sh"))
        self.assertIn('"mpv-burn" = "burn"', read("py_launcher.ps1"))
        self.assertIn("mpv-burn)", read("py_launcher.sh"))

    def test_launcher_maps_legacy_names_without_script_paths(self):
        powershell = read("py_launcher.ps1")
        shell = read("py_launcher.sh")
        for target in ("translate_srt", "merge_ass", "batch"):
            self.assertIn(target, powershell)
            self.assertIn(target, shell)
        self.assertNotIn("$ScriptDir/$script_name", shell)
        self.assertNotIn("translate_srt.py", powershell)

    def test_bash_wrappers_are_syntactically_valid(self):
        if not BASH or sys.platform == "win32":
            self.skipTest("requires bash")
        for path in SCRIPTS.glob("*.sh"):
            with self.subTest(path=path.name):
                result = subprocess.run([BASH, "-n", str(path)], capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_help_has_no_manual_worker_count_options(self):
        result = subprocess.run(
            [sys.executable, "-m", "subtitle_translation", "batch", "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(re.search(r"(?:^|\s)(?:-j|--jobs|--io-jobs|-MaxJobs)(?:\s|$)", result.stdout))


if __name__ == "__main__":
    unittest.main()
