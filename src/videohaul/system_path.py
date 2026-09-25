from __future__ import annotations

import os
from pathlib import Path


def _parts(value: str) -> list[str]:
    return [item.strip().strip('"') for item in value.split(os.pathsep) if item.strip()]


def ensure_current_path(directory: str | Path) -> bool:
    value = str(Path(directory).resolve())
    current = _parts(os.environ.get("PATH", ""))
    if any(item.lower() == value.lower() for item in current):
        return False
    os.environ["PATH"] = value + os.pathsep + os.environ.get("PATH", "")
    return True
