"""Thin command-line entry point for the stage-aware batch runtime."""

from batch_runtime import main


if __name__ == "__main__":
    raise SystemExit(main(_notify_unhandled=True))
