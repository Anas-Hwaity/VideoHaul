from __future__ import annotations

import threading
import time

from .analysis_cache import AnalysisCache
from .audio import sort_audio_streams
from .media_candidates import redact_url
from .models import JobStatus, ProgressState, Stage
from .quality import AUDIO_ONLY_SELECTOR, BEST_SELECTOR, select_preferred_stream
from .resolvers.common import AuthenticationRequired, MediaUnavailable, MediaUnresolved, ResolverDependencyError, ResolverUnsupported, SourceRefusedRequest
from .resolvers.manager import ResolverManager
from .state import VideoHaulState
from .subtitles import sort_subtitles


class Analyzer:
    def __init__(self, state: VideoHaulState, resolvers: ResolverManager | None = None, cache: AnalysisCache | None = None):
        self.state = state
        self.resolvers = resolvers or ResolverManager()
        self.cache = cache or AnalysisCache()
        self._threads: dict[str, threading.Thread] = {}
        self._cancelled: dict[str, threading.Event] = {}
        self._lock = threading.RLock()

    def analyze(self, job_id: str, refresh: bool = False) -> None:
        job = self.state.get_job(job_id)
        if not job.source_url.strip():
            raise ValueError("Enter a media URL")
        self.state.record_diagnostic("analysis_requested", "Media analysis requested", {"job_id": job_id, "url": redact_url(job.source_url), "refresh": refresh})
        with self._lock:
            existing = self._threads.get(job_id)
            if existing and existing.is_alive() and job.status == JobStatus.ANALYZING:
                self.state.record_diagnostic("analysis_already_running", "Analysis request reused the active worker", {"job_id": job_id})
                return
            if refresh:
                self.cache.invalidate(job.source_url)
            if job.analysis and not refresh and not self.cache.is_stale(job.analysis):
                self.cache.put(job.analysis, job.settings.browser_cookies)
                selection = self._selection(job.selected_video, job.analysis.video_streams, job.analysis.audio_streams)
                self.state.update_job(job_id, status=JobStatus.READY, selected_video=selection)
                self.state.record_diagnostic("analysis_reused_job", "Fresh job analysis reused", {"job_id": job_id, "selected_video": selection})
                return
            cached = None if refresh else self.cache.get(job.source_url, job.settings.browser_cookies)
            if cached is not None:
                cached.audio_streams = sort_audio_streams(cached.audio_streams)
                cached.subtitles = sort_subtitles(cached.subtitles)
                selection = self._selection(job.selected_video, cached.video_streams, cached.audio_streams)
                private_headers = dict((cached.metadata or {}).get("_transfer_headers") or {})
                self.state.update_job(job_id, analysis=cached, status=JobStatus.READY, failure_reason="", selected_video=selection, transfer_headers=private_headers)
                self.state.set_progress(job_id, ProgressState(stage=Stage.IDLE, message="Ready"), JobStatus.READY)
                self.state.record_diagnostic("analysis_cache_hit", "Analysis cache reused", {"job_id": job_id, "selected_video": selection})
                return
            cancelled = threading.Event()
            self._cancelled[job_id] = cancelled
            self.state.update_job(job_id, started_at=time.time(), finished_at=None)
            self.state.set_progress(job_id, ProgressState(stage=Stage.ANALYZING, message="Analyzing"), JobStatus.ANALYZING)
            thread = threading.Thread(target=self._worker, args=(job_id, cancelled), daemon=True)
            self._threads[job_id] = thread
            self.state.record_diagnostic("analysis_worker_started", "Analysis worker started", {"job_id": job_id})
            thread.start()

    def cancel(self, job_id: str) -> None:
        with self._lock:
            event = self._cancelled.get(job_id)
            if event is not None:
                event.set()
        current = self.state.get_job(job_id).progress
        progress = ProgressState(stage=Stage.STOPPED, downloaded_bytes=current.downloaded_bytes, total_bytes=current.total_bytes, total_is_estimate=current.total_is_estimate, elapsed_seconds=current.elapsed_seconds, terminal=True, message="Stopped")
        self.state.set_progress(job_id, progress, JobStatus.STOPPED)
        self.state.record_diagnostic("analysis_cancelled", "Analysis cancelled", {"job_id": job_id})

    def _worker(self, job_id: str, cancelled: threading.Event) -> None:
        job = self.state.get_job(job_id)
        try:
            self.state.set_progress_if_status(job_id, ProgressState(stage=Stage.RESOLVING_SOURCE, message="Resolving source"), JobStatus.ANALYZING, {JobStatus.ANALYZING})
            analysis = self.resolvers.analyze(job.source_url, job.settings.browser_cookies, cancel_event=cancelled)
            if cancelled.is_set():
                return
            analysis.audio_streams = sort_audio_streams(analysis.audio_streams)
            analysis.subtitles = sort_subtitles(analysis.subtitles)
            self.cache.put(analysis, job.settings.browser_cookies)
            if cancelled.is_set():
                return
            status = JobStatus.READY
            selection = self._selection(job.selected_video, analysis.video_streams, analysis.audio_streams)
            private_headers = dict((analysis.metadata or {}).get("_transfer_headers") or {})
            committed = self.state.update_job_if_status(job_id, {"analysis": analysis, "status": status, "failure_reason": "", "selected_video": selection, "transfer_headers": private_headers}, {JobStatus.ANALYZING})
            if committed:
                self.state.set_progress_if_status(job_id, ProgressState(stage=Stage.IDLE, message="Ready"), status, {JobStatus.READY})
                self.state.record_diagnostic("analysis_completed", "Media analysis completed", {"job_id": job_id, "platform": analysis.platform, "title": analysis.title, "video_streams": len(analysis.video_streams), "audio_streams": len(analysis.audio_streams), "subtitles": len(analysis.subtitles), "selected_video": selection})
        except (AuthenticationRequired, PermissionError):
            if cancelled.is_set():
                return
            self._fail(job_id, JobStatus.AUTH_REQUIRED, "Authentication required", "authentication_required")
        except SourceRefusedRequest:
            if cancelled.is_set():
                return
            self._fail(job_id, JobStatus.UNRESOLVED, "This source refused a direct request. Open it in the Media Detector.", "source_refused")
        except ValueError as exc:
            if cancelled.is_set():
                return
            self._fail(job_id, JobStatus.UNANALYZED, str(exc) or "Invalid URL", "invalid_input")
        except MediaUnresolved:
            if cancelled.is_set():
                return
            self._fail(job_id, JobStatus.UNRESOLVED, "Could not resolve media", "unresolved_media")
        except ResolverDependencyError:
            if cancelled.is_set():
                return
            self._fail(job_id, JobStatus.DEPENDENCY_REPAIR, "Dependency repair required", "dependency_repair")
        except ResolverUnsupported:
            if cancelled.is_set():
                return
            self._fail(job_id, JobStatus.UNRESOLVED, "Could not resolve media", "unresolved_media")
        except MediaUnavailable:
            if cancelled.is_set():
                return
            self._fail(job_id, JobStatus.UNAVAILABLE, "Unavailable", "media_unavailable")
        except Exception as exc:
            if cancelled.is_set():
                return
            self.state.record_diagnostic("analysis_internal_error", "Unexpected analysis failure", {"job_id": job_id, "error_type": type(exc).__name__, "error": str(exc)}, "error")
            self._fail(job_id, JobStatus.FAILED, "Analysis failed because VideoHaul encountered an internal error", "internal_error")
        finally:
            with self._lock:
                if self._threads.get(job_id) is threading.current_thread():
                    self._threads.pop(job_id, None)
                if self._cancelled.get(job_id) is cancelled:
                    self._cancelled.pop(job_id, None)


    def shutdown(self, timeout: float = 5.0) -> None:
        with self._lock:
            events = list(self._cancelled.values())
            threads = list(self._threads.values())
        for event in events:
            event.set()
        deadline = time.monotonic() + max(0.0, float(timeout))
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        with self._lock:
            alive = [thread.name for thread in self._threads.values() if thread.is_alive()]
        if alive:
            self.state.record_diagnostic("analysis_shutdown_workers_alive", "Some analysis workers did not stop before the shutdown deadline", {"threads": alive}, "warning")

    def _selection(self, current: str | None, streams, audio_streams) -> str | None:
        ids = {stream.stream_id for stream in streams}
        if current == AUDIO_ONLY_SELECTOR and audio_streams:
            return current
        if current == BEST_SELECTOR or current in ids:
            return current
        return select_preferred_stream(streams, self.state.settings.default_quality)

    def _fail(self, job_id: str, status: JobStatus, message: str, failure_class: str = "") -> None:
        committed = self.state.update_job_if_status(job_id, {"status": status, "failure_reason": message, "failure_class": str(failure_class or "")}, {JobStatus.ANALYZING})
        if committed:
            self.state.set_progress_if_status(job_id, ProgressState(stage=Stage.FAILED, terminal=True, message=message), status, {status})
            level = "warning" if status in {JobStatus.UNRESOLVED, JobStatus.AUTH_REQUIRED} else "error"
            self.state.record_diagnostic("analysis_failed", message, {"job_id": job_id, "status": status.value, "failure_class": str(failure_class or "")}, level)
