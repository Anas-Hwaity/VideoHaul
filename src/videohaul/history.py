from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import time
from typing import Any
from uuid import uuid4

from .media_candidates import redact_url, url_carries_secrets
from .models import DownloadJob, JobSettings


@dataclass(slots=True)
class HistoryEntry:
    history_id: str
    completed_at: float
    title: str
    source_url: str
    canonical_identity: str
    platform: str
    creator: str
    output_path: str
    settings: dict[str, Any]
    canonical_url: str = ""
    platform_media_id: str = ""


def _history_url(value: str) -> str:
    text = str(value or "")
    return redact_url(text) if url_carries_secrets(text) else text


def canonical_media_identity(job: DownloadJob) -> str:
    analysis = job.analysis
    if analysis:
        platform = str(analysis.platform or "").strip().casefold()
        media_id = _history_url(str(analysis.platform_media_id or "").strip())
        if platform and media_id:
            return f"{platform}:{media_id}"
        canonical = str(analysis.canonical_url or "").strip()
        if canonical:
            return f"url:{_history_url(canonical).casefold()}"
    source = str(job.source_url or "").strip()
    return f"url:{_history_url(source).casefold()}" if source else f"job:{job.job_id}"


def history_from_job(job: DownloadJob) -> HistoryEntry:
    analysis = job.analysis
    return HistoryEntry(
        history_id=uuid4().hex,
        completed_at=float(job.finished_at or time.time()),
        title=str(analysis.title if analysis else Path(job.output_path).stem or "media"),
        source_url=_history_url(job.source_url),
        canonical_identity=canonical_media_identity(job),
        platform=str(analysis.platform if analysis else ""),
        creator=str(analysis.creator if analysis else ""),
        output_path=str(job.output_path or ""),
        settings=asdict(job.settings),
        canonical_url=_history_url(analysis.canonical_url if analysis else ""),
        platform_media_id=_history_url(analysis.platform_media_id if analysis else ""),
    )


def history_to_dict(entry: HistoryEntry) -> dict[str, Any]:
    return asdict(entry)


def _history_identity(value: str) -> str:
    text = str(value or "")
    if text.startswith("url:"):
        return "url:" + _history_url(text[4:]).casefold()
    prefix, separator, remainder = text.partition(":")
    if separator and remainder and url_carries_secrets(remainder):
        return prefix + ":" + _history_url(remainder)
    return text


def sanitize_history_dict(value: dict[str, Any]) -> dict[str, Any] | None:
    data = dict(value or {}) if isinstance(value, dict) else {}
    history_id = str(data.get("history_id") or "").strip()
    if not history_id:
        return None
    try:
        completed_at = max(0.0, float(data.get("completed_at") or 0.0))
    except Exception:
        completed_at = 0.0
    settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
    return {
        "history_id": history_id,
        "completed_at": completed_at,
        "title": str(data.get("title") or "media"),
        "source_url": _history_url(str(data.get("source_url") or "")),
        "canonical_identity": _history_identity(str(data.get("canonical_identity") or "")),
        "platform": str(data.get("platform") or ""),
        "creator": str(data.get("creator") or ""),
        "output_path": str(data.get("output_path") or ""),
        "settings": dict(settings),
        "canonical_url": _history_url(str(data.get("canonical_url") or "")),
        "platform_media_id": _history_url(str(data.get("platform_media_id") or "")),
    }


def history_from_dict(value: dict[str, Any]) -> HistoryEntry:
    sanitized = sanitize_history_dict(value)
    if sanitized is None:
        raise ValueError("History entry has no identity")
    return HistoryEntry(**sanitized)


def settings_from_history(entry: HistoryEntry) -> JobSettings:
    allowed = JobSettings.__dataclass_fields__
    values = {key: item for key, item in dict(entry.settings or {}).items() if key in allowed}
    return JobSettings(**values).normalized()
