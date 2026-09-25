from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import shutil
import tempfile
from typing import Iterable

from .file_policy import render_output_template, resolve_collision, safe_name
from .formats import combined_filesize
from .models import DownloadJob


@dataclass(slots=True)
class DestinationCheck:
    destination: str
    writable: bool
    free_bytes: int | None
    required_bytes: int | None
    reserved_bytes: int
    enough_space: bool | None
    error: str = ""


def estimated_job_bytes(job: DownloadJob) -> int | None:
    analysis = job.analysis
    if not analysis:
        return None
    if str(job.selected_video or "") == "audio_only":
        selected = [item for item in analysis.audio_streams if item.stream_id in set(job.settings.selected_audio)]
        if not selected and analysis.audio_streams:
            selected = [max(analysis.audio_streams, key=lambda item: (item.bitrate or 0, item.stream_id))]
        values = [item.filesize_bytes for item in selected]
        return sum(int(value) for value in values if value is not None) if values and all(value is not None for value in values) else None
    stream = next((item for item in analysis.video_streams if item.stream_id == job.selected_video), None)
    if stream is None and analysis.video_streams:
        stream = max(analysis.video_streams, key=lambda item: (item.height or 0, item.fps or 0, item.bitrate or 0, item.stream_id))
    if stream is None:
        return None
    value, _ = combined_filesize(stream, analysis.audio_streams)
    return value


def organized_destination(job: DownloadJob) -> Path:
    base = Path(job.settings.destination).expanduser()
    rule = str(getattr(job.settings, "organization_rule", "none") or "none").strip().lower()
    analysis = job.analysis
    if rule == "none" or not analysis:
        return base
    if rule == "creator":
        label = analysis.creator or analysis.channel or "unknown-creator"
    elif rule == "platform":
        label = analysis.platform or "unknown-platform"
    elif rule == "date":
        raw = str(analysis.upload_date or "unknown-date")
        label = raw[:4] if len(raw) >= 4 else raw
    elif rule == "playlist":
        label = str((analysis.playlist or {}).get("title") or "playlist")
    else:
        return base
    return base / safe_name(label)


def check_destination(job: DownloadJob, reserved_bytes: int = 0, create: bool = True) -> DestinationCheck:
    destination = organized_destination(job)
    required = estimated_job_bytes(job)
    reserved = max(0, int(reserved_bytes or 0))
    try:
        if create:
            destination.mkdir(parents=True, exist_ok=True)
        if not destination.is_dir():
            return DestinationCheck(str(destination), False, None, required, reserved, False, "Destination is unavailable")
        fd, probe_name = tempfile.mkstemp(prefix=f".videohaul-write-{os.getpid()}-", dir=destination)
        probe = Path(probe_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(b"1")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
        free = int(shutil.disk_usage(destination).free)
        enough = None if required is None else free >= required + reserved
        error = "" if enough is not False else f"Insufficient disk space: need {required + reserved} bytes, have {free}"
        return DestinationCheck(str(destination), True, free, required, reserved, enough, error)
    except Exception as exc:
        return DestinationCheck(str(destination), False, None, required, reserved, False, str(exc) or "Destination is unavailable")


def reserve_for_jobs(jobs: Iterable[DownloadJob], exclude_job_id: str | None = None) -> int:
    total = 0
    for job in jobs:
        if exclude_job_id and job.job_id == exclude_job_id:
            continue
        if str(getattr(job.status, "value", job.status)) not in {"queued", "ready", "paused"}:
            continue
        value = estimated_job_bytes(job)
        if value is not None:
            total += int(value)
    return total


def prepare_output_path(job: DownloadJob, reserved_bytes: int = 0) -> Path:
    check = check_destination(job, reserved_bytes, True)
    if not check.writable:
        raise RuntimeError(check.error or "Destination is not writable")
    if check.enough_space is False:
        raise RuntimeError(check.error)
    destination = Path(check.destination)
    rendered = render_output_template(job)
    return resolve_collision(destination, rendered, job.settings.collision_policy)
