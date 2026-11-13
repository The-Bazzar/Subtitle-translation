"""Install MFA as a managed tool environment, separate from the Python CLI."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import tarfile
import tempfile
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from .artifacts import atomic_write_json, file_sha256, read_json_object
from .config import ProjectConfig
from .mfa import MFA_PROFILE, MfaRuntime, isolated_python_environment, vad_cache_fingerprint
from .process import capture_command, run_command


MFA_VERSION = "3.4.3"
MICROMAMBA_VERSION = "2.10.0"
MICROMAMBA_URL = f"https://micro.mamba.pm/api/micromamba/linux-64/{MICROMAMBA_VERSION}"
WINDOWS_MICROMAMBA_URL = f"https://micro.mamba.pm/api/micromamba/win-64/{MICROMAMBA_VERSION}"
MODEL_VERSION = "3.0.0"
MODEL_RESOURCES = {
    "acoustic": ("english_mfa.zip", 92175348),
    "dictionary": ("english_us_mfa.dict", 3844549),
}
ENVIRONMENT_SPECS = ["python=3.11", f"montreal-forced-aligner={MFA_VERSION}", "kaldi=*=cpu*",
                     "pytorch=2.8.0=cpu*", "torchaudio=2.8.0=cpu*", "huggingface_hub<1"]
WINDOWS_ENVIRONMENT_SPECS = ["python=3.11", f"montreal-forced-aligner={MFA_VERSION}",
                             "kaldi=*=cpu*", "huggingface_hub<1", "pip"]


def download_model(kind: str, destination: Path) -> None:
    filename, expected_size = MODEL_RESOURCES[kind]
    model = filename.rsplit(".", 1)[0]
    url = f"https://github.com/MontrealCorpusTools/mfa-models/releases/download/{kind}-{model}-v{MODEL_VERSION}/{filename}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.download")
    try:
        size = 0
        with urllib.request.urlopen(url, timeout=60) as response, open(temporary, "wb") as file:
            while block := response.read(1024 * 1024):
                size += len(block)
                if size > expected_size:
                    raise ValueError("MFA resource exceeds its pinned release size")
                file.write(block)
        if size != expected_size:
            raise ValueError("MFA resource download is truncated")
        if kind == "acoustic":
            with zipfile.ZipFile(temporary) as archive:
                if archive.testzip() is not None:
                    raise ValueError("MFA acoustic archive integrity check failed")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def wsl_host_path(path: str, distribution: str) -> Path:
    posix = PurePosixPath(path)
    if not posix.is_absolute() or ".." in posix.parts:
        raise ValueError("WSL tool paths must be absolute")
    return Path(PureWindowsPath(f"\\\\wsl.localhost\\{distribution}\\", *posix.parts[1:]))


class ToolHost:
    def __init__(self, distribution: str = ""):
        self.distribution = distribution
        self.prefix: list[str] = []
        self.windows = platform.system() == "Windows" and not distribution
        if self.windows:
            if platform.machine() not in {"AMD64", "x86_64"}:
                raise ValueError("native Windows MFA requires x86_64")
            self.root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "subtitle-translation"
        elif platform.system() == "Windows":
            wsl = shutil.which("wsl.exe")
            if not wsl:
                raise ValueError("WSL is required for managed MFA on Windows; install a Linux distribution first")
            if not distribution:
                result = capture_command([wsl, "--list", "--quiet"], timeout=30)
                names = [line.strip("\ufeff\ufffd \r\n") for line in result.stdout.replace("\x00", "").splitlines()]
                names = [name for name in names if name and not name.lower().startswith("docker")]
                if result.returncode or not names:
                    raise ValueError("no usable WSL distribution; run setup-mfa --distribution NAME after installing WSL")
                distribution = "Debian" if "Debian" in names else names[0]
                self.distribution = distribution
            self.prefix = [wsl, "--distribution", distribution, "--exec"]
            result = capture_command([*self.prefix, "/usr/bin/printenv", "HOME"], timeout=30)
            if result.returncode or not result.stdout.strip().startswith("/"):
                raise ValueError("cannot locate the selected WSL home directory")
            self.root = PurePosixPath(result.stdout.strip()) / ".local/share/subtitle-translation"
            architecture = capture_command([*self.prefix, "/usr/bin/uname", "-m"], timeout=30)
            if architecture.returncode or architecture.stdout.strip() != "x86_64":
                raise ValueError("the managed MFA profile currently requires Linux x86_64")
        else:
            if platform.system() != "Linux" or platform.machine() not in {"x86_64", "AMD64"}:
                raise ValueError("the managed MFA profile currently supports Linux x86_64 and Windows/WSL")
            if distribution:
                raise ValueError("--distribution is only applicable to Windows/WSL")
            self.root = PurePosixPath(str(Path.home())) / ".local/share/subtitle-translation"

    def path(self, value: str | PurePosixPath) -> Path:
        return wsl_host_path(str(value), self.distribution) if self.prefix else Path(str(value))

    def execution(self, command: list[str], env: dict | None = None) -> tuple[list[str], dict]:
        environment = isolated_python_environment({**os.environ, **(env or {})})
        exports = {k: v for k, v in (env or {}).items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
        exports["PYTHONNOUSERSITE"] = "1"
        prefix = ([*self.prefix, "/usr/bin/env", "-u", "PYTHONPATH", "-u", "PYTHONHOME",
                   *[f"{k}={v}" for k, v in exports.items()]] if self.prefix else [])
        return [*prefix, *command], environment

    def run(self, command: list[str], label: str, timeout: float = 1800, *, env: dict | None = None) -> None:
        arguments, environment = self.execution(command, env)
        result = run_command(arguments, env=environment, label=label, timeout=timeout)
        if result.returncode:
            raise ValueError(f"{label} failed with exit code {result.returncode}")

    def capture(self, command: list[str], *, env: dict | None = None) -> str:
        arguments, environment = self.execution(command, env)
        result = capture_command(arguments, env=environment, timeout=60)
        if result.returncode:
            raise ValueError(f"MFA environment probe failed: {result.stderr}")
        return result.stdout.strip()


def install_micromamba(destination: Path, *, windows: bool = False) -> None:
    if destination.is_file():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive_path = ""
    executable_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.candidate")
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".tar.bz2", delete=False) as archive:
            archive_path = archive.name
            with urllib.request.urlopen(WINDOWS_MICROMAMBA_URL if windows else MICROMAMBA_URL, timeout=60) as response:
                size = 0
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > 100 * 1024 * 1024:
                        raise ValueError("micromamba download exceeds the expected size limit")
                    archive.write(block)
        with tarfile.open(archive_path, "r:bz2") as archive:
            member = archive.getmember("Library/bin/micromamba.exe" if windows else "bin/micromamba")
            if not member.isfile() or member.size > 100 * 1024 * 1024:
                raise ValueError("invalid micromamba archive member")
            with archive.extractfile(member) as source, open(executable_path, "wb") as target:
                shutil.copyfileobj(source, target)
        executable_path.chmod(0o755)
        os.replace(executable_path, destination)
    finally:
        executable_path.unlink(missing_ok=True)
        if archive_path:
            Path(archive_path).unlink(missing_ok=True)


def locate_model(root: Path, kind: str, name: str) -> Path:
    directory = root / "pretrained_models" / kind
    extensions = (".dict", ".zip") if kind == "dictionary" else (".zip",)
    for extension in extensions:
        candidate = directory / (name + extension)
        if candidate.is_file():
            return candidate
    raise ValueError(f"MFA model download did not create the expected {kind} resource: {name}")


def environment_versions(host: ToolHost, base: list[str]) -> dict:
    output = host.capture([*base, "python", "-c",
                           "import importlib.metadata as m,json; "
                           "v={d.metadata['Name'].lower().replace('-','_'):d.version for d in m.distributions()}; "
                           "print(json.dumps({k:v.get(k,'') for k in ['torch','torchaudio','speechbrain','huggingface_hub']}))"])
    result = json.loads(output)
    if not isinstance(result, dict):
        raise ValueError("invalid MFA dependency probe")
    return result


def prepare_vad(host: ToolHost, base: list[str]) -> tuple[Path, dict]:
    hf_home = host.root / "huggingface"
    manifest = host.path(host.root / "vad_manifest.json")
    script = Path(__file__).with_name("mfa_prefetch.py")
    tool_script = host.capture(["/usr/bin/wslpath", "-a", "-u", str(script)]) if host.prefix else str(script)

    def probe():
        if host.prefix:
            return json.loads(host.capture([*base, "python", tool_script, "--probe"], env={"HF_HOME": str(hf_home)}))
        revision, digest = vad_cache_fingerprint(host.path(hf_home))
        return {"revision": revision, "files_sha256": digest}
    try:
        data = read_json_object(manifest)
        actual = probe()
        if (data.get("schema_version") == 1 and data.get("speechbrain_version") == "1.0.3"
                and all(actual[key] == data[key] for key in ("revision", "files_sha256"))):
            return host.path(hf_home), data
    except (OSError, ValueError, KeyError, TypeError):
        pass
    host.run([*base, "python", tool_script, "--output", str(host.root / "vad_manifest.json"),
              "--savedir", str(host.root / "vad")], "Prepare MFA VAD",
             env={"HF_HOME": str(hf_home), "OMP_NUM_THREADS": "1", "CUDA_VISIBLE_DEVICES": ""})
    data = read_json_object(manifest)
    actual = probe()
    if any(actual[key] != data[key] for key in ("revision", "files_sha256")):
        raise ValueError("MFA VAD cache integrity check failed")
    return host.path(hf_home), data


def install_runtime(project_dir: Path, distribution: str = "") -> Path:
    if not project_dir.is_dir():
        raise ValueError("MFA configuration directory does not exist")
    host = ToolHost(distribution)
    root = host.root
    windows = getattr(host, "windows", False)
    specs = WINDOWS_ENVIRONMENT_SPECS if windows else ENVIRONMENT_SPECS
    micromamba = root / ("tools/bin/micromamba.exe" if windows else "tools/bin/micromamba")
    environment = root / "mfa-3.4"
    models = root / "models" / f"english-{MODEL_VERSION}"
    install_micromamba(host.path(micromamba), windows=windows)
    if not windows:
        host.run(["/usr/bin/chmod", "755", str(micromamba)], "Prepare micromamba executable", timeout=30)
    actual_mamba = host.capture([str(micromamba), "--version"])
    if actual_mamba != MICROMAMBA_VERSION:
        raise ValueError(f"micromamba version mismatch: expected {MICROMAMBA_VERSION}, got {actual_mamba}")
    base = [str(micromamba), "run", "--prefix", str(environment)]
    if not host.path(environment / ("Scripts/mfa.exe" if windows else "bin/mfa")).is_file():
        host.run([str(micromamba), "create", "--root-prefix", str(root / "mamba"),
                  "--prefix", str(environment), "--channel", "conda-forge", "--override-channels", "--yes",
                  *specs], "Install MFA")
    version = host.capture([*base, "python", "-c", "import importlib.metadata; print(importlib.metadata.version('montreal-forced-aligner'))"])
    if version != MFA_VERSION:
        raise ValueError(f"MFA version mismatch: expected {MFA_VERSION}, got {version}")
    dependencies = environment_versions(host, base)
    if (dependencies.get("torch", "").split("+", 1)[0] != "2.8.0"
            or dependencies.get("torchaudio", "").split("+", 1)[0] != "2.8.0"
            or not dependencies.get("huggingface_hub", "").startswith("0.")):
        if windows:
            if not dependencies.get("huggingface_hub", "").startswith("0."):
                host.run([str(micromamba), "install", "--root-prefix", str(root / "mamba"),
                          "--prefix", str(environment), "--channel", "conda-forge", "--override-channels",
                          "--yes", "huggingface_hub<1"], "Prepare MFA Hub dependency")
            host.run([*base, "python", "-m", "pip", "install", "--disable-pip-version-check",
                      "torch==2.8.0", "torchaudio==2.8.0", "--index-url", "https://download.pytorch.org/whl/cpu"],
                     "Prepare MFA CPU dependencies")
        else:
            host.run([str(micromamba), "install", "--root-prefix", str(root / "mamba"), "--prefix", str(environment),
                      "--channel", "conda-forge", "--override-channels", "--yes", *ENVIRONMENT_SPECS], "Prepare MFA CPU dependencies")
        dependencies = environment_versions(host, base)
    if (dependencies.get("torch", "").split("+", 1)[0] != "2.8.0"
            or dependencies.get("torchaudio", "").split("+", 1)[0] != "2.8.0"
            or not dependencies.get("huggingface_hub", "").startswith("0.")):
        raise ValueError("MFA CPU dependency versions do not match the managed profile")
    if dependencies.get("speechbrain") != "1.0.3":
        host.run([*base, "python", "-m", "pip", "install", "--disable-pip-version-check", "speechbrain==1.0.3"], "Install MFA VAD dependencies")
    hf_home, vad = prepare_vad(host, base)
    model_base = [*base, "mfa"]
    model_env = {"MFA_ROOT_DIR": str(models), "OMP_NUM_THREADS": "1", "CUDA_VISIBLE_DEVICES": ""}
    for kind, name in (("acoustic", "english_mfa"), ("dictionary", "english_us_mfa")):
        try:
            locate_model(host.path(models), kind, name)
        except ValueError:
            filename = MODEL_RESOURCES[kind][0]
            download_model(kind, host.path(models / "pretrained_models" / kind / filename))
        model_path = locate_model(host.path(models), kind, name)
        tool_model_path = str(models / "pretrained_models" / kind / model_path.name)
        if kind == "acoustic":
            host.run([*model_base, "model", "inspect", kind, tool_model_path], "Validate MFA acoustic", env=model_env)
        else:
            # MFA 3.4.3's dictionary pretty-printer passes a Path where a model is required.
            host.run([*base, "python", "-c",
                      "from montreal_forced_aligner.models import DictionaryModel; import sys; "
                      "m=DictionaryModel(sys.argv[1]); assert m.load_dictionary_paths(); print('MFA dictionary: OK')",
                      tool_model_path], "Validate MFA dictionary")
    dictionary = locate_model(host.path(models), "dictionary", "english_us_mfa")
    acoustic = locate_model(host.path(models), "acoustic", "english_mfa")
    work = host.path(root / "work")
    work.mkdir(parents=True, exist_ok=True)
    config = {
        "profile": MFA_PROFILE, "version": version, "transport": "wsl" if host.prefix else "native",
        "distribution": host.distribution, "command": [*host.prefix, *base, "mfa"],
        "python_command": [*host.prefix, *base, "python"], "dictionary": str(dictionary),
        "acoustic_model": str(acoustic), "work_directory": str(work), "languages": ["en"],
        "micromamba_sha256": file_sha256(host.path(micromamba)),
        "model_versions": {"acoustic": MODEL_VERSION, "dictionary": MODEL_VERSION},
        "hf_home": str(hf_home), "vad_revision": vad["revision"], "vad_sha256": vad["files_sha256"],
    }
    destination = project_dir / "asr_runtime.json"
    manifest = read_json_object(destination) if destination.exists() else {"schema_version": 1}
    if manifest.get("schema_version") != 1:
        raise ValueError("cannot overwrite an unsupported ASR runtime manifest")
    manifest["mfa"] = config
    candidate = project_dir / f".asr_runtime.{uuid.uuid4().hex}.candidate.json"
    try:
        atomic_write_json(candidate, manifest)
        runtime = MfaRuntime.from_file(candidate)
        runtime.identity()
        atomic_write_json(destination, manifest)
    finally:
        candidate.unlink(missing_ok=True)
    return destination


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="subtitle-translation setup-mfa", description="Install the managed English MFA tool environment")
    parser.add_argument("--distribution", default="", help="Use an existing WSL distribution instead of native Windows")
    args = parser.parse_args(argv)
    import sys

    try:
        path = install_runtime(ProjectConfig.load().project_dir, args.distribution)
    except (OSError, ValueError, tarfile.TarError) as error:
        print(f"Error: MFA setup failed: {error}", file=sys.stderr)
        return 1
    print(f"MFA runtime ready: {path}")
    return 0
