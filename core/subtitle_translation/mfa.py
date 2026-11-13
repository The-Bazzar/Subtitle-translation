"""Experimental local MFA bridge. Callers own admission and final publication."""

from __future__ import annotations

import math
import platform
import re
import shutil
import tempfile
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable

from .artifacts import atomic_write_json, atomic_write_text, file_sha256, json_sha256, read_json_object, text_sha256
from .process import capture_command, child_environment, run_command


MFA_PROFILE = "mfa-3.4-textgrid-v1"
CORPUS_POLICY = "direct-window-corpus-align-v1"


def isolated_python_environment(environment: dict) -> dict:
    result = dict(environment)
    for key in ("PYTHONPATH", "PYTHONHOME"):
        result.pop(key, None)
    result["PYTHONNOUSERSITE"] = "1"
    return result


def vad_cache_fingerprint(hf_home: Path) -> tuple[str, str]:
    cache = hf_home / "hub" / "models--speechbrain--vad-crdnn-libriparty"
    revision = (cache / "refs" / "main").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("invalid MFA VAD revision")
    snapshot = cache / "snapshots" / revision
    files = {path.relative_to(snapshot).as_posix(): file_sha256(path)
             for path in snapshot.rglob("*") if path.is_file()}
    if not files:
        raise ValueError("MFA VAD cache is missing; run setup-mfa")
    return revision, json_sha256(files)


def _local_file(value, label: str) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute() or not Path(value).is_file():
        raise ValueError(f"{label} must be an existing absolute local file")
    return Path(value).resolve()


def _command(value, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(arg, str) or not arg for arg in value):
        raise ValueError(f"invalid {label} argument array")
    _local_file(value[0], label)
    return tuple(value)


@dataclass(frozen=True)
class MfaRuntime:
    command: tuple[str, ...]
    python_command: tuple[str, ...]
    version: str
    dictionary: Path
    acoustic_model: Path
    transport: str = "native"
    distribution: str = ""
    work_directory: Path | None = None
    hf_home: Path | None = None
    vad_revision: str = ""
    vad_sha256: str = ""

    @classmethod
    def for_project(cls, directory: Path) -> "MfaRuntime":
        path = directory / "asr_runtime.json"
        if not path.is_file():
            raise ValueError("MFA runtime is not configured; run subtitle-translation setup-mfa")
        runtime = cls.from_file(path)
        runtime.identity()
        return runtime

    @classmethod
    def from_file(cls, path: Path) -> "MfaRuntime":
        payload = read_json_object(path)
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported ASR runtime schema")
        config = payload.get("mfa")
        if not isinstance(config, dict) or config.get("profile") != MFA_PROFILE:
            raise ValueError("unsupported MFA runtime profile")
        version = config.get("version")
        if not isinstance(version, str) or not re.fullmatch(r"3\.4\.\d+", version):
            raise ValueError("MFA profile requires an exact 3.4.x version")
        transport = config.get("transport", "native")
        distribution = config.get("distribution", "")
        if transport not in {"native", "wsl"} or not isinstance(distribution, str):
            raise ValueError("unsupported MFA transport")
        command = _command(config.get("command"), "MFA command")
        python_command = _command(config.get("python_command"), "MFA environment Python")
        if transport == "wsl":
            if not distribution or any("--exec" not in prefix for prefix in (command, python_command)):
                raise ValueError("WSL runtime requires an explicit distribution and --exec commands")
            for prefix in (command, python_command):
                if (prefix.count("--distribution") != 1 or prefix.index("--distribution") + 1 >= len(prefix)
                        or prefix[prefix.index("--distribution") + 1] != distribution):
                    raise ValueError("MFA and parser must use the selected WSL distribution")
        work_directory = config.get("work_directory")
        if work_directory is not None and (not isinstance(work_directory, str) or not Path(work_directory).is_absolute()):
            raise ValueError("MFA work directory must be absolute")
        hf_home = config.get("hf_home")
        if hf_home is not None and (not isinstance(hf_home, str) or not Path(hf_home).is_absolute()):
            raise ValueError("MFA HF cache must be absolute")
        return cls(
            command, python_command, version,
            _local_file(config.get("dictionary"), "MFA dictionary"),
            _local_file(config.get("acoustic_model"), "MFA acoustic model"),
            transport, distribution, Path(work_directory) if work_directory else None,
            Path(hf_home) if hf_home else None, config.get("vad_revision", ""), config.get("vad_sha256", ""),
        )

    def tool_path(self, path: str | Path) -> str:
        if self.transport == "native":
            return str(path)
        windows = PureWindowsPath(str(path))
        if windows.drive.lower() in {f"\\\\wsl.localhost\\{self.distribution}".lower(),
                                     f"\\\\wsl$\\{self.distribution}".lower()}:
            return "/" + "/".join(windows.parts[1:])
        prefix = self.command[:self.command.index("--exec") + 1]
        result = capture_command([*prefix, "/usr/bin/wslpath", "-a", "-u", str(path)], timeout=30)
        if result.returncode or not result.stdout.strip().startswith("/"):
            raise ValueError("cannot map MFA input path into WSL")
        return result.stdout.strip()

    def invocation(self, command: list[str], root: Path, environment: dict) -> tuple[list[str], Path | None]:
        if self.transport == "native":
            return command, root
        position = command.index("--exec")
        exports = [f"{name}={environment[name]}" for name in (
            "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
            "MKL_NUM_THREADS", "CUDA_VISIBLE_DEVICES",
        )]
        exports.append("PYTHONNOUSERSITE=1")
        if self.hf_home is not None:
            exports.append(f"HF_HOME={self.tool_path(self.hf_home)}")
        return [*command[:position], "--cd", self.tool_path(root), "--exec", "/usr/bin/env",
                "-u", "PYTHONPATH", "-u", "PYTHONHOME",
                f"MFA_ROOT_DIR={self.tool_path(root / 'mfa_root')}", *exports,
                *command[position + 1:]], None

    def identity(self) -> dict:
        if self.hf_home is not None:
            if (not isinstance(self.vad_revision, str) or not re.fullmatch(r"[0-9a-f]{40}", self.vad_revision)
                    or not isinstance(self.vad_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", self.vad_sha256)):
                raise ValueError("MFA VAD identity is invalid; run setup-mfa")
            if self.transport == "native" and vad_cache_fingerprint(self.hf_home) != (self.vad_revision, self.vad_sha256):
                raise ValueError("MFA VAD resources changed; run setup-mfa")
        return {
            "profile": MFA_PROFILE, "version": self.version, "command": list(self.command),
            "python_command": list(self.python_command), "dictionary_sha256": file_sha256(self.dictionary),
            "acoustic_model_sha256": file_sha256(self.acoustic_model),
            "exporter_sha256": file_sha256(Path(__file__).with_name("mfa_export.py")), "num_jobs": 1,
            "transport": self.transport, "distribution": self.distribution,
            "vad_revision": self.vad_revision, "vad_sha256": self.vad_sha256,
        }


def canonical_audio_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth(), audio.getcomptype()) != (16000, 1, 2, "NONE"):
            raise ValueError("MFA requires canonical 16 kHz mono 16-bit PCM WAV")
        frames = audio.getnframes()
        if frames <= 0:
            raise ValueError("MFA input audio is empty")
        actual_bytes = 0
        for block in iter(lambda: audio.readframes(16000 * 30), b""):
            actual_bytes += len(block)
        if actual_bytes != frames * 2:
            raise ValueError("MFA input WAV is truncated")
        return frames / 16000.0


def validate_alignment(payload: dict, text: str, duration: float, version: str, tokenize: Callable) -> list[dict]:
    if payload.get("schema_version") != 1 or payload.get("mfa_version") != version:
        raise ValueError("mfa_output_version_mismatch")
    values = payload.get("words")
    if not isinstance(values, list) or not values:
        raise ValueError("word_timing_unavailable")
    words, tokens, previous_end = [], [], 0.0
    for item in values:
        if not isinstance(item, dict) or not isinstance(item.get("word"), str):
            raise ValueError("word_mapping_failed")
        word, start, end = item["word"], item.get("start"), item.get("end")
        if (type(start) not in (float, int) or type(end) not in (float, int)
                or not math.isfinite(start) or not math.isfinite(end)
                or start < previous_end - 0.001 or not 0 <= start < end <= duration + 0.001):
            raise ValueError("invalid_timestamps")
        if not word.strip() or word.lower() in {"<unk>", "<eps>", "sil", "sp", "spn"}:
            raise ValueError("word_mapping_failed")
        current = tokenize(word)
        if not current:
            raise ValueError("word_mapping_failed")
        tokens.extend(current)
        words.append({"word": word, "start": float(start), "end": float(end)})
        previous_end = end
    if "".join(tokens).replace("'", "") != "".join(tokenize(text)).replace("'", ""):
        raise ValueError("word_mapping_failed")
    return words


def corpus_execution_directory(runtime: MfaRuntime, cache_root: Path) -> Path:
    if runtime.transport != "native" or platform.system() != "Windows":
        return cache_root
    candidates = [runtime.work_directory, Path(tempfile.gettempdir()) / "subtitle-mfa"]
    for directory in candidates:
        if directory is None:
            continue
        root = directory.resolve() / "r" / uuid.uuid4().hex
        # Leave room for MFA's SQLite tables, journals and 64-character window IDs.
        if len(str(root)) + 100 > 240:
            continue
        root.mkdir(parents=True, exist_ok=False)
        return root
    raise ValueError("mfa_windows_execution_path_too_long")


def align_window_corpus(entries: list[dict], runtime: MfaRuntime, work_root: Path, tokenize: Callable,
                        *, admission_guard=None, time_budget: float = 300.0) -> dict:
    """One native align/export for all project windows; retain local result status."""
    if not entries:
        return {}
    if admission_guard:
        admission_guard()
    ids = [e["id"] for e in entries]
    if (len(set(ids)) != len(ids) or any(not isinstance(i, str) or not re.fullmatch(r"[0-9a-f]{64}", i) for i in ids)
            or any(not isinstance(e["text"], str) or not e["text"].strip() for e in entries)):
        raise ValueError("invalid MFA window corpus")
    deadline = time.monotonic() + max(0.0, time_budget)
    resources = runtime.identity()

    def snapshots():
        return [{"id": e["id"], "audio_sha256": file_sha256(e["audio"]), "text_sha256": text_sha256(e["text"]),
                 "duration": canonical_audio_duration(e["audio"])} for e in entries]

    inputs = snapshots()
    identity = {"policy": CORPUS_POLICY, "inputs": inputs, "runtime": resources}
    cache = work_root.resolve() / "corpus_alignment" / json_sha256(identity)
    root = cache / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=False)
    environment = isolated_python_environment(child_environment())
    environment.update(MFA_ROOT_DIR=str(root / "mfa_root"), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                       OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1", CUDA_VISIBLE_DEVICES="")
    if runtime.hf_home is not None:
        environment["HF_HOME"] = str(runtime.hf_home)
    execution_root = root
    exporter = runtime.tool_path(Path(__file__).with_name("mfa_export.py"))
    assets = (["--expected-vad-revision", runtime.vad_revision, "--expected-vad-sha256", runtime.vad_sha256]
              if runtime.hf_home else [])

    def invoke(arguments, probe=False):
        if admission_guard:
            admission_guard()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("mfa_project_timeout")
        command, cwd = runtime.invocation(arguments, execution_root, environment)
        result = run_command(command, cwd=cwd, env=environment, label="MFA corpus", timeout=min(30, remaining) if probe else remaining)
        if admission_guard:
            admission_guard()
        if result.returncode == 124:
            raise TimeoutError("mfa_project_timeout")
        if result.returncode:
            raise ValueError(f"mfa_command_failed:{result.returncode}")

    def check_inputs():
        if runtime.identity() != resources or snapshots() != inputs:
            raise ValueError("mfa_corpus_input_changed")

    def analyze(payload, probe):
        if (any(payload.get(k) != probe.get(k) for k in probe)
                or payload.get("schema_version") != 1 or payload.get("mfa_version") != runtime.version):
            raise ValueError("mfa_output_version_mismatch")
        rows = payload.get("items")
        if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
                or len(rows) != len(ids) or any(not isinstance(row.get("id"), str) for row in rows)
                or len({row["id"] for row in rows}) != len(ids) or {row["id"] for row in rows} != set(ids)):
            raise ValueError("invalid MFA corpus output IDs")
        by_id = {row["id"]: row for row in rows}
        results = {}
        for entry, snapshot in zip(entries, inputs):
            row = by_id[entry["id"]]
            try:
                if row.get("error"):
                    raise ValueError("mfa_window_export_failed")
                words = validate_alignment({**payload, "words": row.get("words")}, entry["text"],
                                           snapshot["duration"], runtime.version, tokenize)
                results[entry["id"]] = {"status": "available", "reason": None, "words": words}
            except ValueError as error:
                results[entry["id"]] = {"status": "unavailable", "reason": str(error), "words": []}
        return results

    failure = {i: {"status": "unavailable", "reason": "mfa_corpus_failed", "words": []} for i in ids}
    try:
        probe_path = root / "probe.json"
        invoke([*runtime.python_command, exporter, "--expected-version", runtime.version,
                "--output", runtime.tool_path(probe_path), *assets], probe=True)
        probe = read_json_object(probe_path)
        if (probe.get("schema_version") != 1 or probe.get("mfa_version") != runtime.version
                or not isinstance(probe.get("praatio_version"), str) or not probe["praatio_version"]):
            raise ValueError("mfa_output_version_mismatch")
        if runtime.hf_home and (probe.get("vad_revision"), probe.get("vad_sha256")) != (runtime.vad_revision, runtime.vad_sha256):
            raise ValueError("mfa_vad_resource_mismatch")
        cached_result = None
        try:
            manifest = read_json_object(cache / "current.json")
            previous = (cache / manifest["generation"]).resolve()
            if previous.parent == cache and previous.name == manifest["generation"] and manifest["identity"] == identity:
                record = read_json_object(previous / "alignment.json")
                payload = read_json_object(previous / "exported.json")
                results = analyze(payload, probe)
                if (json_sha256(record) == manifest["result_sha256"] and record["identity"] == identity
                        and record["probe"] == probe and record["results"] == results):
                    cached_result = results
        except (OSError, ValueError, KeyError, TypeError):
            pass
        check_inputs()
        if cached_result is not None:
            return cached_result
        execution_root = corpus_execution_directory(runtime, root)
        if execution_root != root:
            atomic_write_json(root / "execution.json", {"policy": "native-short-run-v1", "directory": str(execution_root)})
        environment["MFA_ROOT_DIR"] = str(execution_root / "mfa_root")
        corpus, aligned = execution_root / "corpus", execution_root / "aligned"
        corpus.mkdir()
        for entry, snapshot in zip(entries, inputs):
            if admission_guard:
                admission_guard()
            shutil.copyfile(entry["audio"], corpus / f"{entry['id']}.wav")
            if file_sha256(corpus / f"{entry['id']}.wav") != snapshot["audio_sha256"]:
                raise ValueError("mfa_corpus_input_changed")
            atomic_write_text(corpus / f"{entry['id']}.lab", entry["text"] + "\n")
        ids_path = root / "ids.json"
        atomic_write_json(ids_path, {"ids": ids})
        invoke([*runtime.command, "align", runtime.tool_path(corpus), runtime.tool_path(runtime.dictionary),
                runtime.tool_path(runtime.acoustic_model), runtime.tool_path(aligned), "--num_jobs", "1",
                "--temporary_directory", runtime.tool_path(execution_root / "align_temp"), "--clean"])
        export_path = root / "exported.json"
        invoke([*runtime.python_command, exporter, "--expected-version", runtime.version, "--aligned-directory", runtime.tool_path(aligned),
                "--ids-file", runtime.tool_path(ids_path), "--output", runtime.tool_path(export_path), *assets])
        results = analyze(read_json_object(export_path), probe)
        check_inputs()
        record = {"identity": identity, "probe": probe, "results": results}
        if admission_guard:
            admission_guard()
        atomic_write_json(root / "alignment.json", record)
        atomic_write_json(cache / "current.json", {"identity": identity, "generation": root.name, "result_sha256": json_sha256(record)})
        return results
    except (OSError, ValueError, TimeoutError) as error:
        if admission_guard:
            admission_guard()
        for item in failure.values():
            item["reason"] = str(error)
        atomic_write_json(root / "failure.json", {"identity": identity, "results": failure})
        return failure
