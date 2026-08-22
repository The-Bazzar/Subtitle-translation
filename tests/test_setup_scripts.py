import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class SetupAndDocumentationTests(unittest.TestCase):
    def test_setup_scripts_recreate_project_venv_and_sync_project(self):
        for name in ("setup.ps1", "setup.sh"):
            content = (ROOT / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertRegex(content, r"uv\s+venv\s+\.venv\s+--clear")
                self.assertIn("uv sync", content)
                self.assertIn(".env.example", content)
                self.assertIn("template.ass", content)
                self.assertIn("-m subtitle_translation", content)

    def test_docs_describe_the_python_cli_and_resource_invariants(self):
        documents = [
            (ROOT / "AGENTS.md").read_text(encoding="utf-8"),
            (ROOT / "README.md").read_text(encoding="utf-8"),
            (ROOT / "MIGRATION.md").read_text(encoding="utf-8"),
        ]
        for content in documents:
            self.assertIn("subtitle-translation pipeline", content)
            self.assertIn("max(1, (os.cpu_count() or 1) // 4)", content)
            self.assertIn("WhisperX `.json`", content)
            self.assertIn("glossary", content)
            self.assertIn("Ctrl+C", content)

    def test_local_configuration_files_are_explicitly_excluded(self):
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for name in (".env", "providers.json", "cookies.txt", "template.ass", "chroma_db"):
            self.assertIn(name, agents)


if __name__ == "__main__":
    unittest.main()
