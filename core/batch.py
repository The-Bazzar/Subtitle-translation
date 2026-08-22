"""Thin command-line entry point for the stage-aware batch runtime."""

from batch_runtime import main


if __name__ == "__main__":
    raise SystemExit(main(_notify_unhandled=True))
else:
    import sys

    import batch_runtime

    sys.modules[__name__] = batch_runtime
