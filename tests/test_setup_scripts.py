import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


def read_script(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class SetupScriptTests(unittest.TestCase):
    def test_glossary_budget_upgrade_preserves_legacy_tavily_value(self):
        env_example = read_script(".env.example")
        self.assertRegex(env_example, r"(?m)^GLOSSARY_SEARCH_MAX_QUERIES=$")
        self.assertRegex(env_example, r"(?m)^TAVILY_MAX_QUERIES=15$")
        for script in ("setup.ps1", "setup.sh"):
            with self.subTest(script=script):
                body = read_script(script)
                self.assertIn(".env.example", body)
                self.assertRegex(body, r"(?i)missing")

    def test_translate_context_window_is_in_env_upgrade_chain(self):
        env_example = read_script(".env.example")
        self.assertIn("TRANSLATE_CONTEXT_WINDOW=1", env_example)
        for script in ("setup.ps1", "setup.sh"):
            with self.subTest(script=script):
                body = read_script(script)
                self.assertIn(".env.example", body)
                self.assertRegex(body, r"(?i)missing")

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
