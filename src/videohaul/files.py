from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .filesystem import organized_destination
from .models import DownloadJob


def reveal_supported() -> bool:
    return os.name == "nt" or sys.platform == "darwin"


def job_destination(job: DownloadJob) -> Path | None:
    configured = str(job.settings.destination or "").strip()
    if job.output_path:
        parent = Path(job.output_path).parent
        if str(parent) and str(parent) != ".":
            return parent
    if not configured:
        return None
    try:
        return organized_destination(job)
    except Exception:
        return Path(configured)


def destination_info(job: DownloadJob) -> dict:
    configured = str(job.settings.destination or "").strip()
    target = job_destination(job)
    if target is None:
        return {
            "job_id": job.job_id,
            "destination": "",
            "configured_destination": configured,
            "available": False,
            "exists": False,
            "writable": False,
            "reason": "No destination folder is configured for this download",
            "output_path": str(job.output_path or ""),
            "output_exists": False,
            "reveal_supported": reveal_supported(),
        }
    exists = False
    writable = False
    reason = ""
    try:
        exists = target.is_dir()
    except Exception as exc:
        reason = f"Destination could not be inspected: {exc}"
    if exists:
        writable = os.access(str(target), os.W_OK)
        if not writable:
            reason = f"Destination is not writable: {target}"
    elif not reason:
        parent_exists = False
        try:
            parent_exists = target.parent.is_dir()
        except Exception:
            parent_exists = False
        reason = (
            f"Destination folder does not exist yet: {target}"
            if parent_exists
            else f"Destination is unavailable or the drive is disconnected: {target}"
        )
    output_exists = False
    if job.output_path:
        try:
            output_exists = Path(job.output_path).is_file()
        except Exception:
            output_exists = False
    return {
        "job_id": job.job_id,
        "destination": str(target),
        "configured_destination": configured,
        "available": bool(exists),
        "exists": bool(exists),
        "writable": bool(writable),
        "reason": reason,
        "output_path": str(job.output_path or ""),
        "output_exists": output_exists,
        "reveal_supported": reveal_supported(),
    }


def open_directory(path: str | Path, launcher=None) -> bool:
    target = Path(str(path))
    if not target.is_dir():
        return False
    return _launch(target, launcher, reveal=False)


def reveal_file(path: str | Path, launcher=None) -> bool:
    target = Path(str(path))
    if not target.is_file():
        return False
    if os.name == "nt":
        run = launcher or subprocess.Popen
        run(["explorer", f"/select,{target}"])
        return True
    if sys.platform == "darwin":
        run = launcher or subprocess.Popen
        run(["open", "-R", str(target)])
        return True
    return open_directory(target.parent, launcher)


def _launch(target: Path, launcher=None, reveal: bool = False) -> bool:
    if launcher is not None:
        launcher([str(target)])
        return True
    if os.name == "nt":
        os.startfile(str(target))
        return True
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
        return True
    subprocess.Popen(["xdg-open", str(target)])
    return True
