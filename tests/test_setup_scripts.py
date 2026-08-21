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

    def test_setup_scripts_create_the_project_environment_used_by_launchers(self):
        for script in ("setup.ps1", "setup.sh"):
            content = read_script(script)
            with self.subTest(script=script, contract="project-venv"):
                self.assertIn(".venv", content)
                self.assertIn("langcodes", content)


class BatchMigrationDocumentationTests(unittest.TestCase):
    def test_migration_has_explicit_resource_recovery_and_interrupt_sections(self):
        migration = read_script("MIGRATION.md")

        for heading in (
            "### 资源与 GPU waves",
            "### ASR 恢复与锁",
            "### 故障与两阶段中断",
            "### Release smoke 限制",
        ):
            with self.subTest(heading=heading):
                self.assertIn(heading, migration)

    def test_release_docs_cover_cross_platform_batch_migration_contract(self):
        documents = {
            name: read_script(name)
            for name in ("README.md", "AGENTS.md", "MIGRATION.md")
        }
        required_facts = (
            "max(1, (os.cpu_count() or 1) // 4)",
            ".asr.json",
            ".asr.lock",
            "fingerprint",
            "worker_released",
            "batch-worker-failure-",
            "Ctrl+C",
            "130",
        )
        for document, content in documents.items():
            for fact in required_facts:
                with self.subTest(document=document, fact=fact):
                    self.assertIn(fact, content)

        migration = documents["MIGRATION.md"]
        self.assertIn("不再并发启动整条", migration)
        self.assertIn("-MaxJobs", migration)
        self.assertIn("--jobs", migration)
        self.assertIn("prepare-video.ps1", migration)
        self.assertIn("prepare-video.sh", migration)
        self.assertIn("production orchestrator", migration)
        self.assertIn("SHA-256", migration)
        self.assertIn("只 fake", migration)
        self.assertIn("wsl -u root", migration)
        self.assertIn("现有 Linux interpreter", migration)
        self.assertIn("Python `>=3.10,<3.14`", migration)
        self.assertIn("不下载或安装", migration)
        self.assertIn("所有 acquisition 完成后才加载 ASR", migration)
        self.assertIn("worker shutdown 先于所有 burn", migration)
        self.assertIn("ASR 与 alignment command 串行", migration)
        self.assertIn("prepare 与 burn 各自峰值不超过 4", migration)
        self.assertIn("BATCH_SMOKE_REQUIRE_WSL", migration)
        self.assertIn("失败而不是 skip", migration)
        self.assertIn(
            r".\.venv\Scripts\python.exe -m unittest -v tests.test_batch_smoke",
            migration,
        )
        self.assertIn(
            r".\.venv\Scripts\python.exe -m unittest discover -s tests",
            migration,
        )
        self.assertIn("不证明真实 CUDA", migration)

    def test_release_docs_cover_recovery_cleanup_and_machine_report_contract(self):
        documents = {
            name: read_script(name)
            for name in ("README.md", "AGENTS.md", "MIGRATION.md")
        }
        required_facts = (
            "跳过 prepare",
            ".beautified.json",
            "alignment 成功后删除 WAV",
            "worker_failure_root_cause",
            "cleanup_diagnostics",
            "output_directory",
            "prepare-video.sh 的原始退出码",
        )
        for document, content in documents.items():
            for fact in required_facts:
                with self.subTest(document=document, fact=fact):
                    self.assertIn(fact, content)


if __name__ == "__main__":
    unittest.main()
