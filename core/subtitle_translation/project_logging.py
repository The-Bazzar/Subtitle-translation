"""Per-project run logs with task-local stage ownership."""

from __future__ import annotations

import contextvars
import functools
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from loguru import logger


LOG_FILENAME = "subtitle-translation.log"
FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS Z} | {level: <7} | {extra[project]} | {extra[stage]} | {message}"
CONSOLE_FORMAT = ("<green>{time:YYYY-MM-DD HH:mm:ss.SSS Z}</green> | <level>{level: <7}</level> | "
                  "<cyan>{extra[project]}</cyan> | <magenta>{extra[stage]}</magenta> | <level>{message}</level>")
_session = contextvars.ContextVar("subtitle_log_session", default=None)
_stage = contextvars.ContextVar("subtitle_log_stage", default="run")
_project = contextvars.ContextVar("subtitle_log_project", default="")
_lock = threading.RLock()
_console_handlers = []
_active = set()


@dataclass(frozen=True)
class Session:
    directory: Path
    identity: str
    console_level: int
    capture_children: bool = False
    filename: str = LOG_FILENAME


def _console(message):
    sys.stderr.write(str(message))
    sys.stderr.flush()


def _initialize():
    with _lock:
        if not _console_handlers:
            try:
                logger.remove(0)
            except ValueError:
                # An embedding application may already have removed the default sink.
                pass
            handler = logger.add(_console, level="TRACE", format=CONSOLE_FORMAT, colorize=True,
                       filter=lambda r: r["extra"].get("subtitle_log", False)
                       and r["level"].no >= r["extra"]["console_level"],
                       backtrace=False, diagnose=False)
            _console_handlers.append(handler)


def log(level, message, *, stage=None):
    _initialize()
    session = _session.get()
    logger.bind(subtitle_log=True, run_id=session.identity if session else "",
                project=_project.get() or (session.directory.name if session else "-"),
                stage=stage or _stage.get(), console_level=session.console_level if session else 20).log(
                    level, str(message).replace("\r", "\\r").replace("\n", "\\n").replace("\x1b", "\\x1b"))


def active_batch_log_path():
    session = _session.get()
    return session.directory / session.filename if session and session.capture_children else None


def validate_log_level(value):
    return logger.level(str(value).strip().upper()).name


@contextmanager
def project_run(directory, level="INFO", *, filename=LOG_FILENAME, capture_children=False):
    directory = Path(directory).resolve()
    current = _session.get()
    if current is not None and (current.directory == directory or current.capture_children):
        project_token = _project.set(directory.name)
        try:
            yield current
        finally:
            _project.reset(project_token)
        return
    if Path(filename).name != filename:
        raise ValueError("log filename must not contain a directory")
    level_number = logger.level(validate_log_level(level)).no
    _initialize()
    with _lock:
        if directory in _active:
            raise ValueError("another logging run is already active for this project")
        _active.add(directory)
    handler = token = project_token = None
    try:
        identity = uuid.uuid4().hex
        handler = logger.add(directory / filename, mode="w", encoding="utf-8", level=level_number,
                             format=FORMAT, colorize=False, backtrace=False, diagnose=False,
                             filter=lambda r: r["extra"].get("run_id") == identity)
        session = Session(directory, identity, level_number, capture_children, filename)
        token = _session.set(session)
        project_token = _project.set(directory.name)
        log("INFO", f"Run started; log={directory / filename}")
        try:
            yield session
        except BaseException as error:
            log("WARNING" if isinstance(error, (KeyboardInterrupt, SystemExit)) else "ERROR",
                f"Run stopped: {type(error).__name__}")
            raise
        else:
            log("INFO", "Run closed")
    finally:
        if token is not None:
            _session.reset(token)
        if project_token is not None:
            _project.reset(project_token)
        if handler is not None:
            logger.remove(handler)
        with _lock:
            _active.discard(directory)


def logged_stage(name):
    def decorate(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            if _stage.get() == name:
                return function(*args, **kwargs)
            token = _stage.set(name)
            start = time.monotonic()
            active = _session.get() is not None
            if active:
                log("INFO", f"{function.__name__}: started")
            try:
                result = function(*args, **kwargs)
                if active:
                    failed = hasattr(result, "success") and not result.success
                    log("ERROR" if failed else "INFO",
                        f"{function.__name__}: {'failed' if failed else 'finished'} in {time.monotonic() - start:.2f}s")
                return result
            except BaseException as error:
                if active:
                    log("WARNING" if isinstance(error, KeyboardInterrupt) else "ERROR", f"Stopped: {type(error).__name__}")
                raise
            finally:
                _stage.reset(token)
        return wrapped
    return decorate
