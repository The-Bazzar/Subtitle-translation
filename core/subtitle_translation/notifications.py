from __future__ import annotations

import sys
import time


def emit_bell(kind: str, *, stream=None) -> None:
    stream = stream or sys.stderr
    delays = (0.08,) if kind == "success" else (0.18, 0.18)
    try:
        for delay in (0.0, *delays):
            if delay:
                time.sleep(delay)
            stream.write("\a")
            stream.flush()
    except (OSError, ValueError):
        return
