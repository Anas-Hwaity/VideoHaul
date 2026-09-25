from __future__ import annotations

import json
import os
import importlib.metadata
import re
import importlib.util
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .browser import ensure_browser, find_playwright_browser, playwright_browser_present, valid_browser
from .dependencies import (
    MANIFEST_PATH,
    _read_manifest,
    _run,
    _valid,
    _version_tuple,
    _write_manifest,
    ensure_deno,
    ensure_ffmpeg,
    ensure_ytdlp,
)
from .paths import DATA_ROOT
from .preview import vlc_status

APPLICATION_VERSION = "0.1.0"
PROJECT_REPOSITORY = "Anas-Hwaity/VideoHaul"
UPDATE_STATE_PATH = DATA_ROOT / "updates.json"
UPDATE_TIMEOUT_SECONDS = 10.0
IS_WINDOWS = os.name == "nt"

TOOL_PROBES = {
    "yt_dlp": ["--version"],
    "ffmpeg": ["-version"],
    "deno": ["--version"],
}

TOOL_INSTALLERS = {
    "yt_dlp": lambda: ensure_ytdlp(force=True),
    "ffmpeg": lambda: ensure_ffmpeg(force=True),
    "deno": lambda: ensure_deno(force=True),
    "chromium": lambda: ensure_browser("playwright", force=True),
}


def _streamlink_status() -> dict:
    spec = importlib.util.find_spec("streamlink")
    path = str(spec.origin or "") if spec is not None else ""
    try:
        version = importlib.metadata.version("streamlink")
    except Exception:
        version = ""
    healthy = bool(spec is not None and _version_tuple(version) >= (7, 0) and _version_tuple(version) < (8, 0))
    return {
        "name": "streamlink",
        "path": path,
        "recorded_path": path,
        "source": "python-runtime" if spec is not None else "",
        "present": spec is not None,
        "healthy": healthy,
        "version": version,
        "detail": "" if healthy else "Streamlink 7.x is required",
        "rollback_available": False,
    }


def _update_streamlink() -> str:
    flags = 0x08000000 if IS_WINDOWS else 0
    from .bootstrap import externally_managed, install_command

    if externally_managed():
        raise RuntimeError("This Python is managed by your operating system; update Streamlink with your package manager or run VideoHaul from a virtual environment")
    command = [*install_command(["streamlink>=7,<8"]), "--upgrade"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600, creationflags=flags)
    if int(result.returncode or 0) != 0:
        detail = str(result.stderr or result.stdout or "Streamlink update failed").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "Streamlink update failed")
    importlib.invalidate_caches()
    status = _streamlink_status()
    if not status["healthy"]:
        raise RuntimeError(status["detail"])
    return status["path"]


TOOL_INSTALLERS["streamlink"] = _update_streamlink


def _executable_for(name: str, entry: dict) -> str:
    path = str(entry.get("path") or "")
    if name == "ffmpeg" and path and Path(path).is_dir():
        binary = "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"
        return str(Path(path) / binary)
    return path


def _probe_version(name: str, executable: str) -> str:
    args = TOOL_PROBES.get(name)
    if not args or not executable:
        return ""
    try:
        result = _run([str(executable), *args], timeout=20.0)
    except Exception:
        return ""
    if int(result.returncode or 0) != 0:
        return ""
    text = str(result.stdout or result.stderr or "").strip()
    return text.splitlines()[0][:200] if text else ""


def dependency_health(manifest: dict | None = None) -> dict:
    known = set(TOOL_PROBES) | set(TOOL_INSTALLERS)
    raw_values = dict(manifest if manifest is not None else _read_manifest())
    values = {name: entry for name, entry in raw_values.items() if name in known}
    tools = []
    for name in sorted(known):
        if name == "streamlink":
            tools.append(_streamlink_status())
            continue
        if name == "chromium":
            continue
        entry = dict(values.get(name) or {})
        executable = _executable_for(name, entry)
        args = TOOL_PROBES.get(name)
        exists = bool(executable) and Path(executable).exists()
        healthy = bool(exists and args and _valid(executable, args))
        source = str(entry.get("source") or "")
        if not healthy and name in {"yt_dlp", "ffmpeg", "deno"}:
            command = "yt-dlp" if name == "yt_dlp" else name
            candidate = shutil.which(command)
            if candidate and args and _valid(candidate, args):
                if name != "ffmpeg" or (shutil.which("ffprobe") and _valid(shutil.which("ffprobe"), ["-version"])):
                    executable = candidate
                    exists = True
                    healthy = True
                    source = "system"
        tools.append(
            {
                "name": name,
                "path": executable,
                "recorded_path": str(entry.get("path") or ""),
                "source": source,
                "present": exists,
                "healthy": healthy,
                "version": _probe_version(name, executable) if healthy else "",
                "detail": "" if healthy else (("Optional: needed by some sites such as YouTube. " if name == "deno" else "") + ("Recorded location is missing" if not exists else "Recorded tool failed validation")),
                "optional": name == "deno",
                "rollback_available": bool(entry.get("previous_path")),
            }
        )
    from .media_detection import playwright_available

    automation = playwright_available()
    browser = find_playwright_browser() if automation else None
    browser_present = bool(browser)
    browser_healthy = bool(automation and browser_present and valid_browser(browser))
    if not automation:
        browser_detail = "Playwright automation runtime is not installed"
    elif not browser_present:
        browser_detail = "Managed Playwright Chromium has not been provisioned yet"
    else:
        browser_detail = ""
    tools.append(
        {
            "name": "chromium",
            "path": str(browser or ""),
            "recorded_path": str(browser or ""),
            "source": "playwright-chromium" if browser_present else "",
            "present": browser_present,
            "healthy": browser_healthy,
            "automation_runtime": automation,
            "version": "",
            "detail": browser_detail,
            "rollback_available": False,
        }
    )
    vlc = vlc_status()
    tools.append(
        {
            "name": "vlc",
            "path": vlc["executable"],
            "recorded_path": vlc["executable"],
            "source": "existing-install" if vlc["available"] else "",
            "present": vlc["available"],
            "healthy": vlc["available"],
            "version": "",
            "detail": vlc["detail"],
            "optional": True,
            "rollback_available": False,
        }
    )
    unhealthy = [item["name"] for item in tools if not item["healthy"] and not item.get("optional")]
    return {
        "tools": tools,
        "healthy": not unhealthy,
        "repair_required": unhealthy,
        "manifest_path": str(MANIFEST_PATH),
        "checked_at": time.time(),
    }


def update_dependency(name: str, installer=None) -> dict:
    key = str(name)
    if key not in TOOL_INSTALLERS:
        return {"name": key, "state": "unsupported", "detail": f"{key} is not a managed dependency"}
    manifest = _read_manifest()
    entry = dict(manifest.get(key) or {})
    if str(entry.get("source") or "").casefold() == "system":
        return {"name": key, "state": "manual", "detail": f"{key} is provided by the system and must be updated with its package manager", "path": _executable_for(key, entry), "rollback_available": False}
    previous = _executable_for(key, entry)
    backup = _backup_dependency(key, entry)
    install = installer or TOOL_INSTALLERS[key]
    try:
        updated = install()
    except Exception as exc:
        detail = str(exc)
        rollback_available = False
        if backup:
            try:
                _restore_backup(key, entry, backup)
                detail = f"{detail}; previous version restored"
                _remove_backup(backup)
            except Exception as restore_exc:
                detail = f"{detail}; rollback failed: {restore_exc}"
                if Path(backup).exists():
                    manifest = _read_manifest()
                    record = dict(manifest.get(key) or entry)
                    record["previous_path"] = backup
                    manifest[key] = record
                    _write_manifest(manifest)
                    rollback_available = True
        return {"name": key, "state": "failed", "detail": detail, "path": previous, "rollback_available": rollback_available}
    manifest = _read_manifest()
    record = dict(manifest.get(key) or {})
    if backup:
        record["previous_path"] = backup
        manifest[key] = record
        _write_manifest(manifest)
    if key == "streamlink":
        status = _streamlink_status()
        return {"name": key, "state": "updated" if status["healthy"] else "failed", "path": status["path"], "version": status["version"], "rollback_available": False, "detail": status["detail"]}
    if key == "chromium":
        healthy = playwright_browser_present(updated) and valid_browser(updated)
        return {"name": key, "state": "updated" if healthy else "failed", "path": str(updated), "version": "", "rollback_available": False, "detail": "" if healthy else "Managed Playwright Chromium failed validation"}
    args = TOOL_PROBES.get(key)
    executable = _executable_for(key, record) or str(updated)
    healthy = bool(args and _valid(executable, args))
    if not healthy:
        rollback = rollback_dependency(key)
        return {"name": key, "state": "rolled_back", "detail": "Updated dependency failed validation and the previous version was restored", "rollback": rollback}
    return {"name": key, "state": "updated", "path": str(updated), "version": _probe_version(key, executable), "rollback_available": bool(backup)}


def rollback_dependency(name: str) -> dict:
    key = str(name)
    manifest = _read_manifest()
    entry = dict(manifest.get(key) or {})
    backup = str(entry.get("previous_path") or "")
    if not backup or not Path(backup).exists():
        return {"name": key, "state": "unavailable", "detail": "No previous version is stored for this dependency"}
    try:
        target = _restore_backup(key, entry, backup)
    except Exception as exc:
        return {"name": key, "state": "failed", "detail": str(exc)}
    _remove_backup(backup)
    entry.pop("previous_path", None)
    manifest[key] = entry
    _write_manifest(manifest)
    return {"name": key, "state": "rolled_back", "path": target}


def _restore_backup(name: str, entry: dict, backup: str) -> str:
    target = str(entry.get("path") or "") if name == "ffmpeg" and Path(str(entry.get("path") or "")).is_dir() else _executable_for(name, entry)
    if not target:
        raise RuntimeError("No recorded location to restore into")
    source = Path(backup)
    destination = Path(target)
    try:
        source.resolve().relative_to(DATA_ROOT.resolve())
        destination.resolve().relative_to(DATA_ROOT.resolve())
    except ValueError as exc:
        raise RuntimeError("Refusing to restore a dependency outside VideoHaul data") from exc
    if source.is_symlink() or destination.is_symlink():
        raise RuntimeError("Refusing to restore a dependency through a symbolic link")
    if name == "ffmpeg" and source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        binaries = ("ffmpeg.exe" if IS_WINDOWS else "ffmpeg", "ffprobe.exe" if IS_WINDOWS else "ffprobe")
        if not all((source / binary).is_file() for binary in binaries):
            raise RuntimeError("The FFmpeg rollback backup is incomplete")
        for binary in binaries:
            shutil.copy2(source / binary, destination / binary)
    else:
        if not source.is_file():
            raise RuntimeError("The dependency rollback backup is unavailable")
        shutil.copy2(source, target)
    return target


def _remove_backup(path: str) -> None:
    target = Path(str(path or ""))
    if not target.exists():
        return
    try:
        target.resolve().relative_to(DATA_ROOT.resolve())
    except ValueError as exc:
        raise RuntimeError("Refusing to remove a dependency backup outside VideoHaul data") from exc
    if not target.name.endswith(".previous"):
        raise RuntimeError("Refusing to remove an unrecognized dependency backup")
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    else:
        target.unlink(missing_ok=True)


def _backup_dependency(name: str, entry: dict) -> str:
    recorded = Path(str(entry.get("path") or ""))
    if name == "ffmpeg" and recorded.is_dir():
        backup = recorded.with_name(recorded.name + ".previous")
        binaries = ("ffmpeg.exe" if IS_WINDOWS else "ffmpeg", "ffprobe.exe" if IS_WINDOWS else "ffprobe")
        if not all((recorded / binary).is_file() for binary in binaries):
            return ""
        try:
            if backup.exists():
                _remove_backup(str(backup))
            backup.mkdir(parents=True)
            for binary in binaries:
                shutil.copy2(recorded / binary, backup / binary)
            return str(backup)
        except Exception:
            shutil.rmtree(backup, ignore_errors=True)
            return ""
    previous = _executable_for(name, entry)
    if not previous or not Path(previous).is_file():
        return ""
    backup = Path(previous).with_suffix(Path(previous).suffix + ".previous")
    try:
        shutil.copy2(previous, backup)
        return str(backup)
    except Exception:
        return ""


def _read_update_state() -> dict:
    try:
        return json.loads(UPDATE_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_update_state(value: dict) -> None:
    try:
        UPDATE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        temporary = UPDATE_STATE_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
        os.replace(temporary, UPDATE_STATE_PATH)
    except Exception:
        return


_VERSION_PATTERN = re.compile(
    r"^v?(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-_.]?(?P<pre>a|b|c|rc|alpha|beta|pre|preview)[-_.]?(?P<pre_number>\d*))?"
    r"(?:[-_.]?(?:post|rev|r)[-_.]?(?P<post>\d*))?"
    r"(?:[-_.]?dev[-_.]?(?P<dev>\d*))?$",
    re.IGNORECASE,
)
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}


def version_key(value: str) -> tuple | None:
    match = _VERSION_PATTERN.match(str(value or "").strip())
    if not match:
        return None
    release = [int(part) for part in match.group("release").split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    pre = match.group("pre")
    dev = match.group("dev")
    post = match.group("post")
    if pre:
        stage = (1, _PRE_RANK[pre.lower()], int(match.group("pre_number") or 0))
    elif dev is not None and post is None:
        stage = (0, 0, 0)
    else:
        stage = (2, 0, 0)
    post_key = int(post or 0) if post is not None else -1
    dev_key = int(dev or 0) if dev is not None else float("inf")
    return (tuple(release), stage, post_key, dev_key)


def compare_versions(current: str, available: str) -> bool:
    right = version_key(available)
    if right is None:
        return False
    left = version_key(current)
    if left is None:
        return True
    return right > left


def default_update_source() -> str:
    repository = PROJECT_REPOSITORY.strip().strip("/")
    if repository.count("/") != 1:
        return ""
    return f"https://api.github.com/repos/{repository}/releases/latest"


def check_application_update(current_version: str = APPLICATION_VERSION, enabled: bool = True, fetcher=None, source_url: str = "") -> dict:
    source_url = str(source_url or "").strip() or default_update_source()
    if not enabled:
        return {"enabled": False, "state": "disabled", "current_version": current_version, "available_version": "", "update_available": False, "detail": "Update checks are disabled"}
    if fetcher is None and not source_url:
        return {
            "enabled": True,
            "state": "not_configured",
            "current_version": current_version,
            "available_version": "",
            "update_available": False,
            "detail": "No update source is configured for this build",
        }
    try:
        if fetcher is not None:
            payload = fetcher()
        else:
            request = urllib.request.Request(source_url, headers={"User-Agent": f"VideoHaul/{current_version}"})
            with urllib.request.urlopen(request, timeout=UPDATE_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        state = {"enabled": True, "state": "unreachable", "current_version": current_version, "available_version": "", "update_available": False, "detail": str(exc)}
        _write_update_state({"checked_at": time.time(), "result": state})
        return state
    except Exception as exc:
        state = {"enabled": True, "state": "unreachable", "current_version": current_version, "available_version": "", "update_available": False, "detail": str(exc)}
        _write_update_state({"checked_at": time.time(), "result": state})
        return state
    available = str((payload or {}).get("version") or (payload or {}).get("tag_name") or "").lstrip("vV")
    result = {
        "enabled": True,
        "state": "checked",
        "current_version": current_version,
        "available_version": available,
        "update_available": compare_versions(current_version, available),
        "release_url": _safe_release_url((payload or {}).get("html_url") or (payload or {}).get("url") or ""),
        "detail": "",
    }
    _write_update_state({"checked_at": time.time(), "result": result})
    return result


def _safe_release_url(value) -> str:
    text = str(value or "").strip()
    return text if text.startswith("https://") else ""


def last_update_check() -> dict:
    return _read_update_state()
