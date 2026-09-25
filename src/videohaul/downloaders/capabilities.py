from __future__ import annotations

import re
import subprocess
import threading

from ..unicode_text import decode_external_bytes

OPTION_PATTERN = re.compile(r"(?<![\w-])(--[a-z0-9][a-z0-9-]*)")

REQUIRED_PROGRESS_OPTIONS = ("--progress", "--no-quiet", "--newline", "--progress-template")

PROBE_TIMEOUT_SECONDS = 25.0


class ToolCapabilities:
    def __init__(self, executable: str, options: set[str], version: str, probed: bool, detail: str = ""):
        self.executable = str(executable or "")
        self.options = set(options)
        self.version = str(version or "")
        self.probed = bool(probed)
        self.detail = str(detail or "")

    def supports(self, option: str) -> bool:
        if not self.probed:
            return True
        return str(option) in self.options

    def filter(self, args: list[str]) -> tuple[list[str], list[str]]:
        kept: list[str] = []
        dropped: list[str] = []
        index = 0
        values = list(args)
        while index < len(values):
            token = values[index]
            if isinstance(token, str) and token.startswith("--") and not self.supports(token):
                dropped.append(token)
                index += 1
                while index < len(values) and not str(values[index]).startswith("-"):
                    index += 1
                continue
            kept.append(token)
            index += 1
        return kept, dropped

    def missing_progress_options(self) -> list[str]:
        if not self.probed:
            return []
        return [name for name in REQUIRED_PROGRESS_OPTIONS if name not in self.options]

    def public_dict(self) -> dict:
        return {
            "executable": self.executable,
            "version": self.version,
            "probed": self.probed,
            "option_count": len(self.options),
            "detail": self.detail,
            "missing_progress_options": self.missing_progress_options(),
        }


_LOCK = threading.RLock()
_CACHE: dict[str, ToolCapabilities] = {}


def _run(args: list[str]) -> str:
    completed = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    return decode_external_bytes(completed.stdout or b"")


def probe_capabilities(executable: str, runner=None) -> ToolCapabilities:
    path = str(executable or "")
    if not path:
        return ToolCapabilities("", set(), "", False, "No yt-dlp executable was resolved")
    with _LOCK:
        cached = _CACHE.get(path)
    if cached is not None:
        return cached
    execute = runner or _run
    version = ""
    try:
        version = execute([path, "--version"]).strip().splitlines()[0].strip()
    except Exception:
        version = ""
    try:
        help_text = execute([path, "--help"])
    except Exception as exc:
        value = ToolCapabilities(path, set(), version, False, f"yt-dlp option probe failed: {exc}")
        with _LOCK:
            _CACHE[path] = value
        return value
    options = set(OPTION_PATTERN.findall(help_text or ""))
    if len(options) < 40:
        value = ToolCapabilities(path, options, version, False, "yt-dlp help output was not recognisable")
    else:
        value = ToolCapabilities(path, options, version, True, "yt-dlp options probed")
    with _LOCK:
        _CACHE[path] = value
    return value


def reset_capabilities_cache() -> None:
    with _LOCK:
        _CACHE.clear()
