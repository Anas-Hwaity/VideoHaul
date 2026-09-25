from __future__ import annotations

from .models import JobStatus


ACTIVE_STATUSES = {JobStatus.ANALYZING, JobStatus.DOWNLOADING, JobStatus.FINALIZING}


def parse_backend_progress(payload: dict) -> dict:
    downloaded = _number(payload.get("downloaded_bytes"), int) or 0
    total = _number(payload.get("total_bytes"), int) or _number(payload.get("total_bytes_estimate"), int)
    speed = _number(payload.get("speed"), float)
    eta = _number(payload.get("eta"), float)
    elapsed = _number(payload.get("elapsed"), float) or 0.0
    downloaded = max(0, downloaded)
    total = max(0, total) if total is not None else None
    determinate = total is not None and total > 0
    return {
        "downloaded_bytes": downloaded,
        "total_bytes": total,
        "speed_bps": max(0.0, speed) if speed is not None else None,
        "eta_seconds": max(0.0, eta) if eta is not None else None,
        "elapsed_seconds": max(0.0, elapsed),
        "determinate": determinate,
        "fraction": min(0.99, downloaded / total) if determinate else None,
    }


def aggregate_progress(jobs) -> dict:
    active = [job for job in jobs if job.status in ACTIVE_STATUSES]
    speed = sum(float(job.progress.speed_bps or 0.0) for job in active)
    byte_jobs = [job for job in active if job.status in {JobStatus.DOWNLOADING, JobStatus.FINALIZING}]
    determinate = bool(byte_jobs) and all(job.progress.total_bytes is not None and job.progress.total_bytes > 0 for job in byte_jobs)
    downloaded = sum(max(0, int(job.progress.downloaded_bytes or 0)) for job in byte_jobs)
    total = sum(int(job.progress.total_bytes or 0) for job in byte_jobs) if determinate else None
    fraction = min(0.99, downloaded / total) if determinate and total else None
    eta_values = [job.progress.eta_seconds for job in byte_jobs if job.progress.eta_seconds is not None]
    eta = max(eta_values) if determinate and len(eta_values) == len(byte_jobs) and eta_values else None
    return {
        "active_count": len(active),
        "determinate": determinate,
        "fraction": fraction,
        "downloaded_bytes": downloaded,
        "total_bytes": total,
        "speed_bps": speed,
        "eta_seconds": eta,
    }


def _number(value, output_type):
    try:
        return output_type(float(value))
    except Exception:
        return None
