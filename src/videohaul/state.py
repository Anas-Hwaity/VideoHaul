from __future__ import annotations

import threading
import time
from collections.abc import Callable

from .diagnostics import diagnostic_entry
from .history import canonical_media_identity, history_from_dict, history_from_job, history_to_dict, sanitize_history_dict, settings_from_history
from .media_candidates import redact_url, url_carries_secrets
from .models import DownloadJob, JobSettings, JobStatus, ProgressState, Stage
from .persistence import Store
from .serialization import job_from_dict, job_to_dict
from .settings import AppSettings


class VideoHaulState:
    def __init__(self, store: Store):
        self.store = store
        self._lock = threading.RLock()
        self._listeners: list[Callable[[dict], None]] = []
        self._persist_failure = ""
        self._progress_persisted_at: dict[str, float] = {}
        stored_settings = dict(store.get_json("settings", {}) or {})
        migrations = dict(store.get_json("settings_migrations", {}) or {})
        migrate_unlimited_bandwidth = not migrations.get("v0_1_unlimited_bandwidth_default")
        if migrate_unlimited_bandwidth:
            stored_settings["global_speed_limit_bps"] = None
        self.settings = AppSettings.from_dict(stored_settings)
        store.set_json("settings", self.settings.to_dict())
        if migrate_unlimited_bandwidth:
            migrations["v0_1_unlimited_bandwidth_default"] = True
        if not migrations.get("v0_1_private_history_cleanup"):
            repaired_history = 0
            for raw_history in store.load_history(10000):
                sanitized = sanitize_history_dict(raw_history)
                if sanitized is not None and sanitized != raw_history:
                    store.add_history(sanitized)
                    repaired_history += 1
            migrations["v0_1_private_history_cleanup"] = True
            if repaired_history:
                store.add_diagnostic(diagnostic_entry("history_privacy_migrated", "Stored history was sanitized for private URL data", {"entries": repaired_history}))
        store.set_json("settings_migrations", migrations)
        self.jobs = []
        quarantined = 0
        for item in store.load_jobs():
            try:
                self.jobs.append(job_from_dict(item))
            except Exception as exc:
                quarantined += 1
                store.add_diagnostic(diagnostic_entry("persisted_job_quarantined", "A malformed persisted job was removed so VideoHaul could start", {"error": str(exc)}, "error"))
        self._repair_loaded_jobs()
        if quarantined:
            self._persist_jobs()
        self.record_diagnostic("application_started", "VideoHaul state initialized", {"jobs": len(self.jobs), "quarantined_jobs": quarantined})

    def _repair_loaded_jobs(self) -> None:
        changed = False
        for job in self.jobs:
            if job.status == JobStatus.DONE:
                if job.transfer_headers:
                    job.transfer_headers = {}
                    changed = True
                for attribute in ("source_url", "thumbnail_source_url"):
                    raw = str(getattr(job, attribute, "") or "")
                    if url_carries_secrets(raw):
                        setattr(job, attribute, redact_url(raw))
                        changed = True
                analysis = job.analysis
                if analysis is not None:
                    for attribute in ("source_url", "canonical_url", "thumbnail_url", "platform_media_id"):
                        raw = str(getattr(analysis, attribute, "") or "")
                        if url_carries_secrets(raw):
                            setattr(analysis, attribute, redact_url(raw))
                            changed = True
                    private_keys = [key for key in analysis.metadata if str(key).startswith("_")]
                    if private_keys:
                        analysis.metadata = {key: item for key, item in analysis.metadata.items() if not str(key).startswith("_")}
                        changed = True
                    for stream in [*analysis.video_streams, *analysis.audio_streams]:
                        if stream.direct_stream_url:
                            stream.direct_stream_url = ""
                            changed = True
            if job.effective_speed_limit_bps is not None:
                job.effective_speed_limit_bps = None
                changed = True
            if job.status in {JobStatus.DOWNLOADING, JobStatus.ANALYZING, JobStatus.FINALIZING}:
                job.status = JobStatus.PAUSED
                current = job.progress
                job.progress = ProgressState(
                    stage=Stage.PAUSED,
                    determinate=current.determinate,
                    fraction=current.fraction,
                    downloaded_bytes=current.downloaded_bytes,
                    total_bytes=current.total_bytes,
                    total_is_estimate=current.total_is_estimate,
                    speed_bps=0.0,
                    eta_seconds=None,
                    elapsed_seconds=current.elapsed_seconds,
                    terminal=False,
                    message="Restored after restart",
                )
                if job.partial_path:
                    job.resume_state = "supported" if job.resume_state == "supported" else "uncertain"
                else:
                    job.resume_state = "restart_required"
                changed = True
        self._reindex()
        if changed:
            self._persist_jobs()

    def _reindex(self) -> None:
        for index, job in enumerate(self.jobs):
            job.queue_position = index

    def subscribe(self, listener: Callable[[dict], None]) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)
        def unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)
        return unsubscribe

    def record_diagnostic(self, kind: str, message: str = "", payload: dict | None = None, level: str = "info") -> None:
        self.store.add_diagnostic(diagnostic_entry(kind, message, payload, level))

    def diagnostics(self, limit: int = 1000, scope: str = "session") -> list[dict]:
        if str(scope or "session").casefold() == "all":
            return self.store.load_diagnostics(limit)
        return self.store.load_diagnostics(limit, self.store.session_id)

    def diagnostics_scope_info(self) -> dict:
        return {
            "session_id": self.store.session_id,
            "session_started_at": self.store.session_started_at,
            "earlier_session_events": self.store.diagnostic_session_count(),
        }

    def clear_diagnostics(self, scope: str = "session") -> None:
        everything = str(scope or "session").casefold() == "all"
        self.store.clear_diagnostics(None if everything else self.store.session_id)
        self.record_diagnostic(
            "diagnostics_cleared",
            "All diagnostic history cleared" if everything else "Diagnostics cleared for this session",
            {"scope": "all" if everything else "session"},
        )

    def add_history_for_job(self, job_id: str) -> dict | None:
        with self._lock:
            job = self.get_job(job_id)
            if not self.settings.history_enabled or job.settings.incognito or not job.output_path:
                return None
            entry = history_from_job(job)
            payload = history_to_dict(entry)
            self.store.add_history(payload)
            self._emit("history_added", {"job_id": job_id, "history_id": entry.history_id, "canonical_identity": entry.canonical_identity})
            return payload

    def history(self, query: str = "", limit: int = 1000) -> list[dict]:
        text = str(query or "").strip().casefold()
        values = self.store.load_history(limit)
        if not text:
            return values
        result = []
        for item in values:
            haystack = " ".join(str(item.get(key) or "") for key in ("title", "platform", "source_url", "output_path", "creator", "completed_at")).casefold()
            if text in haystack:
                result.append(item)
        return result

    def history_entry(self, history_id: str) -> dict:
        for item in self.store.load_history(10000):
            if str(item.get("history_id")) == str(history_id):
                return item
        raise KeyError(history_id)

    def delete_history(self, history_id: str) -> bool:
        deleted = self.store.delete_history(history_id)
        if deleted:
            self._emit("history_deleted", {"history_id": str(history_id)})
        return deleted

    def clear_history(self) -> None:
        self.store.clear_history()
        self._emit("history_cleared", {})

    def redownload_history(self, history_id: str) -> DownloadJob:
        entry = history_from_dict(self.history_entry(history_id))
        job = self.add_job(entry.source_url, settings_from_history(entry))
        self._emit("history_redownload", {"history_id": history_id, "job_id": job.job_id})
        return job

    def duplicate_history_for_job(self, job_id: str) -> list[dict]:
        job = self.get_job(job_id)
        identity = canonical_media_identity(job)
        return self.store.find_history_identity(identity) if identity else []

    def canonical_duplicate_job_ids(self, job_id: str) -> list[str]:
        job = self.get_job(job_id)
        identity = canonical_media_identity(job)
        if not identity or identity.startswith("job:"):
            return []
        matches = []
        for other in self.jobs:
            if other.job_id == job_id or other.analysis is None:
                continue
            if canonical_media_identity(other) == identity:
                matches.append(other.job_id)
        return matches

    def remember_folder(self, path: str) -> None:
        value = str(path or "").strip()
        if not value:
            return
        folders = [value] + [item for item in self.settings.recent_download_folders if item.casefold() != value.casefold()]
        updated = folders[:12]
        if updated == self.settings.recent_download_folders:
            return
        self.settings.recent_download_folders = updated
        self._persist_settings()

    def _emit(self, kind: str, payload: dict | None = None) -> None:
        event = {"kind": kind, "payload": payload or {}, "at": time.time()}
        if kind != "progress":
            self.record_diagnostic(kind, payload=payload or {})
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                continue

    def _persist_failure_result(self, exc: Exception) -> None:
        if self._persist_failure != str(exc):
            self._persist_failure = str(exc)
            try:
                self.store.add_diagnostic(diagnostic_entry("state_persist_failed", "Job state could not be written to disk; the running state is still correct in memory", {"error": str(exc)}, "error"))
            except Exception:
                return

    def _persist_recovered(self) -> None:
        if self._persist_failure:
            self._persist_failure = ""
            self.record_diagnostic("state_persist_recovered", "Job state is being written to disk again")

    def _persist_jobs(self) -> None:
        try:
            self.store.save_jobs([job_to_dict(job, include_private=True) for job in self.jobs])
        except Exception as exc:
            self._persist_failure_result(exc)
            return
        self._persist_recovered()

    def _persist_job(self, job: DownloadJob) -> None:
        try:
            position = next(index for index, item in enumerate(self.jobs) if item.job_id == job.job_id)
            self.store.save_job(job_to_dict(job, include_private=True), position)
        except Exception as exc:
            self._persist_failure_result(exc)
            return
        self._persist_recovered()

    def _persist_settings(self) -> None:
        self.store.set_json("settings", self.settings.to_dict())

    def default_job_settings(self) -> JobSettings:
        return JobSettings(
            destination=self.settings.default_download_folder,
            filename_template=self.settings.default_filename_template,
            container=self.settings.default_container,
            preferred_codec=self.settings.default_preferred_codec,
            preserve_metadata=self.settings.default_preserve_metadata,
            preserve_chapters=self.settings.default_preserve_chapters,
            subtitle_format=self.settings.default_subtitle_format,
            partial_cleanup=self.settings.default_partial_cleanup,
            max_retries=self.settings.default_max_retries,
            retry_backoff_seconds=self.settings.default_retry_backoff_seconds,
            browser_cookies=self.settings.default_browser_cookies,
            keep_original_streams=self.settings.default_keep_original_streams,
            notify_complete=self.settings.notifications,
            sound_complete=self.settings.completion_sound,
            completion_action=self.settings.default_completion_action,
            completion_move_destination=self.settings.default_completion_move_destination,
            completion_command=self.settings.default_completion_command,
            organization_rule=self.settings.default_organization_rule,
            embed_thumbnail=self.settings.embed_source_thumbnail_default,
            tag_download_comment=self.settings.tag_download_comment_default,
            incognito=not self.settings.history_enabled,
        )

    def add_job(self, url: str = "", settings: JobSettings | None = None) -> DownloadJob:
        with self._lock:
            job = DownloadJob(
                source_url=str(url or "").strip(),
                settings=settings or self.default_job_settings(),
                created_at=time.time(),
            )
            self.jobs.append(job)
            self._reindex()
            self._persist_job(job)
            self._emit("job_added", {"job_id": job.job_id})
            return job

    def get_job(self, job_id: str) -> DownloadJob:
        with self._lock:
            for job in self.jobs:
                if job.job_id == job_id:
                    return job
        raise KeyError(job_id)

    def delete_job(self, job_id: str) -> None:
        with self._lock:
            job = self.get_job(job_id)
            if job.status in {JobStatus.DOWNLOADING, JobStatus.ANALYZING, JobStatus.FINALIZING}:
                raise RuntimeError("Stop the active job before removing it")
            self.jobs = [item for item in self.jobs if item.job_id != job_id]
            self._progress_persisted_at.pop(job_id, None)
            self._reindex()
            self._persist_jobs()
            self._emit("job_deleted", {"job_id": job_id})

    def update_job(self, job_id: str, **changes) -> DownloadJob:
        with self._lock:
            job = self.get_job(job_id)
            for key, value in changes.items():
                if not hasattr(job, key):
                    raise AttributeError(key)
                setattr(job, key, value)
            self._persist_job(job)
            details = {"job_id": job_id, "fields": sorted(changes)}
            if "status" in changes:
                details["status"] = str(job.status.value if hasattr(job.status, "value") else job.status)
            if "failure_reason" in changes and job.failure_reason:
                details["failure_reason"] = str(job.failure_reason)
            if "output_path" in changes and job.output_path:
                details["output_path"] = str(job.output_path)
            self._emit("job_updated", details)
            return job


    def update_job_if_status(
        self,
        job_id: str,
        changes: dict,
        allowed_statuses: set[JobStatus],
    ) -> bool:
        with self._lock:
            job = self.get_job(job_id)
            if job.status not in allowed_statuses:
                return False
            for key, value in changes.items():
                if not hasattr(job, key):
                    raise AttributeError(key)
                setattr(job, key, value)
            self._persist_job(job)
            self._emit("job_updated", {"job_id": job_id})
            return True

    def set_progress(self, job_id: str, progress: ProgressState, status: JobStatus | None = None) -> DownloadJob:
        with self._lock:
            job = self.get_job(job_id)
            job.progress = progress.normalized()
            if status is not None:
                job.status = status
            self._persist_job(job)
            self._emit("progress", {"job_id": job_id})
            return job

    def set_progress_if_status(
        self,
        job_id: str,
        progress: ProgressState,
        status: JobStatus | None,
        allowed_statuses: set[JobStatus],
    ) -> bool:
        with self._lock:
            job = self.get_job(job_id)
            if job.status not in allowed_statuses:
                return False
            job.progress = progress.normalized()
            if status is not None:
                job.status = status
            self._persist_job(job)
            self._emit("progress", {"job_id": job_id})
            return True

    def set_transfer_progress_if_status(
        self,
        job_id: str,
        progress: ProgressState,
        status: JobStatus | None,
        allowed_statuses: set[JobStatus],
        partial_path: str | None = None,
        resume_state: str | None = None,
        retry_state: dict | None = None,
    ) -> bool:
        with self._lock:
            job = self.get_job(job_id)
            if job.status not in allowed_statuses:
                return False
            previous_stage = job.progress.stage
            previous_partial = job.partial_path
            previous_resume = job.resume_state
            job.progress = progress.normalized()
            if status is not None:
                job.status = status
            if partial_path is not None:
                job.partial_path = str(partial_path)
            if resume_state is not None:
                job.resume_state = str(resume_state)
            if retry_state is not None:
                job.retry_state = dict(retry_state)
            now = time.monotonic()
            last = self._progress_persisted_at.get(job_id)
            durable_change = (
                last is None
                or now - last >= 0.25
                or job.progress.stage != previous_stage
                or job.partial_path != previous_partial
                or job.resume_state != previous_resume
                or retry_state is not None
                or job.progress.terminal
            )
            if durable_change:
                self._persist_job(job)
                self._progress_persisted_at[job_id] = now
            self._emit("progress", {"job_id": job_id})
            return True

    def reorder(self, job_id: str, new_index: int) -> None:
        with self._lock:
            job = self.get_job(job_id)
            self.jobs.remove(job)
            target = max(0, min(int(new_index), len(self.jobs)))
            self.jobs.insert(target, job)
            self._reindex()
            self._persist_jobs()
            self._emit("jobs_reordered", {})

    def move_top(self, job_id: str) -> None:
        self.reorder(job_id, 0)

    def move_bottom(self, job_id: str) -> None:
        self.reorder(job_id, max(0, len(self.jobs) - 1))

    def set_collapsed(self, job_id: str, collapsed: bool) -> None:
        self.update_job(job_id, collapsed=bool(collapsed))

    def collapse_all(self, collapsed: bool) -> None:
        with self._lock:
            for job in self.jobs:
                job.collapsed = bool(collapsed)
            self._persist_jobs()
            self._emit("collapse_all", {"collapsed": bool(collapsed)})

    def update_settings(self, values: dict) -> AppSettings:
        with self._lock:
            current = self.settings.to_dict()
            current.update(dict(values or {}))
            self.settings = AppSettings.from_dict(current)
            self._persist_settings()
            self._emit("settings_updated", {})
            return self.settings

    def set_effective_speed_limits(self, values: dict[str, int | None]) -> bool:
        with self._lock:
            changed = False
            for job in self.jobs:
                updated = values.get(job.job_id)
                if job.effective_speed_limit_bps != updated:
                    job.effective_speed_limit_bps = updated
                    changed = True
            if changed:
                self._emit("bandwidth_updated", {})
            return changed

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "settings": self.settings.to_dict(),
                "jobs": [job_to_dict(job) for job in self.jobs],
            }
