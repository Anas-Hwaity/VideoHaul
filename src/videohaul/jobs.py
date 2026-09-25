from __future__ import annotations

import copy
import os
from dataclasses import replace
from pathlib import Path
import subprocess
import threading
import time

from .bandwidth import allocate_bandwidth
from .completion import CompletionService
from .downloaders.contracts import TransferResult, normalize_result
from .downloaders.http_direct import DirectHttpTransfer, is_direct_file_job
from .downloaders.manifest_direct import DirectManifestTransfer, is_direct_manifest_job
from .downloaders.ytdlp import YtDlpTransfer
from .dependencies import ensure_ffmpeg, ensure_tools
from .file_policy import remove_partial
from .files import destination_info, open_directory, reveal_file
from .filesystem import check_destination, reserve_for_jobs
from .http_security import has_sensitive_headers
from .media_candidates import redact_url
from .models import RETRYABLE_STATUSES, JobStatus, ProgressState, Stage
from .postprocess.thumbnail import embed_source_thumbnail
from .postprocess.metadata import MetadataStampUnsupported, stamp_videohaul_comment
from .postprocess.verify import probe_media, validate_output
from .quality import AUDIO_ONLY_SELECTOR
from .state import VideoHaulState


class JobController:
    def _diag(self, job_id: str, kind: str, message: str, payload: dict | None = None, level: str = "info") -> None:
        data = {"job_id": job_id}
        data.update(payload or {})
        self.state.record_diagnostic(kind, message, data, level)

    def __init__(self, state: VideoHaulState, network_checker=None, completion_service=None):
        self.state = state
        self.completion = completion_service or CompletionService(state)
        self.network_checker = network_checker or default_network_checker
        self._active: dict[str, object] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._runs: dict[str, int] = {}
        self._lock = threading.RLock()
        self._scheduler_gate = threading.Lock()
        self._scheduler_thread: threading.Thread | None = None
        self._scheduler_refresh_requested = False
        self._progress_diag_at: dict[str, float] = {}
        self._closing = threading.Event()
        self._completion_threads: set[threading.Thread] = set()

    def start(self, job_id: str) -> None:
        if self._closing.is_set():
            raise RuntimeError("VideoHaul is shutting down")
        with self._lock:
            current = self.state.get_job(job_id)
            job = copy.deepcopy(current)
            reserved = reserve_for_jobs(self.state.jobs, job_id)
            self._diag(job_id, "job_start_requested", "Start requested", {"status": job.status.value, "selected_video": job.selected_video, "selected_audio": list(job.settings.selected_audio), "audio_output": job.settings.audio_output_format, "destination": job.settings.destination})
            if job.status not in {JobStatus.READY, JobStatus.PAUSED, JobStatus.FAILED, JobStatus.STOPPED, JobStatus.QUEUED}:
                raise RuntimeError("Job is not ready to start")
            resume_active = job.status == JobStatus.PAUSED and job_id in self._active
        if resume_active:
            self.resume(job_id)
            return
        while True:
            preflight_key = self._destination_preflight_key(job)
            destination = check_destination(job, reserved, True)
            if not destination.writable or destination.enough_space is False:
                reason = destination.error or "Destination is unavailable"
                self._diag(job_id, "destination_preflight_failed", reason, {"destination": destination.destination, "free_bytes": destination.free_bytes, "required_bytes": destination.required_bytes, "reserved_bytes": destination.reserved_bytes}, "error")
                raise RuntimeError(reason)
            retry_preflight = False
            resume_after_preflight = False
            with self._lock:
                current = self.state.get_job(job_id)
                if current.status not in {JobStatus.READY, JobStatus.PAUSED, JobStatus.FAILED, JobStatus.STOPPED, JobStatus.QUEUED}:
                    self._diag(job_id, "job_start_cancelled", "Start cancelled because the job state changed during destination preflight", {"status": current.status.value})
                    return
                if self._destination_preflight_key(current) != preflight_key:
                    job = copy.deepcopy(current)
                    reserved = reserve_for_jobs(self.state.jobs, job_id)
                    retry_preflight = True
                elif current.status == JobStatus.PAUSED and job_id in self._active:
                    resume_after_preflight = True
                elif self._active_slot_count() >= self._concurrency_limit() or (current.settings.scheduled_start is not None and current.settings.scheduled_start > time.time()):
                    progress = ProgressState(stage=Stage.QUEUED, message="Queued")
                    self.state.set_progress(job_id, progress, JobStatus.QUEUED)
                    self._diag(job_id, "job_queued", "Job queued", {"active": self._active_slot_count(), "limit": self._concurrency_limit()})
                else:
                    run_id = self._runs.get(job_id, 0) + 1
                    self._runs[job_id] = run_id
                    transfer = self._create_transfer(job_id, run_id)
                    self._diag(job_id, "job_transfer_created", "Transfer created", {"run_id": run_id, "resume_state": str(getattr(transfer, "resume_state", current.resume_state))})
                    self._active[job_id] = transfer
                    try:
                        self.state.update_job(
                            job_id,
                            status=JobStatus.DOWNLOADING,
                            failure_reason="",
                            failure_class="",
                            started_at=current.started_at or time.time(),
                            resume_state=str(getattr(transfer, "resume_state", current.resume_state)),
                        )
                        self.state.set_progress(job_id, ProgressState(stage=self._initial_stage(current), message="Preparing downloader"), JobStatus.DOWNLOADING)
                        thread = threading.Thread(target=self._run_job, args=(job_id, transfer, run_id), daemon=True)
                        self._threads[job_id] = thread
                        self._rebalance()
                        thread.start()
                    except Exception:
                        if self._active.get(job_id) is transfer:
                            self._active.pop(job_id, None)
                        self._threads.pop(job_id, None)
                        raise
            if retry_preflight:
                continue
            self.state.remember_folder(destination.destination)
            self._diag(job_id, "destination_preflight_passed", "Destination preflight passed", {"destination": destination.destination, "free_bytes": destination.free_bytes, "required_bytes": destination.required_bytes, "reserved_bytes": destination.reserved_bytes})
            if resume_after_preflight:
                self.resume(job_id)
            return

    @staticmethod
    def _destination_preflight_key(job) -> tuple:
        return (
            str(job.settings.destination),
            str(getattr(job.settings, "organization_rule", "none")),
            str(job.selected_video or ""),
            tuple(job.settings.selected_audio),
        )

    def _is_current_run(self, job_id: str, run_id: int, transfer=None) -> bool:
        with self._lock:
            if self._runs.get(job_id) != run_id:
                return False
            if transfer is not None and self._active.get(job_id) is not transfer:
                return False
            return True

    def _is_retryable_run(self, job_id: str, run_id: int, transfer=None) -> bool:
        if not self._is_current_run(job_id, run_id, transfer):
            return False
        try:
            return self.state.get_job(job_id).status in {JobStatus.DOWNLOADING, JobStatus.FINALIZING}
        except KeyError:
            return False

    def _create_transfer(self, job_id: str, run_id: int):
        job = self.state.get_job(job_id)
        progress = self._progress_callback(job_id, run_id)
        stage = self._stage_callback(job_id, run_id)
        diagnostic = lambda kind, message, payload, level="info": self._diag(job_id, kind, message, payload, level)
        if is_direct_file_job(job):
            return DirectHttpTransfer(job, progress, stage, diagnostic)
        if is_direct_manifest_job(job):
            if has_sensitive_headers(job.transfer_headers):
                raise RuntimeError("Authenticated detector HLS/DASH transfer was blocked because origin-scoped credential forwarding cannot be guaranteed safely")
            limits = [value for value in (job.settings.speed_limit_bps, job.effective_speed_limit_bps) if value is not None and int(value) > 0]
            if limits:
                return YtDlpTransfer(job, progress, stage, diagnostic)
            return DirectManifestTransfer(job, progress, stage, diagnostic)
        return YtDlpTransfer(job, progress, stage, diagnostic)

    def _component_message(self, job, stage: Stage) -> str:
        selected_stream = next(
            (item for item in job.analysis.video_streams if item.stream_id == job.selected_video),
            None,
        ) if job.analysis else None
        separate_audio = bool(
            selected_stream
            and not selected_stream.has_audio
            and not job.settings.video_only
            and (selected_stream.audio_reference or job.analysis.audio_streams)
        )
        if stage == Stage.DOWNLOADING_VIDEO:
            return "Step 1 of 2: Downloading Video" if separate_audio else "Downloading Video"
        if stage == Stage.DOWNLOADING_AUDIO:
            return "Step 2 of 2: Downloading Audio" if separate_audio else "Downloading Audio"
        if stage == Stage.DOWNLOADING_SUBTITLES:
            return "Downloading Subtitles"
        return stage.value.replace("_", " ").title()

    def _resumed_component(self, job) -> Stage:
        remembered = str((job.retry_state or {}).get("paused_stage") or "")
        if remembered in {Stage.DOWNLOADING_VIDEO.value, Stage.DOWNLOADING_AUDIO.value, Stage.DOWNLOADING_SUBTITLES.value}:
            return Stage(remembered)
        return self._initial_stage(job)

    def _progress_callback(self, job_id: str, run_id: int | None = None):
        if run_id is None:
            with self._lock:
                run_id = self._runs.get(job_id, 0)

        def callback(downloaded, total, speed, eta, elapsed, partial_path=None, resume_state=None, message=None, total_is_estimate=False):
            if not self._is_current_run(job_id, run_id):
                return
            current = self.state.get_job(job_id)
            current_stage = current.progress.stage
            stage = current_stage if current_stage in {Stage.DOWNLOADING_VIDEO, Stage.DOWNLOADING_AUDIO, Stage.DOWNLOADING_SUBTITLES} else self._initial_stage(current)
            determinate = total is not None and total > 0
            fraction = min(0.99, downloaded / total) if determinate else None
            progress = ProgressState(
                stage=stage,
                determinate=determinate,
                fraction=fraction,
                downloaded_bytes=downloaded,
                total_bytes=total,
                total_is_estimate=bool(total_is_estimate),
                speed_bps=speed,
                eta_seconds=eta,
                elapsed_seconds=elapsed,
                terminal=False,
                message=self._component_message(current, stage) if stage in {
                    Stage.DOWNLOADING_VIDEO, Stage.DOWNLOADING_AUDIO, Stage.DOWNLOADING_SUBTITLES,
                } else str(message or "Downloading"),
            )
            changed = self.state.set_transfer_progress_if_status(
                job_id,
                progress,
                JobStatus.DOWNLOADING,
                {JobStatus.DOWNLOADING},
                partial_path=partial_path,
                resume_state=resume_state,
            )
            if changed:
                now = time.monotonic()
                with self._lock:
                    previous = self._progress_diag_at.get(job_id)
                    diagnostic_due = previous is None or now - previous >= 1.0
                    if diagnostic_due:
                        self._progress_diag_at[job_id] = now
                if diagnostic_due:
                    self._diag(job_id, "job_progress", "Download progress updated", {
                        "stage": stage.value,
                        "downloaded_bytes": downloaded,
                        "total_bytes": total,
                        "speed_bps": speed,
                        "eta_seconds": eta,
                        "elapsed_seconds": elapsed,
                        "determinate": determinate,
                        "fraction": fraction,
                        "partial_path": partial_path or "",
                        "run_id": run_id,
                    })
        return callback

    def _stage_callback(self, job_id: str, run_id: int | None = None):
        if run_id is None:
            with self._lock:
                run_id = self._runs.get(job_id, 0)

        def callback(stage: str):
            if not self._is_current_run(job_id, run_id):
                return
            mapping = {
                "downloading_video": Stage.DOWNLOADING_VIDEO,
                "downloading_audio": Stage.DOWNLOADING_AUDIO,
                "downloading_subtitles": Stage.DOWNLOADING_SUBTITLES,
                "merging": Stage.MERGING,
                "finalizing": Stage.FINALIZING,
            }
            current_job = self.state.get_job(job_id)
            current = current_job.progress
            if stage in {"preparing", "starting"}:
                mapped = self._initial_stage(current_job)
                message = "Preparing downloader" if stage == "preparing" else "Starting yt-dlp"
            else:
                mapped = mapping.get(stage, current.stage)
                message = self._component_message(current_job, mapped) if stage in mapping else stage.replace("_", " ").title()
            status = JobStatus.FINALIZING if mapped in {Stage.MERGING, Stage.FINALIZING} else JobStatus.DOWNLOADING
            progress = ProgressState(
                stage=mapped,
                determinate=current.determinate,
                fraction=current.fraction,
                downloaded_bytes=current.downloaded_bytes,
                total_bytes=current.total_bytes,
                total_is_estimate=current.total_is_estimate,
                speed_bps=None,
                eta_seconds=None,
                elapsed_seconds=current.elapsed_seconds,
                terminal=False,
                message=message,
            )
            changed = self.state.set_progress_if_status(
                job_id,
                progress,
                status,
                {JobStatus.DOWNLOADING, JobStatus.FINALIZING},
            )
            if changed:
                self._diag(job_id, "job_stage_changed", message, {"stage": stage, "run_id": run_id})
        return callback

    def _run_job(self, job_id: str, transfer, run_id: int) -> None:
        try:
            while True:
                result = self._execute_transfer(job_id, transfer, run_id)
                if not self._is_current_run(job_id, run_id, transfer):
                    return
                if result.succeeded:
                    self._diag(job_id, "transfer_succeeded", "Transfer process succeeded", {"output_path": result.output_path, "run_id": run_id})
                    reported = Path(str(result.output_path or ""))
                    if not reported.is_file() or reported.stat().st_size <= 0:
                        raise RuntimeError("Downloader reported success without a usable output file")
                    self._stage(job_id, Stage.VALIDATING, "Validating staged output")
                    staged_path = self._validate_output(job_id, str(reported))
                    self._embed_thumbnail(job_id, staged_path)
                    self._stamp_download_comment(job_id, staged_path)
                    self._stage(job_id, Stage.VALIDATING, "Validating finalized output")
                    staged_path = self._validate_output(job_id, staged_path)
                    publisher = getattr(transfer, "publish", None)
                    if publisher is None:
                        raise RuntimeError("Transfer engine cannot publish a validated staged output")
                    output_path = str(publisher(staged_path))
                    published = Path(output_path)
                    if not published.is_file() or published.stat().st_size <= 0:
                        raise RuntimeError("Validated output could not be published")
                    self._complete(job_id, output_path, run_id, transfer)
                    self._diag(job_id, "job_completed", "Output validated and job completed", {"output_path": output_path, "run_id": run_id})
                    return
                if result.paused or result.stopped:
                    self._diag(job_id, "transfer_interrupted", "Transfer interrupted", {"paused": result.paused, "stopped": result.stopped, "run_id": run_id})
                    return
                if result.restart:
                    self._diag(job_id, "transfer_restart_required", "Transfer requested restart", {"run_id": run_id}, "warning")
                    replacement = self._replace_transfer(job_id, transfer, run_id)
                    if replacement is None:
                        return
                    transfer = replacement
                    continue
                replacement = self._retry_transfer(job_id, transfer, run_id, result)
                if replacement is None:
                    message = self._failure_message(result)
                    if self._is_current_run(job_id, run_id, transfer):
                        self._cleanup_partial_if_required(job_id)
                    self._fail(job_id, message, result.component, run_id, transfer)
                    return
                transfer = replacement
        except Exception as exc:
            if self._is_current_run(job_id, run_id, transfer):
                self._cleanup_partial_if_required(job_id)
            self._fail(job_id, str(exc) or "Download failed", "validation", run_id, transfer)
        finally:
            with self._lock:
                if self._runs.get(job_id) == run_id and self._active.get(job_id) is transfer:
                    self._active.pop(job_id, None)
                current_thread = threading.current_thread()
                if self._runs.get(job_id) == run_id and self._threads.get(job_id) is current_thread:
                    self._threads.pop(job_id, None)
                self._progress_diag_at.pop(job_id, None)
            self.request_scheduler_refresh()


    def _execute_transfer(self, job_id: str, transfer, run_id: int) -> TransferResult:
        return normalize_result(transfer.run())

    def _complete(self, job_id: str, output_path: str, run_id: int, transfer) -> None:
        committed = False
        with self._lock:
            if not self._is_current_run(job_id, run_id, transfer):
                return
            current = self.state.get_job(job_id).progress
            progress = ProgressState(
                stage=Stage.DONE,
                determinate=True,
                fraction=1.0,
                downloaded_bytes=current.total_bytes or current.downloaded_bytes,
                total_bytes=current.total_bytes or current.downloaded_bytes,
                total_is_estimate=current.total_is_estimate,
                speed_bps=0.0,
                eta_seconds=0.0,
                elapsed_seconds=current.elapsed_seconds,
                terminal=True,
                message="Done",
            )
            changes = {
                "status": JobStatus.DONE,
                "finished_at": time.time(),
                "failure_reason": "",
                "failure_class": "",
                "retry_state": {},
                "resume_state": "supported",
                "collapsed": bool(self.state.settings.auto_collapse_completed) or self.state.get_job(job_id).collapsed,
                "progress": progress.normalized(),
            }
            if output_path:
                changes["output_path"] = output_path
                changes["companion_paths"] = [str(path) for path in getattr(transfer, "published_companions", []) if str(path)]
                changes["partial_path"] = ""
            committed = self.state.update_job_if_status(
                job_id,
                changes,
                {JobStatus.DOWNLOADING, JobStatus.FINALIZING},
            )
        if not committed:
            return
        job = self.state.get_job(job_id)
        if str(job.settings.completion_action or "none") != "move_file":
            try:
                self.state.add_history_for_job(job_id)
            except Exception as exc:
                self._diag(job_id, "history_write_failed", "Completed output could not be added to history", {"error": str(exc)}, "error")
        thread = threading.Thread(target=self._run_completion_tracked, args=(job_id,), name=f"videohaul-completion-{job_id[:8]}", daemon=True)
        with self._lock:
            self._completion_threads.add(thread)
        thread.start()

    def _run_completion_tracked(self, job_id: str) -> None:
        try:
            self._run_completion(job_id)
        finally:
            with self._lock:
                self._completion_threads.discard(threading.current_thread())

    def _run_completion(self, job_id: str) -> None:
        try:
            job = self.state.get_job(job_id)
        except KeyError:
            return
        try:
            self.completion.notify(job)
        except Exception as exc:
            self._diag(job_id, "completion_notification_failed", str(exc), {}, "warning")
        try:
            self.completion.sound(job)
        except Exception as exc:
            self._diag(job_id, "completion_sound_failed", str(exc), {}, "warning")
        try:
            self.completion.run(job)
        except Exception as exc:
            self._diag(job_id, "completion_action_failed", str(exc), {}, "warning")
        if str(job.settings.completion_action or "none") == "move_file":
            try:
                self.state.add_history_for_job(job_id)
            except Exception as exc:
                self._diag(job_id, "history_write_failed", "Moved output could not be added to history", {"error": str(exc)}, "error")

    def _retry_transfer(self, job_id: str, transfer, run_id: int, result: TransferResult):
        job = self.state.get_job(job_id)
        state = dict(job.retry_state or {})
        attempts = int(state.get("attempts") or 0)
        maximum = max(0, int(job.settings.max_retries))
        if not result.retryable:
            self._diag(
                job_id,
                "retry_not_attempted",
                "This failure is deterministic, so retrying the identical command cannot change the outcome",
                {"component": str(result.component or ""), "attempts_consumed": attempts, "run_id": run_id},
                "warning",
            )
            return None
        if attempts >= maximum:
            self._diag(job_id, "retry_budget_exhausted", "All configured retries were used", {"component": str(result.component or ""), "attempts": attempts, "maximum": maximum, "run_id": run_id}, "warning")
            return None
        if str(result.component or "") == "network" and not self._online():
            current = job.progress
            waiting = ProgressState(
                stage=current.stage,
                determinate=current.determinate,
                fraction=current.fraction,
                downloaded_bytes=current.downloaded_bytes,
                total_bytes=current.total_bytes,
                total_is_estimate=current.total_is_estimate,
                speed_bps=0.0,
                eta_seconds=None,
                elapsed_seconds=current.elapsed_seconds,
                terminal=False,
                message="Waiting for connection",
            )
            self._diag(job_id, "offline_wait", "Network unavailable; retries paused", {"component": result.component, "attempts_consumed": attempts}, "warning")
            self.state.set_transfer_progress_if_status(job_id, waiting, JobStatus.DOWNLOADING, {JobStatus.DOWNLOADING, JobStatus.FINALIZING}, partial_path=str(getattr(transfer, "current_path", "") or job.partial_path), resume_state=str(getattr(transfer, "resume_state", job.resume_state)), retry_state=state)
            while self._is_retryable_run(job_id, run_id, transfer) and not self._online():
                time.sleep(0.25)
            if not self._is_retryable_run(job_id, run_id, transfer):
                return None
            self._diag(job_id, "online_restored", "Connectivity restored; retrying without consuming an offline retry", {"component": result.component})
        attempts += 1
        component_attempts = dict(state.get("components") or {})
        component_attempts[result.component] = int(component_attempts.get(result.component) or 0) + 1
        delay = max(0.0, float(job.settings.retry_backoff_seconds)) * (2 ** (attempts - 1))
        retry_state = {
            "attempts": attempts,
            "components": component_attempts,
            "last_component": result.component,
            "last_error": self._failure_message(result),
            "next_retry_at": time.time() + delay,
        }
        current = job.progress
        progress = ProgressState(
            stage=current.stage,
            determinate=current.determinate,
            fraction=current.fraction,
            downloaded_bytes=current.downloaded_bytes,
            total_bytes=current.total_bytes,
            total_is_estimate=current.total_is_estimate,
            speed_bps=0.0,
            eta_seconds=delay,
            elapsed_seconds=current.elapsed_seconds,
            terminal=False,
            message="Waiting to retry",
        )
        self._diag(job_id, "retry_scheduled", "Waiting to retry", {"component": result.component, "attempt": attempts, "maximum": maximum, "delay_seconds": delay, "error": self._failure_message(result)}, "warning")
        self.state.set_transfer_progress_if_status(
            job_id,
            progress,
            JobStatus.DOWNLOADING,
            {JobStatus.DOWNLOADING, JobStatus.FINALIZING},
            partial_path=str(getattr(transfer, "current_path", "") or job.partial_path),
            resume_state=str(getattr(transfer, "resume_state", job.resume_state)),
            retry_state=retry_state,
        )
        deadline = time.monotonic() + delay
        while time.monotonic() < deadline:
            if not self._is_retryable_run(job_id, run_id, transfer):
                return None
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        replacement = self._create_transfer(job_id, run_id)
        with self._lock:
            if not self._is_current_run(job_id, run_id, transfer):
                return None
            self._active[job_id] = replacement
        return replacement

    def _replace_transfer(self, job_id: str, transfer, run_id: int):
        replacement = self._create_transfer(job_id, run_id)
        with self._lock:
            if not self._is_current_run(job_id, run_id, transfer):
                return None
            self._active[job_id] = replacement
        return replacement

    def _stage(self, job_id: str, stage: Stage, message: str) -> None:
        try:
            current = self.state.get_job(job_id).progress
        except KeyError:
            return
        progress = ProgressState(
            stage=stage,
            determinate=current.determinate,
            fraction=current.fraction,
            downloaded_bytes=current.downloaded_bytes,
            total_bytes=current.total_bytes,
            total_is_estimate=current.total_is_estimate,
            speed_bps=0.0,
            eta_seconds=None,
            elapsed_seconds=current.elapsed_seconds,
            terminal=False,
            message=message,
        )
        self.state.set_progress_if_status(job_id, progress, None, {JobStatus.DOWNLOADING, JobStatus.FINALIZING})

    def _thumbnail_source(self, job) -> str:
        if job.thumbnail_source_url:
            return str(job.thumbnail_source_url)
        if job.analysis and job.analysis.thumbnail_url:
            return str(job.analysis.thumbnail_url)
        return ""

    def _embed_thumbnail(self, job_id: str, output_path: str) -> None:
        try:
            job = self.state.get_job(job_id)
        except KeyError:
            return
        if not job.settings.embed_thumbnail:
            self.state.update_job(job_id, thumbnail_embed_result="disabled", thumbnail_embed_detail="Source thumbnail embedding is disabled for this job")
            self._diag(job_id, "thumbnail_embed_skipped", "Source thumbnail embedding disabled for this job")
            return
        source = self._thumbnail_source(job)
        if not output_path:
            return
        self._stage(job_id, Stage.EMBEDDING_THUMBNAIL, "Embedding thumbnail")
        self._diag(job_id, "thumbnail_embed_started", "Embedding source thumbnail", {"output_path": output_path, "thumbnail_url": source})
        try:
            tool_dir = Path(ensure_ffmpeg())
            name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
            ffmpeg = tool_dir / name
        except Exception as exc:
            self.state.update_job(job_id, thumbnail_embed_result="embed_failed", thumbnail_embed_detail=str(exc))
            self._diag(job_id, "thumbnail_embed_failed", "Artwork engine unavailable", {"error": str(exc)}, "warning")
            return
        result = embed_source_thumbnail(output_path, source, ffmpeg)
        self.state.update_job(job_id, thumbnail_embed_result=result.state, thumbnail_embed_detail=result.detail, thumbnail_source_url=result.source_url or source)
        level = "info" if result.state in {"embedded", "disabled"} else "warning"
        self._diag(job_id, "thumbnail_embed_result", result.detail or result.state, {"state": result.state, "thumbnail_url": redact_url(result.source_url or source), "output_path": output_path}, level)

    def _stamp_download_comment(self, job_id: str, output_path: str) -> None:
        if not output_path:
            return
        try:
            job = self.state.get_job(job_id)
        except KeyError:
            return
        if not getattr(job.settings, "tag_download_comment", True):
            self._diag(job_id, "comment_tag_skipped", "Comment tag disabled for this job")
            return
        self._stage(job_id, Stage.EMBEDDING_METADATA, "Writing comment tag")
        try:
            ffmpeg_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
            ffprobe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
            tool_dir = Path(ensure_ffmpeg())
            stamp_videohaul_comment(output_path, tool_dir / ffmpeg_name, tool_dir / ffprobe_name)
        except MetadataStampUnsupported as exc:
            self._diag(job_id, "comment_tag_unsupported", str(exc), {"output_path": output_path}, "info")
            return
        except Exception as exc:
            self._diag(job_id, "comment_tag_failed", "The comment tag could not be written; the download is unaffected", {"output_path": output_path, "error": str(exc)}, "warning")
            return
        self._diag(job_id, "comment_tag_written", "Comment tag written and verified", {"output_path": output_path})

    def _validate_output(self, job_id: str, output_path: str) -> str:
        self._diag(job_id, "output_validation_started", "Validating completed output", {"output_path": output_path})
        if not output_path:
            raise RuntimeError("Downloaded output path was not reported")
        job = self.state.get_job(job_id)
        probe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
        probe = Path(ensure_ffmpeg()) / probe_name
        require_video = job.selected_video != AUDIO_ONLY_SELECTOR
        selected = None
        if job.analysis and require_video:
            selected = next((item for item in job.analysis.video_streams if item.stream_id == job.selected_video), None)
        require_audio = not require_video or bool(job.settings.selected_audio)
        if require_video and selected is not None:
            require_audio = require_audio or selected.has_audio or bool(job.analysis and job.analysis.audio_streams)
        require_subtitles = bool(
            job.settings.subtitles_enabled
            and job.settings.selected_subtitles
            and job.settings.subtitle_mode in {"embed", "both"}
        )
        validated = validate_output(output_path, probe, require_video, require_audio, require_subtitles)
        if job.settings.subtitles_enabled and job.settings.selected_subtitles and job.settings.subtitle_mode in {"external", "both"}:
            path = Path(validated)
            formats = {".srt", ".vtt", ".ass", ".ssa", ".lrc"}
            if not any(item.is_file() and item.suffix.lower() in formats for item in path.parent.glob(path.stem + "*")):
                raise RuntimeError("Expected external subtitle output was not created")
        self._diag(job_id, "output_validation_passed", "Output validation passed", {"output_path": validated})
        return validated

    def _failure_message(self, result: TransferResult) -> str:
        lines = [line.strip() for line in str(result.detail or "").splitlines() if line.strip()]
        detail = lines[-1] if lines else "Download failed"
        lowered = detail.casefold()
        component = str(result.component or "download")
        if component == "network" and any(token in lowered for token in ("failed to resolve", "getaddrinfo", "name or service not known")):
            summary = "Network failed: DNS could not resolve the source host"
        elif component == "network" and any(token in lowered for token in ("timed out", "timeout")):
            summary = "Network failed: Connection timed out"
        elif component == "network" and any(token in lowered for token in ("429", "rate limit")):
            summary = "Network failed: Source rate limit reached"
        elif component == "authentication":
            summary = "Authentication failed: The source rejected the current session"
        elif component == "access":
            summary = "Access failed: The detected media URL rejected the preserved browser request context"
        elif component == "unavailable":
            summary = "Media unavailable: The detected media URL is no longer available"
        elif component == "filesystem":
            summary = "Filesystem failed: The destination is unavailable or not writable"
        elif component == "collision":
            summary = "Output collision: The destination file already exists"
        else:
            summary = f"{component.replace('_', ' ').title()} failed"
        return summary if detail == summary else f"{summary}\n{detail}"

    def _cleanup_partial_if_required(self, job_id: str) -> None:
        job = self.state.get_job(job_id)
        policy = str(job.settings.partial_cleanup or "keep").lower()
        if policy == "ask" and job.partial_path and Path(job.partial_path).is_file():
            retry_state = dict(job.retry_state or {})
            retry_state["partial_cleanup_pending"] = True
            retry_state["partial_cleanup_path"] = str(job.partial_path)
            self.state.update_job(job_id, retry_state=retry_state)
            self._diag(job_id, "partial_cleanup_confirmation_required", "Choose whether to keep or remove the failed partial file", {"partial_path": str(job.partial_path)}, "warning")
            return
        if policy != "remove":
            return
        if remove_partial(job.partial_path):
            self.state.update_job(job_id, partial_path="", resume_state="restart_required")

    def resolve_partial_cleanup(self, job_id: str, action: str) -> dict:
        job = self.state.get_job(job_id)
        value = str(action or "").casefold()
        if value not in {"keep", "remove"}:
            raise ValueError("Partial cleanup action must be keep or remove")
        retry_state = dict(job.retry_state or {})
        pending = bool(retry_state.get("partial_cleanup_pending"))
        if not pending:
            return {"action": value, "changed": False, "partial_path": str(job.partial_path or "")}
        removed = False
        partial_path = str(job.partial_path or retry_state.get("partial_cleanup_path") or "")
        if value == "remove" and partial_path:
            removed = remove_partial(partial_path)
        retry_state.pop("partial_cleanup_pending", None)
        retry_state.pop("partial_cleanup_path", None)
        changes = {"retry_state": retry_state}
        if removed:
            changes.update({"partial_path": "", "resume_state": "restart_required"})
        self.state.update_job(job_id, **changes)
        self._diag(job_id, "partial_cleanup_resolved", "Partial file removed" if removed else "Partial file kept", {"action": value, "partial_path": partial_path})
        return {"action": value, "changed": True, "removed": removed, "partial_path": "" if removed else partial_path}

    def _fail(self, job_id: str, message: str, component: str = "download", run_id: int | None = None, transfer=None) -> bool:
        with self._lock:
            if run_id is not None and not self._is_current_run(job_id, run_id, transfer):
                return False
            try:
                job = self.state.get_job(job_id)
            except KeyError:
                return False
            if job.status not in {JobStatus.DOWNLOADING, JobStatus.FINALIZING}:
                return False
            current = job.progress
            state = dict(job.retry_state or {})
            state["last_component"] = component
            state["last_error"] = message
            progress = ProgressState(
                stage=Stage.FAILED,
                downloaded_bytes=current.downloaded_bytes,
                total_bytes=current.total_bytes,
                total_is_estimate=current.total_is_estimate,
                elapsed_seconds=current.elapsed_seconds,
                terminal=True,
                message=message,
            )
            changes = {"status": JobStatus.FAILED, "failure_reason": message, "failure_class": component, "retry_state": state, "progress": progress.normalized()}
            if self.state.settings.auto_expand_failed:
                changes["collapsed"] = False
            committed = self.state.update_job_if_status(job_id, changes, {JobStatus.DOWNLOADING, JobStatus.FINALIZING})
        if committed:
            self._diag(job_id, "job_failed", message, {"component": component}, "error")
        return bool(committed)

    def pause(self, job_id: str, schedule: bool = True) -> None:
        self._diag(job_id, "job_pause_requested", "Pause requested")
        with self._lock:
            transfer = self._active.get(job_id)
            if transfer is None:
                raise RuntimeError("Job is not active")
        mode = transfer.pause()
        pause_raced = False
        pause_completed_elsewhere = False
        with self._lock:
            job = self.state.get_job(job_id)
            active_now = self._active.get(job_id)
            if active_now is not None and active_now is not transfer:
                pause_raced = True
            else:
                previous_stage = job.progress.stage
                current = replace(
                    job.progress,
                    stage=Stage.PAUSED,
                    message="Paused",
                    speed_bps=0.0,
                    eta_seconds=None,
                )
                if not self.state.set_progress_if_status(
                    job_id,
                    current,
                    JobStatus.PAUSED,
                    {JobStatus.DOWNLOADING},
                ):
                    current_status = self.state.get_job(job_id).status
                    pause_completed_elsewhere = current_status not in {JobStatus.DOWNLOADING}
                    pause_raced = not pause_completed_elsewhere
                else:
                    if previous_stage in {Stage.DOWNLOADING_VIDEO, Stage.DOWNLOADING_AUDIO, Stage.DOWNLOADING_SUBTITLES}:
                        job = self.state.get_job(job_id)
                        self.state.update_job(job_id, retry_state={**job.retry_state, "paused_stage": previous_stage.value})
                    if mode == "restart" and self._active.get(job_id) is transfer:
                        self._active.pop(job_id, None)
        if pause_raced:
            raise RuntimeError("Job is no longer downloadable")
        if pause_completed_elsewhere:
            self._rebalance()
            if schedule:
                self._schedule_queued()
            current_status = self.state.get_job(job_id).status
            if current_status == JobStatus.DONE:
                raise RuntimeError("Job completed before pause could be applied")
            raise RuntimeError(f"Job is no longer downloadable ({current_status.value})")
        self._diag(job_id, "job_paused", "Job paused", {"mode": mode})
        self._rebalance()
        if schedule:
            self._schedule_queued()

    def resume(self, job_id: str) -> None:
        self._diag(job_id, "job_resume_requested", "Resume requested")
        with self._lock:
            job = self.state.get_job(job_id)
            if job.status != JobStatus.PAUSED:
                raise RuntimeError("Job is not paused")
            if self._active_slot_count() >= self._concurrency_limit():
                progress = replace(job.progress, stage=Stage.QUEUED, message="Queued", speed_bps=0.0, eta_seconds=None)
                self.state.set_progress(job_id, progress, JobStatus.QUEUED)
                return
            transfer = self._active.get(job_id)
        if transfer is None:
            self.start(job_id)
            return
        mode = transfer.resume()
        if mode == "restart":
            with self._lock:
                if self._active.get(job_id) is transfer:
                    self._active.pop(job_id, None)
            self.start(job_id)
            return
        pause_after_race = False
        with self._lock:
            if self._active.get(job_id) is not transfer:
                pause_after_race = True
            job = self.state.get_job(job_id)
            component = self._resumed_component(job)
            current = replace(job.progress, stage=component, message=self._component_message(job, component))
            if not pause_after_race and not self.state.set_progress_if_status(
                job_id,
                current,
                JobStatus.DOWNLOADING,
                {JobStatus.PAUSED},
            ):
                pause_after_race = True
        if pause_after_race:
            transfer.pause()
            raise RuntimeError("Job is no longer paused")
        self._diag(job_id, "job_resumed", "Job resumed")
        self._rebalance()

    def stop(self, job_id: str, schedule: bool = True) -> None:
        self._diag(job_id, "job_stop_requested", "Stop requested")
        with self._lock:
            transfer = self._active.get(job_id)
        if transfer is not None:
            transfer.stop()
        with self._lock:
            current = self.state.get_job(job_id).progress
            progress = ProgressState(
                stage=Stage.STOPPED,
                downloaded_bytes=current.downloaded_bytes,
                total_bytes=current.total_bytes,
                total_is_estimate=current.total_is_estimate,
                elapsed_seconds=current.elapsed_seconds,
                terminal=True,
                message="Stopped",
            )
            changed = self.state.set_progress_if_status(
                job_id,
                progress,
                JobStatus.STOPPED,
                {JobStatus.DOWNLOADING, JobStatus.PAUSED, JobStatus.FINALIZING, JobStatus.QUEUED},
            )
            current_status = self.state.get_job(job_id).status
            stop_completed_elsewhere = not changed and current_status != JobStatus.STOPPED
            if stop_completed_elsewhere and current_status not in {JobStatus.DONE, JobStatus.FAILED, JobStatus.UNAVAILABLE}:
                raise RuntimeError("Job is not active")
        if stop_completed_elsewhere:
            self._rebalance()
            if schedule:
                self._schedule_queued()
            return
        self._diag(job_id, "job_stopped", "Job stopped")
        self._rebalance()
        if schedule:
            self._schedule_queued()

    def retry(self, job_id: str) -> None:
        self._diag(job_id, "job_retry_requested", "Retry requested")
        job = self.state.get_job(job_id)
        if job.status not in RETRYABLE_STATUSES:
            raise RuntimeError("Job is not retryable")
        self.state.update_job(job_id, status=JobStatus.READY, failure_reason="", failure_class="", retry_state={})
        self.start(job_id)

    def delete(self, job_id: str) -> None:
        self._diag(job_id, "job_delete_requested", "Delete requested")
        with self._lock:
            thread = self._threads.get(job_id)
            active = self._active.get(job_id)
        if active is not None:
            self.stop(job_id)
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
            if thread.is_alive():
                raise RuntimeError("Job is still stopping")
        job = self.state.get_job(job_id)
        if job.status in {JobStatus.DOWNLOADING, JobStatus.ANALYZING, JobStatus.FINALIZING}:
            raise RuntimeError("Job is still stopping")
        self.state.delete_job(job_id)
        self.state.record_diagnostic("job_deleted_by_controller", "Job removed", {"job_id": job_id})

    def pause_all(self) -> None:
        for job_id in list(self.active_job_ids()):
            job = self.state.get_job(job_id)
            if job.status == JobStatus.DOWNLOADING:
                self.pause(job_id, schedule=False)
        self._rebalance()

    def resume_all(self) -> None:
        ids = [job.job_id for job in list(self.state.jobs) if job.status == JobStatus.PAUSED]
        for job_id in ids:
            self.resume(job_id)

    def stop_all(self) -> None:
        ids = [job.job_id for job in list(self.state.jobs) if job.status in {JobStatus.DOWNLOADING, JobStatus.PAUSED, JobStatus.FINALIZING, JobStatus.QUEUED}]
        for job_id in ids:
            try:
                self.stop(job_id, schedule=False)
            except RuntimeError:
                pass
        self._rebalance()

    def shutdown(self, timeout: float = 8.0) -> None:
        self._closing.set()
        self.stop_all()
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._lock:
                threads = [thread for thread in self._threads.values() if thread.is_alive()]
                transfers = list(self._active.values())
            if not threads:
                break
            for transfer in transfers:
                try:
                    transfer.stop()
                except Exception:
                    pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            for thread in threads:
                thread.join(timeout=min(0.2, remaining))
            if time.monotonic() >= deadline:
                break
        with self._lock:
            alive = [thread.name for thread in self._threads.values() if thread.is_alive()]
            completion_threads = [thread for thread in self._completion_threads if thread.is_alive()]
        if alive:
            self.state.record_diagnostic("shutdown_workers_alive", "Some download workers did not stop before the shutdown deadline", {"threads": alive}, "error")
        for thread in completion_threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)
        with self._lock:
            completion_alive = [thread.name for thread in self._completion_threads if thread.is_alive()]
        if completion_alive:
            self.state.record_diagnostic("completion_shutdown_workers_alive", "Some completion actions did not finish before shutdown", {"threads": completion_alive}, "warning")

    def open_media(self, job_id: str) -> bool:
        self._diag(job_id, "open_media_requested", "Open media requested")
        job = self.state.get_job(job_id)
        candidate = job.output_path or job.partial_path
        if not candidate:
            with self._lock:
                transfer = self._active.get(job_id)
            candidate = str(getattr(transfer, "current_path", "") or "")
        path = Path(candidate)
        if not path.is_file():
            return False
        if job.status in {JobStatus.DOWNLOADING, JobStatus.PAUSED, JobStatus.FINALIZING}:
            analysis = job.analysis
            selected = next((item for item in analysis.video_streams if item.stream_id == job.selected_video), None) if analysis else None
            if selected is not None and not selected.has_audio and (job.settings.selected_audio or analysis.audio_streams):
                return False
            try:
                probe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
                probe_media(path, Path(ensure_ffmpeg()) / probe_name, timeout=5)
            except Exception:
                return False
        if os.name == "nt":
            os.startfile(str(path))
        elif sys_platform() == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True

    def destination_state(self, job_id: str) -> dict:
        return destination_info(self.state.get_job(job_id))

    def open_folder(self, job_id: str, reveal: bool = False) -> bool:
        job = self.state.get_job(job_id)
        info = destination_info(job)
        self._diag(job_id, "open_folder_requested", "Open destination folder requested", {"destination": info["destination"], "available": info["available"], "reveal": bool(reveal)})
        if not info["available"]:
            self._diag(job_id, "open_folder_unavailable", info["reason"] or "Destination is unavailable", {"destination": info["destination"]}, "warning")
            return False
        if reveal and info["output_exists"] and info["reveal_supported"]:
            return reveal_file(info["output_path"])
        return open_directory(info["destination"])

    def delete_output(self, job_id: str) -> bool:
        self._diag(job_id, "delete_output_requested", "Delete downloaded file requested")
        job = self.state.get_job(job_id)
        if job.status != JobStatus.DONE or not job.output_path:
            raise RuntimeError("Completed output is unavailable")
        path = Path(job.output_path)
        if not path.is_file():
            return False
        path.unlink()
        for companion in list(job.companion_paths or []):
            candidate = Path(str(companion or ""))
            try:
                if candidate.is_file() and candidate.parent.resolve() == path.parent.resolve():
                    candidate.unlink()
            except Exception:
                continue
        progress = ProgressState(stage=Stage.STOPPED, terminal=True, message="Downloaded file deleted", elapsed_seconds=job.progress.elapsed_seconds)
        self.state.update_job(job_id, output_path="", companion_paths=[], status=JobStatus.STOPPED)
        self.state.set_progress(job_id, progress, JobStatus.STOPPED)
        return True

    def active_job_ids(self) -> list[str]:
        with self._lock:
            return list(self._active)

    def refresh_scheduler(self) -> None:
        with self._scheduler_gate:
            self._enforce_concurrency_limit()
            self._schedule_queued()
            self._rebalance()

    def request_scheduler_refresh(self) -> None:
        with self._lock:
            self._scheduler_refresh_requested = True
            current = self._scheduler_thread
            if current is not None and current.is_alive():
                return
            thread = threading.Thread(target=self._background_scheduler_refresh, name="videohaul-scheduler-refresh", daemon=True)
            self._scheduler_thread = thread
            thread.start()

    def _background_scheduler_refresh(self) -> None:
        current = threading.current_thread()
        try:
            while True:
                with self._lock:
                    if not self._scheduler_refresh_requested:
                        if self._scheduler_thread is current:
                            self._scheduler_thread = None
                        return
                    self._scheduler_refresh_requested = False
                self.refresh_scheduler()
        finally:
            restart = False
            with self._lock:
                if self._scheduler_thread is current:
                    self._scheduler_thread = None
                    restart = self._scheduler_refresh_requested
            if restart:
                self.request_scheduler_refresh()

    def revalidate_environment(self) -> dict:
        restored = []
        failed = []
        online = self._online()
        relevant = {JobStatus.PAUSED, JobStatus.QUEUED, JobStatus.READY, JobStatus.DOWNLOADING, JobStatus.FINALIZING}
        for job in list(self.state.jobs):
            if job.status not in relevant:
                continue
            check = check_destination(job, reserve_for_jobs(self.state.jobs, job.job_id), False)
            if not check.writable:
                with self._lock:
                    transfer = self._active.get(job.job_id)
                if transfer is not None:
                    try:
                        transfer.stop()
                    except Exception:
                        pass
                progress = replace(job.progress, stage=Stage.FAILED, terminal=True, speed_bps=0.0, eta_seconds=None, message="Destination unavailable")
                self.state.set_progress(job.job_id, progress, JobStatus.FAILED)
                self.state.update_job(job.job_id, failure_reason="Destination unavailable")
                failed.append(job.job_id)
                continue
            if job.status == JobStatus.PAUSED and job.progress.message == "Restored after restart":
                restored.append(job.job_id)
            if job.status == JobStatus.QUEUED and not online:
                waiting = replace(job.progress, stage=Stage.QUEUED, speed_bps=0.0, eta_seconds=None, message="Waiting for connection")
                self.state.set_progress(job.job_id, waiting, JobStatus.QUEUED)
        if online:
            self.refresh_scheduler()
        self.state.record_diagnostic("environment_revalidated", "Recovery environment revalidated", {"restored_jobs": restored, "failed_jobs": failed, "online": online})
        return {"restored_jobs": restored, "failed_jobs": failed, "online": online}

    def _online(self) -> bool:
        try:
            return bool(self.network_checker())
        except Exception:
            return True

    def _initial_stage(self, job) -> Stage:
        return Stage.DOWNLOADING_AUDIO if job.selected_video == AUDIO_ONLY_SELECTOR else Stage.DOWNLOADING_VIDEO

    def _concurrency_limit(self) -> int:
        try:
            return max(1, int(self.state.settings.max_concurrent_downloads))
        except Exception:
            return 1

    def _active_slot_count(self) -> int:
        return sum(1 for job in self.state.jobs if job.status in {JobStatus.DOWNLOADING, JobStatus.FINALIZING})

    def _schedule_queued(self) -> None:
        with self._lock:
            slots = self._concurrency_limit() - self._active_slot_count()
            if slots <= 0:
                return
            now = time.time()
            rank = {"high": 0, "normal": 1, "low": 2}
            queued = [
                job
                for job in self.state.jobs
                if job.status == JobStatus.QUEUED and (job.settings.scheduled_start is None or job.settings.scheduled_start <= now)
            ]
            queued.sort(key=lambda job: (not job.pinned, rank.get(str(job.settings.priority).lower(), 1), job.queue_position))
            selected = queued[:slots]
        for job in selected:
            with self._lock:
                transfer = self._active.get(job.job_id)
            if transfer is not None:
                transfer.resume()
                component = self._resumed_component(job)
                current = replace(job.progress, stage=component, message=self._component_message(job, component))
                self.state.set_progress_if_status(job.job_id, current, JobStatus.DOWNLOADING, {JobStatus.QUEUED})
            else:
                self.start(job.job_id)
        self._rebalance()

    def _enforce_concurrency_limit(self) -> None:
        with self._lock:
            running = [job for job in self.state.jobs if job.status == JobStatus.DOWNLOADING]
            limit = self._concurrency_limit()
            if len(running) <= limit:
                return
            rank = {"high": 0, "normal": 1, "low": 2}
            running.sort(key=lambda job: (not job.pinned, rank.get(str(job.settings.priority).lower(), 1), job.queue_position))
            overflow = [(job, self._active.get(job.job_id)) for job in running[limit:]]
        for job, transfer in overflow:
            if transfer is None:
                continue
            mode = transfer.pause()
            with self._lock:
                if self._active.get(job.job_id) is not transfer:
                    continue
                current_job = self.state.get_job(job.job_id)
                if current_job.status != JobStatus.DOWNLOADING:
                    continue
                previous_stage = current_job.progress.stage
                queued = replace(current_job.progress, stage=Stage.QUEUED, message="Queued by concurrency limit", speed_bps=0.0, eta_seconds=None)
                if self.state.set_progress_if_status(job.job_id, queued, JobStatus.QUEUED, {JobStatus.DOWNLOADING}):
                    if previous_stage in {Stage.DOWNLOADING_VIDEO, Stage.DOWNLOADING_AUDIO, Stage.DOWNLOADING_SUBTITLES}:
                        self.state.update_job(job.job_id, retry_state={**current_job.retry_state, "paused_stage": previous_stage.value})
                    if mode == "restart":
                        self._active.pop(job.job_id, None)

    def _rebalance(self) -> None:
        with self._lock:
            running = [job for job in self.state.jobs if job.status in {JobStatus.DOWNLOADING, JobStatus.FINALIZING}]
            allocation = allocate_bandwidth(
                running,
                self.state.settings.global_speed_limit_bps,
                self.state.settings.fair_share_mode,
                self.state.settings.prioritized_job_id,
            )
            self.state.set_effective_speed_limits(allocation)
            active = [(job, self._active.get(job.job_id)) for job in running]
        for job, transfer in active:
            if transfer is not None and hasattr(transfer, "reconfigure_speed_limit"):
                transfer.reconfigure_speed_limit(allocation.get(job.job_id))


def default_network_checker() -> bool:
    return True


def sys_platform() -> str:
    import sys
    return sys.platform
