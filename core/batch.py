"""Thin command-line entry point for the stage-aware batch runtime."""

import batch_runtime


if __name__ == "__main__":
    raise SystemExit(batch_runtime.main(_notify_unhandled=True))
else:
    import sys

    sys.modules[__name__] = batch_runtime
