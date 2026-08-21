import ast
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests.fixtures import production_batch_smoke


ROOT = pathlib.Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")
BASH = shutil.which("bash")
WSL = shutil.which("wsl.exe") or shutil.which("wsl")


def read_script(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


class ProjectLauncherTests(unittest.TestCase):
    def test_pipelines_call_local_launchers_instead_of_python_directly(self):
        powershell = read_script("pipeline.ps1")
        shell = read_script("pipeline.sh")

        self.assertIn('$PyLauncherPs1 = Join-Path $ScriptDir "py_launcher.ps1"', powershell)
        self.assertIn('& $PyLauncherPs1 translate_srt', powershell)
        self.assertNotIn("translate_srt.ps1", powershell)
        self.assertNotIn("$PythonExe $TranslatePy", powershell)
        self.assertNotIn("$PythonExe =", powershell)
        self.assertIn('PY_LAUNCHER="$SCRIPT_DIR/py_launcher.sh"', shell)
        self.assertIn('bash "$PY_LAUNCHER" translate_srt', shell)
        self.assertNotIn("translate_srt.sh", shell)
        self.assertNotIn('"$PYTHON_BIN" "$TRANSLATE_SCRIPT"', shell)

    def test_operational_script_names_match_across_platforms(self):
        powershell_names = {
            path.stem
            for path in ROOT.glob("*.ps1")
            if path.name != ".env.ps1"
        }
        bash_names = {path.stem for path in ROOT.glob("*.sh")}

        self.assertEqual(powershell_names, bash_names)
        self.assertEqual(
            powershell_names,
            {
                "download",
                "ffmpeg-burn",
                "mpv-burn",
                "pipeline",
                "prepare-video",
                "py_launcher",
                "setup",
                "whisper",
            },
        )

    def test_per_target_wrappers_are_removed(self):
        for script_name in (
            "translate_srt.ps1",
            "translate_srt.sh",
            "merge_ass.ps1",
            "merge_ass.sh",
            "batch.ps1",
            "batch.sh",
        ):
            with self.subTest(script_name=script_name):
                self.assertFalse((ROOT / script_name).exists())

    def test_shared_python_launcher_target_whitelists_match(self):
        powershell = read_script("py_launcher.ps1")
        shell = read_script("py_launcher.sh")
        powershell_targets = set(
            re.findall(r"^\s*'(translate_srt|merge_ass|batch)'\s*\{", powershell, re.MULTILINE)
        )
        shell_targets = set(
            re.findall(r"^\s*(translate_srt|merge_ass|batch)\)\s+script_name=", shell, re.MULTILINE)
        )

        self.assertEqual(powershell_targets, {"translate_srt", "merge_ass", "batch"})
        self.assertEqual(shell_targets, powershell_targets)

    def batch_entrypoints(self):
        entries = [
            (
                "batch.py",
                [sys.executable, str(ROOT / "batch.py")],
                os.environ.copy(),
            )
        ]
        if PWSH:
            powershell_env = os.environ.copy()
            powershell_env["PYTHON_PATH_WIN"] = sys.executable
            entries.append(
                (
                    "py_launcher.ps1 batch",
                    [
                        PWSH,
                        "-NoProfile",
                        "-NonInteractive",
                        "-File",
                        str(ROOT / "py_launcher.ps1"),
                        "batch",
                    ],
                    powershell_env,
                )
            )
        return entries

    def bash_batch_entrypoint(self):
        env = os.environ.copy()
        if os.name == "nt" and WSL:
            repo_path = production_batch_smoke._wsl_path(WSL, ROOT)
            script_path = production_batch_smoke._wsl_path(WSL, ROOT / "py_launcher.sh")
            bash_python, _probe = production_batch_smoke._resolve_wsl_python(
                wsl=WSL,
                repo_path=repo_path,
            )
            command = [
                WSL,
                "-u",
                "root",
                "--",
                "env",
                f"PYTHON_PATH_LINUX={bash_python}",
                "bash",
                script_path,
                "batch",
            ]
        else:
            env["PYTHON_PATH_LINUX"] = sys.executable
            command = [BASH, str(ROOT / "py_launcher.sh"), "batch"]
        return command, env

    def test_batch_entrypoints_reject_retired_job_options(self):
        retired_arguments = (
            ("-j", "2"),
            ("--jobs", "2"),
            ("--io-jobs", "2"),
            ("-MaxJobs", "2"),
        )
        for entrypoint, command, env in self.batch_entrypoints():
            for arguments in retired_arguments:
                with self.subTest(entrypoint=entrypoint, arguments=arguments):
                    result = subprocess.run(
                        [*command, *arguments, "https://example.invalid/video"],
                        cwd=ROOT,
                        env=env,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=20,
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(BASH, "requires bash")
    def test_bash_batch_launcher_rejects_retired_job_options(self):
        command, env = self.bash_batch_entrypoint()
        for arguments in (
            ("-j", "2"),
            ("--jobs", "2"),
            ("--io-jobs", "2"),
            ("-MaxJobs", "2"),
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [*command, *arguments, "https://example.invalid/video"],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_batch_help_omits_retired_job_options(self):
        retired_option = re.compile(
            r"(?:^|[\s[,])(?:-j|--jobs|--io-jobs|-MaxJobs)(?=$|[\s,\]])",
            re.IGNORECASE | re.MULTILINE,
        )
        for entrypoint, command, env in self.batch_entrypoints():
            with self.subTest(entrypoint=entrypoint):
                result = subprocess.run(
                    [*command, "--help"],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                )
                output = result.stdout + result.stderr
                self.assertEqual(result.returncode, 0, output)
                self.assertIsNone(retired_option.search(output), output)

    @unittest.skipUnless(BASH, "requires bash")
    def test_bash_batch_help_omits_retired_job_options(self):
        command, env = self.bash_batch_entrypoint()
        result = subprocess.run(
            [*command, "--help"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        retired_option = re.compile(
            r"(?:^|[\s[,])(?:-j|--jobs|--io-jobs|-MaxJobs)(?=$|[\s,\]])",
            re.IGNORECASE | re.MULTILINE,
        )
        self.assertIsNone(retired_option.search(output), output)

    def test_python_batch_ast_has_no_thread_pool_or_pipeline_executor(self):
        findings = []
        for script_name in ("batch.py", "batch_runtime.py", "batch_scheduler.py"):
            tree = ast.parse(read_script(script_name), filename=script_name)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "concurrent.futures":
                    for alias in node.names:
                        if alias.name == "ThreadPoolExecutor":
                            findings.append((script_name, node.lineno, "import"))
                if isinstance(node, (ast.Name, ast.Attribute)):
                    identifier = node.id if isinstance(node, ast.Name) else node.attr
                    if identifier == "ThreadPoolExecutor":
                        findings.append((script_name, node.lineno, "reference"))
                if isinstance(node, ast.Subscript):
                    value = node.value
                    key = node.slice
                    if (
                        isinstance(value, ast.Attribute)
                        and isinstance(value.value, ast.Name)
                        and value.value.id == "os"
                        and value.attr == "environ"
                        and isinstance(key, ast.Constant)
                        and key.value == "PIPELINE_BATCH_CHILD"
                    ):
                        findings.append((script_name, node.lineno, "child-environment"))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    value = node.func.value
                    if (
                        node.func.attr == "get"
                        and isinstance(value, ast.Attribute)
                        and isinstance(value.value, ast.Name)
                        and value.value.id == "os"
                        and value.attr == "environ"
                        and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and node.args[0].value == "PIPELINE_BATCH_CHILD"
                    ):
                        findings.append((script_name, node.lineno, "child-environment"))
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in {"map", "submit"} or not node.args:
                    continue
                callback = node.args[0]
                if isinstance(callback, ast.Name):
                    callback_name = callback.id
                elif isinstance(callback, ast.Attribute):
                    callback_name = callback.attr
                else:
                    callback_name = ""
                if "pipeline" in callback_name.lower():
                    findings.append((script_name, node.lineno, "pipeline-executor"))
        self.assertEqual(findings, [])

    @unittest.skipUnless(PWSH, "requires PowerShell 7")
    def test_powershell_batch_ast_has_no_parallel_command_constructs(self):
        parser_script = r'''
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:BATCH_AST_TARGET,
    [ref]$tokens,
    [ref]$errors
)
if ($errors.Count -gt 0) {
    $errors | ForEach-Object { [Console]::Error.WriteLine($_.Message) }
    exit 2
}
$findings = [System.Collections.Generic.List[string]]::new()
foreach ($node in $ast.FindAll({ param($candidate) $candidate -is [System.Management.Automation.Language.CommandAst] }, $true)) {
    $commandName = $node.GetCommandName()
    if ($commandName -eq 'Start-Job') {
        $findings.Add('Start-Job')
    }
    if ($commandName -eq 'ForEach-Object') {
        foreach ($element in $node.CommandElements) {
            if ($element -is [System.Management.Automation.Language.CommandParameterAst] -and $element.ParameterName -eq 'Parallel') {
                $findings.Add('ForEach-Object -Parallel')
            }
        }
    }
}
foreach ($node in $ast.FindAll({ param($candidate) $candidate -is [System.Management.Automation.Language.InvokeMemberExpressionAst] }, $true)) {
    if ([string]$node.Member.Value -eq 'CreateRunspacePool') {
        $findings.Add('CreateRunspacePool')
    }
}
foreach ($node in $ast.FindAll({ param($candidate) $candidate -is [System.Management.Automation.Language.TypeExpressionAst] }, $true)) {
    if ($node.TypeName.FullName -match '(^|\.)RunspacePool$') {
        $findings.Add('RunspacePool type')
    }
}
foreach ($node in $ast.FindAll({ param($candidate) $candidate -is [System.Management.Automation.Language.VariableExpressionAst] }, $true)) {
    if ($node.VariablePath.UserPath -eq 'env:PIPELINE_BATCH_CHILD') {
        $findings.Add('PIPELINE_BATCH_CHILD environment access')
    }
}
[Console]::Out.WriteLine((ConvertTo-Json -Compress -InputObject @($findings)))
'''
        for script_name in ("py_launcher.ps1", "pipeline.ps1"):
            with self.subTest(script_name=script_name):
                env = os.environ.copy()
                env["BATCH_AST_TARGET"] = str(ROOT / script_name)
                result = subprocess.run(
                    [PWSH, "-NoProfile", "-NonInteractive", "-Command", parser_script],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout), [])

    @unittest.skipUnless(BASH, "requires bash")
    def test_bash_launcher_is_a_single_exec_wrapper_without_background_pool(self):
        if os.name == "nt" and WSL:
            command = [
                WSL,
                "-u",
                "root",
                "--",
                "bash",
                "-n",
                production_batch_smoke._wsl_path(WSL, ROOT / "py_launcher.sh"),
            ]
        else:
            command = [BASH, "-n", str(ROOT / "py_launcher.sh")]
        result = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        commands = [
            line.strip()
            for line in read_script("py_launcher.sh").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(commands[-1], 'exec "$PYTHON_BIN" "$SCRIPT_DIR/$script_name" "$@"')
        self.assertFalse(any(re.search(r"(^|[;&|])\s*(wait|parallel|xargs)\b", line) for line in commands))

    def test_batch_stage_contract_and_standalone_entrypoints_are_distinct(self):
        batch = read_script("batch_runtime.py")
        scheduler = read_script("batch_scheduler.py")
        powershell_pipeline = read_script("pipeline.ps1")
        bash_pipeline = read_script("pipeline.sh")

        self.assertIn(
            "download -> prepare -> extract_audio -> asr -> align -> translate",
            batch,
        )
        for stage in (
            'task.start("download")',
            'task.advance("prepare")',
            'task.advance("extract_audio")',
            'task.advance("asr")',
            'task.advance("alignment")',
            'task.advance("postprocess")',
            'task.advance("burn")',
        ):
            with self.subTest(stage=stage):
                self.assertIn(stage, scheduler)

        self.assertIn('& $WhisperPs1 $VideoPath', powershell_pipeline)
        self.assertIn('bash "$WHISPER_SCRIPT" "$VIDEO_PATH"', bash_pipeline)

    def test_batch_help_and_dry_run_exit_zero_without_bell_output(self):
        cases = (("--help",), ("--dry-run", "https://example.invalid/video"))
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [sys.executable, str(ROOT / "batch.py"), *arguments],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertNotIn("\a", result.stdout + result.stderr)


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
        scripts = (self.shared_launcher,)
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
        for script_name in ("translate_srt.py", "merge_ass.py", "batch.py"):
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
            ("batch", "batch.py"),
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
        for target in ("unsupported", "BATCH"):
            with self.subTest(target=target):
                result = self.run_script(self.shared_launcher, target)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("unsupported Python target", result.stderr)

@unittest.skipUnless(PWSH, "requires PowerShell 7")
class PowerShellLauncherBehaviorTests(LauncherBehaviorMixin, unittest.TestCase):
    platform = "powershell"
    shared_launcher = "py_launcher.ps1"

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

@unittest.skipUnless(BASH, "requires bash")
class BashLauncherBehaviorTests(LauncherBehaviorMixin, unittest.TestCase):
    platform = "bash"
    shared_launcher = "py_launcher.sh"

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
