from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


_active_processes: set[subprocess.Popen] = set()
_active_processes_lock = threading.RLock()


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str = ""
    stderr: str = ""


def _display_command(args: Sequence[str]) -> str:
    return " ".join(str(arg) for arg in args)


def run_command(
    args: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    label: str = "",
) -> CommandResult:
    command = tuple(str(arg) for arg in args)
    if label:
        print(f"{label}: {_display_command(command)}", file=sys.stderr)
    process: subprocess.Popen | None = None
    try:
        process_kwargs: dict[str, object] = {}
        if os.name == "nt":
            process_kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
            )
        else:
            process_kwargs["start_new_session"] = True
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            **process_kwargs,
        )
        with _active_processes_lock:
            _active_processes.add(process)
        returncode = int(process.wait())
    except FileNotFoundError as error:
        print(f"Error: command not found: {command[0]}", file=sys.stderr)
        return CommandResult(command, 127, "", str(error))
    except OSError as error:
        print(f"Error: failed to start command: {error}", file=sys.stderr)
        return CommandResult(command, 1, "", str(error))
    finally:
        if process is not None:
            with _active_processes_lock:
                _active_processes.discard(process)
    return CommandResult(command, returncode)


def terminate_active_processes() -> None:
    """Terminate commands started by this process, including their child trees."""
    with _active_processes_lock:
        processes = tuple(_active_processes)
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (FileNotFoundError, ProcessLookupError, OSError):
            try:
                process.kill()
            except OSError:
                pass


def capture_command(
    args: Sequence[str],
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    command = tuple(str(arg) for arg in args)
    try:
        completed = subprocess.run(
            list(command),
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as error:
        return CommandResult(command, 127, "", str(error))
    except OSError as error:
        return CommandResult(command, 1, "", str(error))
    return CommandResult(
        command,
        int(completed.returncode),
        completed.stdout or "",
        completed.stderr or "",
    )


def child_environment(config_values: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    if config_values:
        for key, value in config_values.items():
            environment.setdefault(key, value)
    return environment
