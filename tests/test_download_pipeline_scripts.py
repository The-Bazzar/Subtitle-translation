import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")
BASH = shutil.which("bash")


def read_script(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def bash_path(path: pathlib.Path) -> str:
    resolved = path.resolve()
    if os.name != "nt":
        return str(resolved)
    if BASH and "system32" in BASH.lower():
        drive, tail = os.path.splitdrive(str(resolved))
        return f"/mnt/{drive[0].lower()}{tail.replace(os.sep, '/')}"
    return subprocess.run(
        [BASH, "-lc", 'cygpath -u "$1"', "cygpath", str(resolved)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def bash_lexical_path(path: pathlib.Path) -> str:
    absolute = path.absolute()
    if os.name != "nt":
        return str(absolute)
    if BASH and "system32" in BASH.lower():
        drive, tail = os.path.splitdrive(str(absolute))
        return f"/mnt/{drive[0].lower()}{tail.replace(os.sep, '/')}"
    return subprocess.run(
        [BASH, "-lc", 'cygpath -u "$1"', "cygpath", str(absolute)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class ScriptBehaviorMixin:
    script_suffix = None

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="download-script-test-",
            dir=ROOT,
        )
        self.sandbox = pathlib.Path(self.temp_dir.name)
        self.work_dir = self.sandbox / "work"
        self.work_dir.mkdir()
        for script_name in (
            f"prepare-video.{self.script_suffix}",
            f"download.{self.script_suffix}",
            f"pipeline.{self.script_suffix}",
        ):
            shutil.copy2(ROOT / script_name, self.sandbox / script_name)
        self.original_video = self.work_dir / "sample.original.mp4"
        self.original_video.write_bytes(b"original")

    def tearDown(self):
        sandbox = self.sandbox
        self.temp_dir.cleanup()
        self.assertFalse(sandbox.exists(), f"temporary sandbox leaked: {sandbox}")

    def run_prepare(
        self,
        *args,
        ffmpeg_exit=0,
        create_output=True,
        nvenc_available=False,
        output_kind=None,
    ):
        raise NotImplementedError

    def run_download(self):
        raise NotImplementedError

    def read_ffmpeg_args(self):
        raise NotImplementedError

    def run_pipeline_prepare_failure(self):
        raise NotImplementedError

    def script_path(self, path):
        raise NotImplementedError

    def script_parent(self, path):
        raise NotImplementedError

    def script_literal_path(self, path):
        raise NotImplementedError

    def test_prepare_requires_exactly_one_original_video_argument(self):
        missing = self.run_prepare()
        self.assertNotEqual(missing.returncode, 0, missing.stdout + missing.stderr)

        extra = self.run_prepare(self.original_video, "unexpected")
        self.assertNotEqual(extra.returncode, 0, extra.stdout + extra.stderr)

    def test_prepare_propagates_fake_ffmpeg_failure(self):
        result = self.run_prepare(self.original_video, ffmpeg_exit=23, create_output=False)
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
        self.assertFalse((self.work_dir / "sample.mkv").exists())

    def test_prepare_success_emits_one_marker_and_removes_metadata(self):
        result = self.run_prepare(self.original_video)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        markers = [line for line in result.stdout.splitlines() if line.startswith("OUTPUT_VIDEO=")]
        self.assertEqual(len(markers), 1, result.stdout)
        self.assertTrue((self.work_dir / "sample.mkv").is_file())

        ffmpeg_args = self.read_ffmpeg_args()
        metadata_index = ffmpeg_args.index("-map_metadata")
        self.assertEqual(ffmpeg_args[metadata_index + 1], "-1")

    def test_prepare_accepts_nonempty_current_nvenc_temporary_output(self):
        final_output = self.work_dir / "sample.mkv"
        final_output.write_bytes(b"old-edit")
        result = self.run_prepare(
            self.original_video,
            ffmpeg_exit=23,
            create_output=True,
            nvenc_available=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(final_output.read_bytes(), b"edit")
        ffmpeg_args = self.read_ffmpeg_args()
        self.assertIn("h264_nvenc", ffmpeg_args)
        self.assertNotEqual(ffmpeg_args[-1], self.script_path(final_output))
        self.assertEqual(
            self.script_parent(ffmpeg_args[-1]),
            self.script_parent(self.script_path(final_output)),
        )
        self.assertEqual(list(self.work_dir.glob(".*.prepare.*.mkv")), [])

    def test_prepare_does_not_count_existing_output_as_nvenc_success(self):
        final_output = self.work_dir / "sample.mkv"
        final_output.write_bytes(b"old-edit")
        result = self.run_prepare(
            self.original_video,
            ffmpeg_exit=23,
            create_output=False,
            nvenc_available=True,
        )
        self.assertEqual(result.returncode, 23, result.stdout + result.stderr)
        self.assertEqual(final_output.read_bytes(), b"old-edit")
        self.assertEqual(list(self.work_dir.glob(".*.prepare.*.mkv")), [])

    def test_prepare_rejects_success_without_nonempty_regular_temporary_output(self):
        for output_kind in ("missing", "empty", "directory"):
            with self.subTest(output_kind=output_kind):
                final_output = self.work_dir / "sample.mkv"
                final_output.unlink(missing_ok=True)
                result = self.run_prepare(
                    self.original_video,
                    ffmpeg_exit=0,
                    output_kind=output_kind,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertFalse(
                    any(line.startswith("OUTPUT_VIDEO=") for line in result.stdout.splitlines())
                )
                self.assertFalse(final_output.exists())
                self.assertEqual(list(self.work_dir.glob(".*.prepare.*.mkv")), [])

    def test_prepare_refuses_mkv_original_without_overwriting_it(self):
        mkv_original = self.work_dir / "standalone.mkv"
        mkv_original.write_bytes(b"mkv-original")
        result = self.run_prepare(mkv_original)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(mkv_original.read_bytes(), b"mkv-original")
        self.assertFalse(any(line.startswith("OUTPUT_VIDEO=") for line in result.stdout.splitlines()))

    def test_prepare_refuses_symlink_input_targeting_computed_output(self):
        final_output = self.work_dir / "sample.mkv"
        final_output.write_bytes(b"protected-edit")
        self.original_video.unlink()
        try:
            self.original_video.symlink_to(final_output.name)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

        result = self.run_prepare("sample.original.mp4")

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any(line.startswith("OUTPUT_VIDEO=") for line in result.stdout.splitlines()))
        self.assertIn("original", (result.stdout + result.stderr).lower())
        self.assertEqual(final_output.read_bytes(), b"protected-edit")
        self.assertEqual(list(self.work_dir.glob(".*.prepare.*.mkv")), [])

    def test_prepare_preserves_symlink_directory_for_edit_output(self):
        physical_dir = self.work_dir / "physical"
        linked_dir = self.work_dir / "linked"
        physical_dir.mkdir()
        linked_dir.mkdir()
        physical_input = physical_dir / "episode.original.mp4"
        physical_input.write_bytes(b"physical-original")
        linked_input = linked_dir / "episode.original.mp4"
        try:
            linked_input.symlink_to(physical_input)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

        result = self.run_prepare(self.script_literal_path(linked_input))

        linked_output = linked_dir / "episode.mkv"
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(linked_output.is_file())
        self.assertFalse((physical_dir / "episode.mkv").exists())
        self.assertEqual(physical_input.read_bytes(), b"physical-original")
        self.assertIn(
            f"OUTPUT_VIDEO={self.script_literal_path(linked_output)}",
            result.stdout.splitlines(),
        )

    def test_prepare_refuses_computed_output_directory_before_encoding(self):
        final_output = self.work_dir / "sample.mkv"
        final_output.mkdir()
        original_content = self.original_video.read_bytes()

        result = self.run_prepare(self.original_video)

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any(line.startswith("OUTPUT_VIDEO=") for line in result.stdout.splitlines()))
        self.assertTrue(final_output.is_dir())
        self.assertEqual(list(final_output.iterdir()), [])
        self.assertEqual(self.original_video.read_bytes(), original_content)
        self.assertFalse(self.ffmpeg_log.exists())
        self.assertEqual(list(self.work_dir.glob(".*.prepare.*.mkv")), [])

    def test_download_stops_at_original_without_calling_prepare(self):
        result, prepare_sentinel = self.run_download()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        markers = [line for line in result.stdout.splitlines() if line.startswith("OUTPUT_RENDER_VIDEO=")]
        self.assertEqual(len(markers), 1, result.stdout)
        output_dir = self.work_dir / "Behavior Test"
        self.assertTrue((output_dir / "Behavior Test.original.mp4").is_file())
        self.assertFalse((output_dir / "Behavior Test.mkv").exists())
        self.assertFalse(prepare_sentinel.exists())

    def test_pipeline_stops_when_prepare_fails(self):
        result, downstream_sentinel = self.run_pipeline_prepare_failure()
        self.assertEqual(result.returncode, 37, result.stdout + result.stderr)
        self.assertFalse(downstream_sentinel.exists())


@unittest.skipUnless(PWSH, "requires PowerShell 7")
class PowerShellScriptBehaviorTests(ScriptBehaviorMixin, unittest.TestCase):
    script_suffix = "ps1"

    def setUp(self):
        super().setUp()
        shutil.copy2(ROOT / ".env.ps1", self.sandbox / ".env.ps1")
        self.ffmpeg_log = self.sandbox / "ffmpeg-args.json"
        self.fake_ytdlp = self.sandbox / "fake-ytdlp.ps1"
        self.fake_ytdlp.write_text(
            """\
$Arguments = @($args)
if ($Arguments[0] -eq '--get-title') { Write-Output 'Behavior Test'; exit 0 }
$outputIndex = [Array]::IndexOf($Arguments, '-o')
$outputPath = $Arguments[$outputIndex + 1].Replace('%(ext)s', 'mp4')
$parent = Split-Path $outputPath -Parent
New-Item -ItemType Directory -Force -Path $parent | Out-Null
[System.IO.File]::WriteAllText($outputPath, 'original')
exit 0
""",
            encoding="utf-8",
        )

    def write_env(self, **values):
        (self.sandbox / ".env").write_text(
            "".join(f"{key}={value}\n" for key, value in values.items()),
            encoding="utf-8",
        )

    def run_prepare(
        self,
        *args,
        ffmpeg_exit=0,
        create_output=True,
        nvenc_available=False,
        output_kind=None,
    ):
        fake_module = """\
function Get-EnvValue([string]$Key, [string]$Default) {
    if ($Key -eq 'FFMPEG_PATH_WIN') { return 'Invoke-FakeFfmpeg' }
    return $Default
}
function nvidia-smi {
    Set-Variable LASTEXITCODE __NVIDIA_EXIT__ -Scope 1
}
function Invoke-FakeFfmpeg {
    $Arguments = @()
    foreach ($item in $args) {
        if ($item -is [Array]) { $Arguments += $item } else { $Arguments += @($item) }
    }
    if ($Arguments -contains '-encoders') {
        __ENCODER_OUTPUT__
        Set-Variable LASTEXITCODE 0 -Scope 1
        return
    }
    $Arguments | ConvertTo-Json -Compress | Set-Content -LiteralPath $env:FAKE_FFMPEG_LOG -Encoding UTF8
    switch ($env:FAKE_FFMPEG_OUTPUT_KIND) {
        'file' { [System.IO.File]::WriteAllText($Arguments[-1], 'edit') }
        'empty' { [System.IO.File]::WriteAllBytes($Arguments[-1], [byte[]]@()) }
        'directory' { New-Item -ItemType Directory -Path $Arguments[-1] | Out-Null }
    }
    Set-Variable LASTEXITCODE ([int]$env:FAKE_FFMPEG_EXIT) -Scope 1
}
"""
        fake_module = fake_module.replace(
            "__NVIDIA_EXIT__",
            "0" if nvenc_available else "1",
        ).replace(
            "__ENCODER_OUTPUT__",
            "Write-Output ' V..... h264_nvenc'" if nvenc_available else "",
        )
        (self.sandbox / ".env.ps1").write_text(
            fake_module,
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["FAKE_FFMPEG_LOG"] = str(self.ffmpeg_log)
        env["FAKE_FFMPEG_EXIT"] = str(ffmpeg_exit)
        env["FAKE_FFMPEG_OUTPUT_KIND"] = output_kind or (
            "file" if create_output else "missing"
        )
        return subprocess.run(
            [
                PWSH,
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(self.sandbox / "prepare-video.ps1"),
                *(str(arg) for arg in args),
            ],
            cwd=self.work_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )

    def run_download(self):
        prepare_sentinel = self.sandbox / "prepare-called"
        (self.sandbox / "prepare-video.ps1").write_text(
            f"Set-Content -LiteralPath '{prepare_sentinel}' -Value called\nexit 99\n",
            encoding="utf-8",
        )
        shutil.copy2(ROOT / ".env.ps1", self.sandbox / ".env.ps1")
        self.write_env(
            YTDLP_PATH_WIN=self.fake_ytdlp,
            FFMPEG_PATH_WIN=self.sandbox / "prepare-video.ps1",
        )
        result = subprocess.run(
            [
                PWSH,
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(self.sandbox / "download.ps1"),
                "https://example.invalid/video",
            ],
            cwd=self.work_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        return result, prepare_sentinel

    def run_pipeline_prepare_failure(self):
        downstream_sentinel = self.sandbox / "whisper-called"
        original = self.work_dir / "pipeline.original.mp4"
        original.write_bytes(b"original")
        (self.sandbox / "download.ps1").write_text(
            f'Write-Output "OUTPUT_RENDER_VIDEO={original}"\nexit 0\n',
            encoding="utf-8",
        )
        (self.sandbox / "prepare-video.ps1").write_text(
            'Write-Error "forced prepare failure"\nexit 37\n',
            encoding="utf-8",
        )
        (self.sandbox / "whisper.ps1").write_text(
            f"Set-Content -LiteralPath '{downstream_sentinel}' -Value called\nexit 0\n",
            encoding="utf-8",
        )
        shutil.copy2(ROOT / ".env.ps1", self.sandbox / ".env.ps1")
        env = os.environ.copy()
        env["PIPELINE_BATCH_CHILD"] = "1"
        result = subprocess.run(
            [
                PWSH,
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(self.sandbox / "pipeline.ps1"),
                "https://example.invalid/video",
                "-SkipBurn",
            ],
            cwd=self.work_dir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        return result, downstream_sentinel

    def test_prepare_reports_command_not_found_with_nonzero_exit(self):
        (self.sandbox / ".env.ps1").write_text(
            "function Get-EnvValue([string]$Key, [string]$Default) { return 'missing-ffmpeg-command' }\n",
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                PWSH,
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(self.sandbox / "prepare-video.ps1"),
                str(self.original_video),
            ],
            cwd=self.work_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ffmpeg command not found", (result.stdout + result.stderr).lower())

    def test_prepare_fails_when_final_output_is_locked(self):
        final_output = self.work_dir / "sample.mkv"
        final_output.write_bytes(b"locked-old-edit")
        ready = self.sandbox / "lock-ready"
        lock_script = (
            f"$stream = [IO.File]::Open('{final_output}', [IO.FileMode]::Open, "
            "[IO.FileAccess]::ReadWrite, [IO.FileShare]::None); "
            f"[IO.File]::WriteAllText('{ready}', 'ready'); "
            "try { Start-Sleep -Seconds 20 } finally { $stream.Dispose() }"
        )
        holder = subprocess.Popen(
            [PWSH, "-NoProfile", "-NonInteractive", "-Command", lock_script],
            cwd=self.work_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready.exists(), "failed to acquire output lock")
            result = self.run_prepare(self.original_video)
        finally:
            holder.terminate()
            holder.wait(timeout=5)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(final_output.read_bytes(), b"locked-old-edit")
        self.assertIn("replace prepared edit video", (result.stdout + result.stderr).lower())
        self.assertEqual(list(self.work_dir.glob(".*.prepare.*.mkv")), [])

    def read_ffmpeg_args(self):
        return json.loads(self.ffmpeg_log.read_text(encoding="utf-8-sig"))

    def script_path(self, path):
        return str(path.resolve())

    def script_parent(self, path):
        return pathlib.PureWindowsPath(path).parent

    def script_literal_path(self, path):
        return str(path.absolute())


@unittest.skipUnless(BASH, "requires bash")
class BashScriptBehaviorTests(ScriptBehaviorMixin, unittest.TestCase):
    script_suffix = "sh"

    @classmethod
    def setUpClass(cls):
        cls.bash_python = subprocess.run(
            [BASH, "-lc", "command -v python3 || command -v python"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def setUp(self):
        super().setUp()
        self.ffmpeg_log = self.sandbox / "ffmpeg-args.txt"
        self.fake_ffmpeg = self.sandbox / "fake-ffmpeg.sh"
        self.fake_ytdlp = self.sandbox / "fake-ytdlp.sh"
        self.fake_ytdlp.write_text(
            """\
#!/bin/bash
if [ "$1" = "--get-title" ]; then
    echo "Behavior Test"
    exit 0
fi
while [ "$#" -gt 0 ]; do
    if [ "$1" = "-o" ]; then
        output_pattern="$2"
        break
    fi
    shift
done
output_path="${output_pattern//%(ext)s/mp4}"
mkdir -p "$(dirname "$output_path")"
printf original > "$output_path"
            """,
            encoding="utf-8",
            newline="\n",
        )
        self.fake_ytdlp.chmod(0o755)

    def write_fake_ffmpeg(
        self,
        exit_code,
        create_output,
        nvenc_available=False,
        output_kind=None,
    ):
        selected_output = output_kind or ("file" if create_output else "missing")
        output_blocks = {
            "file": 'printf edit > "${@: -1}"',
            "empty": ': > "${@: -1}"',
            "directory": 'mkdir -p "${@: -1}"',
            "missing": ":",
        }
        output_block = output_blocks[selected_output]
        encoder_output = 'echo " V..... h264_nvenc"' if nvenc_available else ":"
        self.fake_ffmpeg.write_text(
            f"""\
#!/bin/bash
if [[ " $* " == *" -encoders "* ]]; then {encoder_output}; exit 0; fi
printf '%s\\n' "$@" > {shlex.quote(bash_path(self.ffmpeg_log))}
{output_block}
exit {exit_code}
            """,
            encoding="utf-8",
            newline="\n",
        )
        self.fake_ffmpeg.chmod(0o755)
        fake_nvidia = self.sandbox / "nvidia-smi"
        fake_nvidia.write_text(
            f"#!/bin/bash\nexit {0 if nvenc_available else 1}\n",
            encoding="utf-8",
            newline="\n",
        )
        fake_nvidia.chmod(0o755)

    def write_env(self):
        (self.sandbox / ".env").write_text(
            "\n".join(
                (
                    f"FFMPEG_PATH_LINUX='{bash_path(self.fake_ffmpeg)}'",
                    f"PATH='{bash_path(self.sandbox)}':$PATH",
                    "YTDLP_PATH_LINUX=fake-ytdlp.sh",
                    f"PYTHON_PATH_LINUX='{self.bash_python}'",
                )
            )
            + "\n",
            encoding="utf-8",
        )

    def run_prepare(
        self,
        *args,
        ffmpeg_exit=0,
        create_output=True,
        nvenc_available=False,
        output_kind=None,
    ):
        self.write_fake_ffmpeg(
            ffmpeg_exit,
            create_output,
            nvenc_available,
            output_kind,
        )
        self.write_env()
        return self.run_bash_script(
            self.sandbox / "prepare-video.sh",
            *(bash_path(arg) if isinstance(arg, pathlib.Path) else str(arg) for arg in args),
        )

    def run_bash_script(self, script, *args, env=None):
        runner = self.sandbox / "run-script.sh"
        runner.write_text(
            f"#!/bin/bash\nexport PATH=/usr/bin:/bin:$PATH\ncd {shlex.quote(bash_path(self.work_dir))}\n"
            f"exec bash {shlex.quote(bash_path(script))} \"$@\"\n",
            encoding="utf-8",
            newline="\n",
        )
        runner.chmod(0o755)
        return subprocess.run(
            [
                BASH,
                bash_path(runner),
                *args,
            ],
            cwd=ROOT.parent,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )

    def run_download(self):
        self.write_fake_ffmpeg(0, True)
        prepare_sentinel = self.sandbox / "prepare-called"
        (self.sandbox / "prepare-video.sh").write_text(
            f"#!/bin/bash\nprintf called > {shlex.quote(bash_path(prepare_sentinel))}\nexit 99\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.sandbox / "prepare-video.sh").chmod(0o755)
        self.write_env()
        result = self.run_bash_script(
            self.sandbox / "download.sh",
            "https://example.invalid/video",
        )
        return result, prepare_sentinel

    def run_pipeline_prepare_failure(self):
        downstream_sentinel = self.sandbox / "whisper-called"
        original = self.work_dir / "pipeline.original.mp4"
        original.write_bytes(b"original")
        (self.sandbox / "download.sh").write_text(
            f"#!/bin/bash\necho {shlex.quote('OUTPUT_RENDER_VIDEO=' + bash_path(original))}\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.sandbox / "prepare-video.sh").write_text(
            '#!/bin/bash\necho "forced prepare failure" >&2\nexit 37\n',
            encoding="utf-8",
            newline="\n",
        )
        (self.sandbox / "whisper.sh").write_text(
            f"#!/bin/bash\nprintf called > {shlex.quote(bash_path(downstream_sentinel))}\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.sandbox / "translate_srt.sh").write_text(
            "#!/bin/bash\nexit 0\n",
            encoding="utf-8",
            newline="\n",
        )
        self.write_env()
        with (self.sandbox / ".env").open("a", encoding="utf-8", newline="\n") as env_file:
            env_file.write("TRANSLATE_PROVIDER=fake\n")
        env = os.environ.copy()
        env["PIPELINE_BATCH_CHILD"] = "1"
        if os.name == "nt" and BASH and "system32" in BASH.lower():
            inherited = env.get("WSLENV", "")
            env["WSLENV"] = f"{inherited}:PIPELINE_BATCH_CHILD" if inherited else "PIPELINE_BATCH_CHILD"
        result = self.run_bash_script(
            self.sandbox / "pipeline.sh",
            "https://example.invalid/video",
            env=env,
        )
        return result, downstream_sentinel

    def read_ffmpeg_args(self):
        return self.ffmpeg_log.read_text(encoding="utf-8").splitlines()

    def script_path(self, path):
        return bash_path(path)

    def script_parent(self, path):
        return pathlib.PurePosixPath(path).parent

    def script_literal_path(self, path):
        return bash_lexical_path(path)

    @unittest.skipUnless(os.name == "nt", "requires a case-insensitive WSL mount")
    def test_prepare_refuses_case_variant_mkv_original_without_overwriting_it(self):
        uppercase_original = self.work_dir / "standalone.MKV"
        uppercase_original.write_bytes(b"case-sensitive-original")

        result = self.run_prepare(uppercase_original)

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(uppercase_original.read_bytes(), b"case-sensitive-original")
        self.assertFalse(
            any(line.startswith("OUTPUT_VIDEO=") for line in result.stdout.splitlines())
        )


class DownloadPipelineScriptTests(unittest.TestCase):
    def read_required_script(self, name: str) -> str:
        path = ROOT / name
        self.assertTrue(path.is_file(), f"missing required script: {name}")
        return path.read_text(encoding="utf-8")

    def test_download_scripts_emit_only_render_video_path(self):
        expectations = {
            "download.ps1": r'Write-Output\s+"OUTPUT_RENDER_VIDEO=',
            "download.sh": r'echo\s+"OUTPUT_RENDER_VIDEO=',
        }

        for script, pattern in expectations.items():
            content = read_script(script)
            with self.subTest(script=script, assertion="render_marker"):
                self.assertRegex(content, pattern)
            with self.subTest(script=script, assertion="no_edit_marker"):
                self.assertNotIn("OUTPUT_VIDEO=", content)
            with self.subTest(script=script, assertion="no_nvenc"):
                self.assertNotIn("h264_nvenc", content)

    def test_prepare_video_scripts_emit_edit_path_and_reencode_with_encoder_preference(self):
        expectations = {
            "prepare-video.ps1": [
                r'Write-Output\s+"OUTPUT_VIDEO=',
                r'Test-NvidiaAvailable',
                r'Test-FfmpegEncoder',
                r'Test-NonEmptyFile',
                r'h264_nvenc',
                r"'-cq',\s*'12'",
                r'libx264',
                r"'-crf',\s*'12'",
                r'aresample=async=1:out_sample_fmt=s16',
                r"'-c:a',\s*'flac'",
                r'\.mkv',
            ],
            "prepare-video.sh": [
                r'echo\s+"OUTPUT_VIDEO=',
                r'nvidia_available',
                r'ffmpeg_encoder_available',
                r'h264_nvenc',
                r'-cq 12',
                r'libx264',
                r'-crf 12',
                r'aresample=async=1:out_sample_fmt=s16',
                r'-c:a flac',
                r'\.mkv',
            ],
        }

        for script, patterns in expectations.items():
            content = self.read_required_script(script)
            for pattern in patterns:
                with self.subTest(script=script, pattern=pattern):
                    self.assertRegex(content, pattern)
            with self.subTest(script=script, assertion="no_hwaccel_decode"):
                self.assertNotIn("hwaccel", content.lower())
            with self.subTest(script=script, assertion="no_frame_pipe"):
                self.assertNotIn("yuv4mpegpipe", content)
                self.assertNotIn("pipe:0", content)
                self.assertNotIn("Invoke-FfmpegFramePipeAttempt", content)

    def test_prepare_video_scripts_surface_ffmpeg_commands_and_diagnostics(self):
        expectations = {
            "prepare-video.ps1": [
                r'ffmpeg cmd:',
            ],
            "prepare-video.sh": [
                r'ffmpeg cmd:',
            ],
        }

        for script, patterns in expectations.items():
            content = self.read_required_script(script)
            for pattern in patterns:
                with self.subTest(script=script, pattern=pattern):
                    self.assertRegex(content, pattern)

    def test_prepare_video_scripts_accept_nonempty_nvenc_output_after_nonzero_ffmpeg_exit(self):
        expectations = {
            "prepare-video.ps1": [
                r"\$attempt\.Name\s+-eq\s+'h264_nvenc'",
                r'Test-NonEmptyFile\s+-Path\s+\$temporaryPath',
                r'返回 exit=\$lastExitCode',
                r'非 0B 文件',
            ],
            "prepare-video.sh": [
                r'\[ "\$label" = "h264_nvenc" \]',
                r'is_nonempty_regular_file "\$temporary_path"',
                r'\[ -f "\$path" \].*\[ ! -L "\$path" \].*\[ -s "\$path" \]',
                r'返回非零退出码',
                r'非 0B 文件',
            ],
        }

        for script, patterns in expectations.items():
            content = self.read_required_script(script)
            for pattern in patterns:
                with self.subTest(script=script, pattern=pattern):
                    self.assertRegex(content, pattern)

            with self.subTest(script=script, assertion="no_probe_gate"):
                self.assertNotIn("ffprobe 校验", content)
                self.assertNotIn("stream=codec_type", content)
                self.assertNotIn("format=duration", content)

    def test_prepare_video_ps1_resets_accepted_nonzero_native_exit(self):
        content = self.read_required_script("prepare-video.ps1")
        self.assertRegex(content, r'Write-Output\s+"OUTPUT_VIDEO=\$EditVideoAbs"\s*\r?\nexit 0')

    def test_prepare_video_scripts_refuse_in_place_output(self):
        expectations = {
            "prepare-video.ps1": r'OrdinalIgnoreCase\.Equals\(\$OriginalVideoAbs,\s*\$EditVideoAbs\)',
            "prepare-video.sh": r'\[ "\$ORIGINAL_VIDEO_ABS" = "\$EDIT_VIDEO_ABS" \]',
        }

        for script, pattern in expectations.items():
            content = self.read_required_script(script)
            with self.subTest(script=script):
                self.assertRegex(content, pattern)

    def test_download_scripts_reuse_existing_original_mkv_for_metadata_refresh(self):
        expectations = {
            "download.ps1": [
                r'\$ExistingOriginalMkv\s*=\s*Join-Path\s+\$FolderName\s+"\$FolderName\.original\.mkv"',
                r'\$HasExistingOriginalMkv\s*=\s*Test-Path\s+\$ExistingOriginalMkv\s+-PathType\s+Leaf',
                r"'--skip-download'",
                r'if\s*\(\$HasExistingOriginalMkv\)',
                r'使用已有原片',
                r'Move-Item\s+-LiteralPath\s+\$OriginalVideoAbs\s+-Destination\s+\$RenderVideoPath\s+-Force',
            ],
            "download.sh": [
                r'EXISTING_ORIGINAL_MKV="\$FOLDER_NAME/\$FOLDER_NAME\.original\.mkv"',
                r'HAS_EXISTING_ORIGINAL_MKV=true',
                r'--skip-download',
                r'使用已有原片',
                r'mv -f "\$ORIGINAL_VIDEO_PATH" "\$RENDER_VIDEO_PATH"',
            ],
        }

        for script, patterns in expectations.items():
            content = read_script(script)
            for pattern in patterns:
                with self.subTest(script=script, pattern=pattern):
                    self.assertRegex(content, pattern)

        ps_content = read_script("pipeline.ps1")
        self.assertLess(
            ps_content.index("& $DownloadPs1 $Url"),
            ps_content.index("& $PrepareVideoScript $RenderVideoPath"),
        )
        sh_content = read_script("pipeline.sh")
        self.assertLess(
            sh_content.index('bash "$DOWNLOAD_SCRIPT" "$URL"'),
            sh_content.index('bash "$PREPARE_VIDEO_SCRIPT" "$RENDER_VIDEO_PATH"'),
        )

    def test_prepare_video_scripts_do_not_forward_ffmpeg_stderr(self):
        for script in ("prepare-video.ps1", "prepare-video.sh"):
            content = self.read_required_script(script)
            with self.subTest(script=script):
                self.assertNotIn("BeginErrorReadLine", content)
                self.assertNotIn("ErrorDataReceived", content)
                self.assertNotIn("[ffmpeg decode]", content)
                self.assertNotIn("[ffmpeg encode]", content)
                self.assertNotRegex(content, r'2>\s*>\(')

    def test_download_ps1_handles_missing_url_in_script_body(self):
        content = read_script("download.ps1")
        self.assertNotRegex(
            content,
            r'\[Parameter\(\s*Mandatory[^)]*Position\s*=\s*0[^)]*HelpMessage\s*=\s*"YouTube video URL"\)\]\s*\r?\n\s*\[string\]\$Url',
        )
        self.assertRegex(content, r'if\s*\(\$Help\s*-or\s*\(-not\s*\$Url\)\)')

    def test_pipeline_scripts_prepare_edit_video_after_download_and_use_render_video_for_burn(self):
        expectations = {
            "pipeline.ps1": [
                r'prepare-video\.ps1',
                r'OUTPUT_RENDER_VIDEO=',
                r'OUTPUT_VIDEO=',
                r'&\s+\$PrepareVideoScript\s+\$RenderVideoPath',
                r'\$RenderVideoPath',
                r'VideoPath\s*=\s*\$RenderVideoPath',
            ],
            "pipeline.sh": [
                r'prepare-video\.sh',
                r'OUTPUT_RENDER_VIDEO=',
                r'OUTPUT_VIDEO=',
                r'bash\s+"\$PREPARE_VIDEO_SCRIPT"\s+"\$RENDER_VIDEO_PATH"',
                r'RENDER_VIDEO_PATH=',
                r'"\$RENDER_VIDEO_PATH"',
            ],
        }

        for script, patterns in expectations.items():
            content = read_script(script)
            for pattern in patterns:
                with self.subTest(script=script, pattern=pattern):
                    self.assertRegex(content, pattern)

    def test_migration_bash_tee_stops_on_download_failure_before_prepare(self):
        content = self.read_required_script("MIGRATION.md")
        download = content.index('./download.sh "URL" | tee "$download_log"')
        capture = content.index('download_exit=${PIPESTATUS[0]}', download)
        guard = content.index('if [ "$download_exit" -ne 0 ]; then', capture)
        prepare = content.index('./prepare-video.sh "$render_video"', guard)

        self.assertLess(download, capture)
        self.assertLess(capture, guard)
        self.assertLess(guard, prepare)
        self.assertIn('exit "$download_exit"', content[guard:prepare])


if __name__ == "__main__":
    unittest.main()
