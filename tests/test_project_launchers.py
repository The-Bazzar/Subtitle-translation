import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")
BASH = shutil.which("bash")


def read_script(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class ProjectLauncherTests(unittest.TestCase):
    def test_pipelines_call_local_launchers_instead_of_python_directly(self):
        powershell = read_script("pipeline.ps1")
        shell = read_script("pipeline.sh")

        self.assertIn('$TranslatePs1 = Join-Path $ScriptDir "translate_srt.ps1"', powershell)
        self.assertNotIn("$PythonExe $TranslatePy", powershell)
        self.assertNotIn("$PythonExe =", powershell)
        self.assertIn('TRANSLATE_SCRIPT="$SCRIPT_DIR/translate_srt.sh"', shell)
        self.assertNotIn('"$PYTHON_BIN" "$TRANSLATE_SCRIPT"', shell)


class LauncherBehaviorMixin:
    platform = None
    shared_launcher = None
    wrappers = ()

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="launcher-test-",
            dir=ROOT,
        )
        self.sandbox = pathlib.Path(self.temp_dir.name)
        self.cwd = ROOT.parent
        scripts = (self.shared_launcher, *(wrapper for wrapper, _ in self.wrappers))
        for script_name in scripts:
            shutil.copy2(ROOT / script_name, self.sandbox / script_name)
        fake_target = """\
import json
import os
import pathlib
import sys

print(json.dumps({
    "script": pathlib.Path(sys.argv[0]).name,
    "args": sys.argv[1:],
    "cwd": os.getcwd(),
    "override": os.environ.get("LAUNCHER_OVERRIDE_SENTINEL"),
}))
raise SystemExit(int(os.environ.get("FAKE_EXIT_CODE", "0")))
"""
        for script_name in ("translate_srt.py", "merge_ass.py"):
            (self.sandbox / script_name).write_text(fake_target, encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def parse_payload(result):
        return json.loads(result.stdout.strip().splitlines()[-1])

    def run_script(self, script_name, *script_args, exit_code=0):
        raise NotImplementedError

    def expected_cwd(self):
        raise NotImplementedError

    def test_launchers_use_overrides_and_resolve_targets_from_arbitrary_cwd(self):
        for target, script_name in (
            ("translate_srt", "translate_srt.py"),
            ("merge_ass", "merge_ass.py"),
        ):
            with self.subTest(target=target):
                result = self.run_script(self.shared_launcher, target, "probe")
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = self.parse_payload(result)
                self.assertEqual(payload["script"], script_name)
                self.assertEqual(payload["args"], ["probe"])
                self.assertEqual(payload["cwd"], self.expected_cwd())
                self.assertEqual(payload["override"], self.platform)

    def test_launchers_forward_arguments_that_look_like_launcher_parameters(self):
        forwarded = ["-Target", "batch", "--help", "value with spaces"]
        result = self.run_script(
            self.shared_launcher,
            "translate_srt",
            *forwarded,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = self.parse_payload(result)
        self.assertEqual(payload["script"], "translate_srt.py")
        self.assertEqual(payload["args"], forwarded)

    def test_launchers_propagate_exact_python_exit_code(self):
        result = self.run_script(
            self.shared_launcher,
            "merge_ass",
            exit_code=37,
        )
        self.assertEqual(result.returncode, 37, result.stderr)

    def test_launchers_reject_unsupported_and_wrong_case_targets(self):
        for target in ("unsupported", "batch", "BATCH"):
            with self.subTest(target=target):
                result = self.run_script(self.shared_launcher, target)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("unsupported Python target", result.stderr)

    def test_wrappers_delegate_end_to_end(self):
        forwarded = ["-Target", "batch", "--help", "value with spaces"]
        for wrapper, script_name in self.wrappers:
            with self.subTest(wrapper=wrapper):
                result = self.run_script(wrapper, *forwarded, exit_code=41)
                self.assertEqual(result.returncode, 41, result.stderr)
                payload = self.parse_payload(result)
                self.assertEqual(payload["script"], script_name)
                self.assertEqual(payload["args"], forwarded)
                self.assertEqual(payload["cwd"], self.expected_cwd())
                self.assertEqual(payload["override"], self.platform)


@unittest.skipUnless(PWSH, "requires PowerShell 7")
class PowerShellLauncherBehaviorTests(LauncherBehaviorMixin, unittest.TestCase):
    platform = "powershell"
    shared_launcher = "py_launcher.ps1"
    wrappers = (
        ("translate_srt.ps1", "translate_srt.py"),
        ("merge_ass.ps1", "merge_ass.py"),
    )

    def run_script(self, script_name, *script_args, exit_code=0):
        env = os.environ.copy()
        env["PYTHON_PATH_WIN"] = sys.executable
        env["LAUNCHER_OVERRIDE_SENTINEL"] = self.platform
        env["FAKE_EXIT_CODE"] = str(exit_code)
        return subprocess.run(
            [
                PWSH,
                "-NoProfile",
                "-File",
                str(self.sandbox / script_name),
                *script_args,
            ],
            cwd=self.cwd,
            env=env,
            capture_output=True,
            text=True,
        )

    def expected_cwd(self):
        return str(self.cwd.resolve())

    def test_wrappers_preserve_common_and_stop_parsing_arguments(self):
        forwarded = ["-Verbose", "--", "-leading.json"]
        for wrapper, script_name in self.wrappers:
            with self.subTest(wrapper=wrapper):
                result = self.run_script(wrapper, *forwarded)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = self.parse_payload(result)
                self.assertEqual(payload["script"], script_name)
                self.assertEqual(payload["args"], forwarded)


@unittest.skipUnless(BASH, "requires bash")
class BashLauncherBehaviorTests(LauncherBehaviorMixin, unittest.TestCase):
    platform = "bash"
    shared_launcher = "py_launcher.sh"
    wrappers = (
        ("translate_srt.sh", "translate_srt.py"),
        ("merge_ass.sh", "merge_ass.py"),
    )

    @classmethod
    def setUpClass(cls):
        cls.bash_python = subprocess.run(
            [BASH, "-lc", "command -v python3 || command -v python"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    @staticmethod
    def bash_path(path):
        resolved = pathlib.Path(path).resolve()
        if os.name != "nt":
            return str(resolved)
        if "system32" in BASH.lower():
            drive, tail = os.path.splitdrive(str(resolved))
            return f"/mnt/{drive[0].lower()}{tail.replace(os.sep, '/')}"
        return subprocess.run(
            [BASH, "-lc", 'cygpath -u "$1"', "cygpath", str(resolved)],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def run_script(self, script_name, *script_args, exit_code=0):
        env = os.environ.copy()
        env["PYTHON_PATH_LINUX"] = self.bash_python
        env["LAUNCHER_OVERRIDE_SENTINEL"] = self.platform
        env["FAKE_EXIT_CODE"] = str(exit_code)
        if os.name == "nt" and "system32" in BASH.lower():
            inherited = env.get("WSLENV", "")
            variables = "PYTHON_PATH_LINUX:LAUNCHER_OVERRIDE_SENTINEL:FAKE_EXIT_CODE"
            env["WSLENV"] = f"{inherited}:{variables}" if inherited else variables
        return subprocess.run(
            [
                BASH,
                self.bash_path(self.sandbox / script_name),
                *script_args,
            ],
            cwd=self.cwd,
            env=env,
            capture_output=True,
            text=True,
        )

    def expected_cwd(self):
        return self.bash_path(self.cwd)


if __name__ == "__main__":
    unittest.main()
