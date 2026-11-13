"""Prepare SpeechBrain VAD inside the MFA tool environment during setup only."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
from pathlib import Path


def model_snapshot() -> Path:
    from huggingface_hub import snapshot_download

    try:
        snapshot = Path(snapshot_download("speechbrain/vad-crdnn-libriparty", revision="main", local_files_only=True))
        if all((snapshot / name).is_file() and (snapshot / name).stat().st_size > 0
               for name in ("hyperparams.yaml", "model.ckpt", "mean_var_norm.ckpt")):
            return snapshot
    except (OSError, ValueError):
        # Setup can download a snapshot when no complete local copy is available.
        pass
    return Path(snapshot_download("speechbrain/vad-crdnn-libriparty", revision="main"))


def load_vad(snapshot: Path, savedir: Path):
    from hyperpyyaml import load_hyperpyyaml
    from speechbrain.inference.VAD import VAD
    from speechbrain.utils.fetching import LocalStrategy

    # This fixed model has no custom.py; load only its YAML and checkpoint files.
    with (snapshot / "hyperparams.yaml").open(encoding="utf-8") as source:
        hparams = load_hyperpyyaml(source)
    savedir.mkdir(parents=True, exist_ok=True)
    hparams["savedir"] = str(savedir)
    pretrainer = hparams["pretrainer"]
    pretrainer.set_collect_in(savedir)
    pretrainer.collect_files(default_source=snapshot.as_posix(), local_strategy=LocalStrategy.COPY)
    pretrainer.load_collected()
    return VAD(hparams["modules"], hparams, run_opts={"device": "cpu"})


def cache_info(hf_home: Path) -> dict:
    cache = hf_home / "hub" / "models--speechbrain--vad-crdnn-libriparty"
    revision = (cache / "refs/main").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("invalid MFA VAD revision")
    snapshot = cache / "snapshots" / revision
    files = {file.relative_to(snapshot).as_posix(): hashlib.sha256(file.read_bytes()).hexdigest()
             for file in snapshot.rglob("*") if file.is_file()}
    if not files:
        raise ValueError("MFA VAD cache is missing")
    digest = hashlib.sha256(json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"schema_version": 1, "revision": revision, "files_sha256": digest,
            "speechbrain_version": importlib.metadata.version("speechbrain")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output")
    parser.add_argument("--savedir")
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args(argv)
    if not args.probe:
        if not args.savedir or not args.output:
            parser.error("--savedir and --output are required when preparing VAD")
        load_vad(model_snapshot(), Path(args.savedir))
    result = cache_info(Path(os.environ["HF_HOME"]))
    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    else:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
