import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def read_script(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class ProjectLauncherTests(unittest.TestCase):
    def test_windows_launchers_resolve_from_script_directory_and_forward_args(self):
        for script_name, python_script in (
            ("merge_ass.ps1", "merge_ass.py"),
            ("translate_srt.ps1", "translate_srt.py"),
        ):
            with self.subTest(script_name=script_name):
                script = read_script(script_name)
                self.assertIn("$PSScriptRoot", script)
                self.assertIn(f"'{python_script}'", script)
                self.assertIn("@PythonArgs", script)
                self.assertIn("exit $LASTEXITCODE", script)

    def test_shell_launchers_resolve_from_script_directory_and_forward_args(self):
        for script_name, python_script in (
            ("merge_ass.sh", "merge_ass.py"),
            ("translate_srt.sh", "translate_srt.py"),
        ):
            with self.subTest(script_name=script_name):
                script = read_script(script_name)
                self.assertIn(
                    'SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"',
                    script,
                )
                self.assertIn(f'SCRIPT_PATH="$SCRIPT_DIR/{python_script}"', script)
                self.assertIn('exec "$PYTHON_BIN" "$SCRIPT_PATH" "$@"', script)

    def test_pipelines_call_local_launchers_instead_of_python_directly(self):
        powershell = read_script("pipeline.ps1")
        shell = read_script("pipeline.sh")

        self.assertIn('$TranslatePs1 = Join-Path $ScriptDir "translate_srt.ps1"', powershell)
        self.assertNotIn("$PythonExe $TranslatePy", powershell)
        self.assertNotIn("$PythonExe =", powershell)
        self.assertIn('TRANSLATE_SCRIPT="$SCRIPT_DIR/translate_srt.sh"', shell)
        self.assertNotIn('"$PYTHON_BIN" "$TRANSLATE_SCRIPT"', shell)


if __name__ == "__main__":
    unittest.main()
