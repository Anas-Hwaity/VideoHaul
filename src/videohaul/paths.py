from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "VideoHaul"
IS_WINDOWS = os.name == "nt"


def data_root() -> Path:
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        return base / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME


DATA_ROOT = data_root()
BIN_DIR = DATA_ROOT / "bin"
TOOLS_DIR = DATA_ROOT / "tools"
CACHE_DIR = DATA_ROOT / "cache"
BROWSER_DIR = DATA_ROOT / "browser"
PYTHON_PACKAGES_DIR = DATA_ROOT / "python-packages"
STATE_DIR = DATA_ROOT / "state"
DATABASE_PATH = STATE_DIR / "videohaul.sqlite3"
LOG_PATH = DATA_ROOT / "videohaul.log"
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads" / "VideoHaul"


def ensure_directories() -> None:
    for path in (DATA_ROOT, BIN_DIR, TOOLS_DIR, CACHE_DIR, BROWSER_DIR, PYTHON_PACKAGES_DIR, STATE_DIR, DEFAULT_DOWNLOAD_DIR):
        path.mkdir(parents=True, exist_ok=True)
