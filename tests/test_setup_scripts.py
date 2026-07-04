import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def read_script(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class SetupScriptTests(unittest.TestCase):
    def test_setup_scripts_clear_project_venv_before_recreating(self):
        expectations = {
            "setup.ps1": r"uv\s+venv\s+\.venv\s+--clear\s+--python\s+3\.13\.12",
            "setup.sh": r"uv\s+venv\s+\.venv\s+--clear\s+--python\s+3\.13\.12",
        }
        for script, pattern in expectations.items():
            with self.subTest(script=script):
                self.assertRegex(read_script(script), pattern)


if __name__ == "__main__":
    unittest.main()
