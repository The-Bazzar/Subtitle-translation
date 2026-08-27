import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


class SetupAndDocumentationTests(unittest.TestCase):
    def test_repository_root_uses_core_and_scripts_layout(self):
        self.assertFalse(list(ROOT.glob("*.ps1")))
        self.assertFalse(list(ROOT.glob("*.sh")))
        self.assertTrue((ROOT / "core" / "subtitle_translation").is_dir())
        self.assertTrue(SCRIPTS.is_dir())
        self.assertFalse((ROOT / "misc" / "examples").exists())

    def test_setup_scripts_recreate_project_venv_and_sync_project(self):
        for name in ("setup.ps1", "setup.sh"):
            content = (SCRIPTS / name).read_text(encoding="utf-8")
            with self.subTest(name=name):
                self.assertRegex(content, r"uv\s+venv\s+\.venv\s+--clear")
                self.assertIn("uv sync", content)
                self.assertIn(".env.example", content)
                self.assertIn("template.ass", content)
                self.assertIn("-m subtitle_translation", content)
                self.assertIn("subtitle_translation", content)
                self.assertIn("examples", content)

    def test_setup_installs_project_cli_shim(self):
        powershell = (SCRIPTS / "setup.ps1").read_text(encoding="utf-8")
        shell = (SCRIPTS / "setup.sh").read_text(encoding="utf-8")

        self.assertIn("subtitle-translation.cmd", powershell)
        self.assertIn("SubtitleTranslation\\bin", powershell)
        self.assertIn("SetEnvironmentVariable", powershell)
        self.assertIn('--project-dir "$ProjectPath"', powershell)
        self.assertIn("/usr/local/bin/subtitle-translation", shell)
        self.assertIn('install_cli_shim', shell)
        self.assertIn('--project-dir %q', shell)

    def test_setup_sh_uses_posix_awk_for_env_upgrade(self):
        content = (SCRIPTS / "setup.sh").read_text(encoding="utf-8")
        self.assertNotRegex(content, r"match\([^\n]+,[^\n]+,[^\n]+\)")
        self.assertIn('existing[key] = 1', content)

    def test_setup_uses_packaged_examples_as_single_source(self):
        package_examples = ROOT / "core" / "subtitle_translation" / "examples"
        self.assertTrue((package_examples / ".env.example").is_file())
        self.assertTrue((package_examples / "providers.example.json").is_file())
        self.assertTrue((package_examples / "template.ass.example").is_file())

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
