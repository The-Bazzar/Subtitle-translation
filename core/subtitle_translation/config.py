from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in values:
            values[key] = value
    return values


@dataclass(frozen=True)
class ProjectConfig:
    project_dir: Path
    values: dict[str, str]
    invocation_dir: Path | None = None

    @classmethod
    def load(
        cls,
        project_dir: str | Path | None = None,
        invocation_dir: str | Path | None = None,
    ) -> "ProjectConfig":
        invocation_root = Path(invocation_dir or os.getcwd()).expanduser().resolve()
        configured_root = os.environ.get("SUBTITLE_TRANSLATION_PROJECT_DIR", "").strip()
        root = Path(project_dir or configured_root or invocation_root).expanduser().resolve()
        values = _read_env_file(root / ".env")
        values.update({key: value for key, value in os.environ.items()})
        return cls(root, values, invocation_root)

    @property
    def output_dir(self) -> Path:
        return self.invocation_dir or self.project_dir

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def flag(self, key: str, default: bool = False) -> bool:
        value = self.get(key, "").strip().lower()
        if not value:
            return default
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off"}:
            return False
        return default

    def path(self, key: str, default: str = "") -> Path | None:
        value = self.get(key, default).strip()
        if not value:
            return None
        return Path(value).expanduser()

    def resolve_tool(self, env_key: str, default: str) -> str | None:
        configured = self.get(env_key, "").strip()
        candidate = configured or default
        if Path(candidate).is_file():
            return str(Path(candidate).expanduser().resolve())
        return shutil.which(candidate)
