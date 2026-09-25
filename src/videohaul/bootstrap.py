from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import os
from pathlib import Path
import site
import subprocess
import sys

from .system_path import ensure_current_path

REQUIRED = {
    "fastapi": ("fastapi", "fastapi>=0.128,<1", (0, 128), (1, 0)),
    "uvicorn": ("uvicorn", "uvicorn>=0.35,<1", (0, 35), (1, 0)),
    "webview": ("pywebview", "pywebview>=6,<7", (6, 0), (7, 0)),
    "streamlink": ("streamlink", "streamlink>=7,<8", (7, 0), (8, 0)),
    "playwright": ("playwright", "playwright>=1.55,<2", (1, 55), (2, 0)),
}



def _version_tuple(value: str) -> tuple[int, ...]:
    parts = []
    for piece in str(value).split("."):
        digits = "".join(character for character in piece if character.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _version_ok(distribution: str, minimum: tuple[int, ...], maximum: tuple[int, ...]) -> bool:
    try:
        value = _version_tuple(importlib.metadata.version(distribution))
        return value >= minimum and value < maximum
    except Exception:
        return False


def _activate_user_site() -> None:
    value = site.getusersitepackages()
    if value and value not in sys.path:
        sys.path.insert(0, value)


def _user_scripts() -> Path:
    base = Path(site.getuserbase())
    return base / ("Scripts" if os.name == "nt" else "bin")


def missing_packages() -> list[str]:
    _activate_user_site()
    missing = []
    for module, (distribution, requirement, minimum, maximum) in REQUIRED.items():
        if importlib.util.find_spec(module) is None or not _version_ok(distribution, minimum, maximum):
            missing.append(requirement)
    return missing


def in_virtual_environment() -> bool:
    return sys.prefix != getattr(sys, "base_prefix", sys.prefix) or bool(os.environ.get("VIRTUAL_ENV") and Path(sys.prefix) == Path(os.environ["VIRTUAL_ENV"]))


def externally_managed() -> bool:
    if in_virtual_environment():
        return False
    try:
        import sysconfig

        return (Path(sysconfig.get_path("stdlib")) / "EXTERNALLY-MANAGED").is_file()
    except Exception:
        return False


def install_command(requirements: list[str]) -> list[str]:
    command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input"]
    if not in_virtual_environment():
        command.append("--user")
    return [*command, *requirements]


def manual_install_hint() -> str:
    if in_virtual_environment():
        return f'"{sys.executable}" -m pip install . (run it from the VideoHaul folder)'
    if os.name == "nt":
        return "py -m venv .venv, then .venv\\Scripts\\python -m pip install ., then .venv\\Scripts\\python VideoHaul.py"
    return "python3 -m venv .venv && .venv/bin/python -m pip install . && .venv/bin/python VideoHaul.py"


def ask_before_installing(missing: list[str]) -> bool:
    if os.environ.get("VIDEOHAUL_AUTO_INSTALL", "").strip() == "1":
        return True
    target = "this virtual environment" if in_virtual_environment() else "your user Python packages"
    question = "VideoHaul needs these Python packages: " + ", ".join(missing) + f". Install them into {target} now?"
    if os.name == "nt":
        try:
            import ctypes

            return ctypes.windll.user32.MessageBoxW(None, question, "VideoHaul setup", 0x24) == 6
        except Exception:
            pass
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            answer = input(question + " [y/N] ").strip().casefold()
            return answer in {"y", "yes"}
    except (EOFError, OSError):
        return False
    return False


def ensure_python_runtime(confirm=None) -> list[str]:
    _activate_user_site()
    ensure_current_path(_user_scripts())
    missing = missing_packages()
    if not missing:
        return []
    if externally_managed():
        raise RuntimeError(
            "VideoHaul needs " + ", ".join(missing) + ". This Python is managed by your operating system, "
            "so VideoHaul will not install packages into it. Create a virtual environment instead: " + manual_install_hint()
        )
    approve = confirm or ask_before_installing
    if not approve(missing):
        raise RuntimeError("VideoHaul needs " + ", ".join(missing) + ". Install them with: " + manual_install_hint())
    flags = 0x08000000 if os.name == "nt" else 0
    subprocess.run(install_command(missing), check=True, creationflags=flags)
    _activate_user_site()
    importlib.invalidate_caches()
    remaining = missing_packages()
    if remaining:
        raise RuntimeError("VideoHaul could not prepare required Python packages: " + ", ".join(remaining))
    return missing
