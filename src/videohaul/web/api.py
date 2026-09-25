from __future__ import annotations

from copy import deepcopy
import hmac
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

from ..analysis import Analyzer
from ..batch import BatchService
from ..desktop import active_job_summary, persist_window_geometry, taskbar_state, tray_state
from ..files import destination_info
from ..media_candidates import candidate_request_headers, redact_url, url_carries_secrets, verify_candidate
from ..media_detection import DetectorUnavailable, MediaDetectorService, playwright_available
from ..preview import PreviewService, PreviewUnavailable, vlc_status
from ..updates import APPLICATION_VERSION, check_application_update, dependency_health, last_update_check, rollback_dependency, update_dependency
from .events import EventBroker, event_stream
from ..audio import AUDIO_OUTPUTS, audio_label, auto_audio_stream, sort_audio_streams, validate_audio_selection
from ..formats import combined_filesize, family_label, group_streams, stream_label
from ..jobs import JobController
from ..inputs import extract_urls
from ..models import ANALYSIS_FAILURE_STATUSES, RETRYABLE_STATUSES, JobSettings, JobStatus, ProgressState
from ..quality import AUDIO_ONLY_SELECTOR, DEVICE_PRESETS, SORT_KEYS, best_stream, recommended_stream
from ..progress import aggregate_progress
from ..serialization import job_to_dict
from ..subtitles import SUBTITLE_FORMATS, SUBTITLE_MODES, subtitle_label, validate_subtitle_selection
from ..state import VideoHaulState


class AddJobRequest(BaseModel):
    url: str = ""


class SmartAddRequest(BaseModel):
    text: str = ""
    add_duplicates: bool = False


class BulkRequest(BaseModel):
    job_ids: list[str]
    action: str
    values: dict | None = None


class JobUpdateRequest(BaseModel):
    source_url: str | None = None
    selected_video: str | None = None
    collapsed: bool | None = None
    pinned: bool | None = None
    settings: dict | None = None


class SettingsRequest(BaseModel):
    values: dict


class ReorderRequest(BaseModel):
    index: int


class BatchAnalyzeRequest(BaseModel):
    text: str = ""
    range_spec: str = ""
    requested_quality: str = "best"
    requested_audio_languages: list[str] = []
    requested_subtitle_languages: list[str] = []
    shared_settings: dict = {}
    filename_template: str = "%(playlist_index)03d - %(title)s.%(ext)s"


class BatchSelectionRequest(BaseModel):
    indexes: list[int] = []
    range_spec: str | None = None
    mode: str = "set"


class BatchConfigureRequest(BaseModel):
    values: dict


class BatchResolveRequest(BaseModel):
    action: str
    selected_video: str | None = None
    selected_audio: list[str] | None = None
    selected_subtitles: list[str] | None = None


class DetectorOpenRequest(BaseModel):
    url: str = ""
    job_id: str = ""
    mode: str = "interactive"
    headless: bool | None = None
    budget_seconds: float | None = None


class DetectorAdoptRequest(BaseModel):
    job_id: str = ""
    start: bool = False


class PreviewRequest(BaseModel):
    full_stream: bool = False


class PartialCleanupRequest(BaseModel):
    action: str


class WindowGeometryRequest(BaseModel):
    width: int
    height: int
    x: int | None = None
    y: int | None = None


class DependencyActionRequest(BaseModel):
    name: str


class ClientDiagnosticRequest(BaseModel):
    kind: str = "ui_event"
    message: str = ""
    payload: dict = {}
    level: str = "info"


def create_app(
    state: VideoHaulState,
    analyzer: Analyzer,
    jobs: JobController,
    static_dir,
    batch_service: BatchService | None = None,
    broker: EventBroker | None = None,
    detectors: MediaDetectorService | None = None,
    previews: PreviewService | None = None,
    access_token: str | None = None,
    allowed_hosts: set[str] | None = None,
) -> FastAPI:
    app = FastAPI(title="VideoHaul", docs_url=None, redoc_url=None, openapi_url=None)
    batch = batch_service or BatchService(state, getattr(analyzer, "resolvers", None))
    events = broker or EventBroker(state)

    def detector_observer(kind, message, payload, level="info"):
        state.record_diagnostic(kind, message, payload, level)

    detector_service = detectors or MediaDetectorService(observer=detector_observer, probe=verify_candidate, connectivity_checker=getattr(jobs, "network_checker", None))
    preview_service = previews or PreviewService(observer=detector_observer)
    app.state.events = events
    app.state.detectors = detector_service
    app.state.previews = preview_service

    cookie_name = session_cookie_name(allowed_hosts)

    @app.middleware("http")
    async def access_middleware(request: Request, call_next):
        if access_token is None:
            return await call_next(request)
        host = str(request.headers.get("host") or "").casefold()
        if allowed_hosts is not None and host not in allowed_hosts:
            return PlainTextResponse("Requests to VideoHaul must use its local address", status_code=421)
        origin = str(request.headers.get("origin") or "")
        if origin and origin.casefold() not in {f"http://{value}" for value in (allowed_hosts or set())}:
            return PlainTextResponse("Cross-origin requests are not accepted", status_code=403)
        fetch_site = str(request.headers.get("sec-fetch-site") or "").casefold()
        if fetch_site in {"cross-site", "same-site"}:
            return PlainTextResponse("Cross-site requests are not accepted", status_code=403)
        supplied_query = str(request.query_params.get("token") or "")
        if request.method == "GET" and request.url.path == "/" and supplied_query:
            if not hmac.compare_digest(supplied_query, access_token):
                return PlainTextResponse("This VideoHaul link has expired. Open VideoHaul again to get a new one.", status_code=401)
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(cookie_name, access_token, httponly=True, samesite="strict", path="/")
            return response
        supplied = str(request.cookies.get(cookie_name) or request.headers.get("x-videohaul-token") or "")
        if not supplied or not hmac.compare_digest(supplied, access_token):
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": "VideoHaul session token required"}, status_code=401)
            return PlainTextResponse("Open VideoHaul from its own window or from the link it prints when it starts.", status_code=401)
        return await call_next(request)

    @app.middleware("http")
    async def security_headers_middleware(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: http: https:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.middleware("http")
    async def diagnostics_middleware(request: Request, call_next):
        started = time.time()
        path = request.url.path
        should_log = request.method != "GET" and not path.startswith("/static/")
        try:
            response = await call_next(request)
        except Exception as exc:
            if should_log:
                state.record_diagnostic("api_request_failed", str(exc), {"method": request.method, "path": path, "elapsed_ms": round((time.time() - started) * 1000, 2)}, "error")
            raise
        if should_log:
            level = "error" if response.status_code >= 500 else "warning" if response.status_code >= 400 else "info"
            state.record_diagnostic("api_request", f"{request.method} {path} -> {response.status_code}", {"method": request.method, "path": path, "status": response.status_code, "elapsed_ms": round((time.time() - started) * 1000, 2)}, level)
        return response

    @app.get("/")
    def index():
        return FileResponse(static_dir / "index.html")

    @app.get("/static/{name}")
    def static(name: str):
        target = static_dir / name
        if not target.is_file() or target.parent != static_dir:
            raise HTTPException(404)
        return FileResponse(target)

    @app.get("/api/state")
    def get_state():
        jobs.request_scheduler_refresh()
        snapshot = state.snapshot()
        snapshot["aggregate_progress"] = aggregate_progress(state.jobs)
        snapshot["batch"] = batch.snapshot()
        snapshot["detector"] = {"available": playwright_available(), "sessions": detector_service.sessions()}
        snapshot["last_event_id"] = events.last_event_id()
        for item in snapshot["jobs"]:
            try:
                item["destination_info"] = destination_info(state.get_job(item["job_id"]))
            except Exception:
                item["destination_info"] = {"available": False, "destination": "", "reason": "Destination could not be inspected"}
            try:
                item["history_duplicate_count"] = len(state.duplicate_history_for_job(item["job_id"]))
                item["canonical_duplicate_job_ids"] = state.canonical_duplicate_job_ids(item["job_id"])
            except Exception:
                item["history_duplicate_count"] = 0
                item["canonical_duplicate_job_ids"] = []
            analysis = item.get("analysis")
            if analysis:
                public_video_streams = [dict(value) for value in analysis.get("video_streams") or []]
                public_audio_streams = [dict(value) for value in analysis.get("audio_streams") or []]
                public_video_by_id = {str(value.get("stream_id") or ""): value for value in public_video_streams}
                public_audio_by_id = {str(value.get("stream_id") or ""): value for value in public_audio_streams}
                streams = [_stream_from_dict(value) for value in public_video_streams]
                audios = [_audio_from_dict(value) for value in public_audio_streams]
                best = best_stream(streams)
                recommended = recommended_stream(streams)
                grouped = []
                for family, values in group_streams(streams):
                    rendered = []
                    for stream in values:
                        combined_size, combined_estimated = combined_filesize(stream, audios)
                        rendered.append({
                            **dict(public_video_by_id.get(str(stream.stream_id)) or job_to_plain_stream(stream)),
                            "label": stream_label(stream, audios),
                            "is_best": bool(best and stream.stream_id == best.stream_id),
                            "is_recommended": bool(recommended and stream.stream_id == recommended.stream_id),
                            "combined_filesize_bytes": combined_size,
                            "combined_filesize_is_estimate": combined_estimated,
                        })
                    grouped.append({"family": family, "label": family_label(family), "streams": rendered})
                analysis["quality_groups"] = grouped
                analysis["best_stream_id"] = best.stream_id if best else None
                analysis["recommended_stream_id"] = recommended.stream_id if recommended else None
                analysis["device_presets"] = list(DEVICE_PRESETS)
                analysis["stream_sort_keys"] = list(SORT_KEYS)
                ordered_audio = sort_audio_streams(audios)
                automatic_audio = auto_audio_stream(ordered_audio)
                analysis["audio_options"] = [
                    {**dict(public_audio_by_id.get(str(stream.stream_id)) or job_to_plain_audio(stream)), "label": audio_label(stream)}
                    for stream in ordered_audio
                ]
                analysis["auto_audio_stream_id"] = automatic_audio.stream_id if automatic_audio else None
                analysis["audio_output_formats"] = list(AUDIO_OUTPUTS)
                if analysis.get("subtitles"):
                    analysis["subtitles"] = [{**item, "display_label": subtitle_label(_subtitle_from_dict(item))} for item in analysis["subtitles"]]
                    analysis["subtitle_modes"] = list(SUBTITLE_MODES)
                    analysis["subtitle_formats"] = list(SUBTITLE_FORMATS)
        return snapshot

    @app.post("/api/jobs/{job_id}/streams/{stream_id}/reveal-url")
    def reveal_stream_url(job_id: str, stream_id: str):
        try:
            job = state.get_job(job_id)
        except KeyError:
            raise HTTPException(404, "Job not found")
        stream = next((item for item in (job.analysis.video_streams if job.analysis else []) if item.stream_id == stream_id), None)
        url = str(getattr(stream, "direct_stream_url", "") or "") if stream else ""
        if not url:
            raise HTTPException(409, "The selected stream does not expose a direct URL")
        if url_carries_secrets(url) or job.transfer_headers:
            raise HTTPException(409, "The selected stream URL carries temporary authorization and cannot be copied safely")
        state.record_diagnostic("stream_url_revealed", "Safe direct stream URL revealed for an explicit copy action", {"job_id": job_id, "stream_id": stream_id, "url": redact_url(url)})
        return {"url": url}

    @app.post("/api/jobs/{job_id}/streams/{stream_id}/preview")
    def preview_stream(job_id: str, stream_id: str, request: PreviewRequest):
        try:
            job = state.get_job(job_id)
        except KeyError:
            raise HTTPException(404, "Job not found")
        stream = next((item for item in (job.analysis.video_streams if job.analysis else []) if item.stream_id == stream_id), None)
        url = str(getattr(stream, "direct_stream_url", "") or "") if stream else ""
        if not url:
            raise HTTPException(409, "The selected stream has no previewable direct URL")
        from ..models import DetectedMediaCandidate

        candidate = DetectedMediaCandidate(
            candidate_id=f"stream-{job_id}-{stream_id}",
            discovered_url=url,
            final_url=url,
            title=job.analysis.title if job.analysis else "Selected stream",
            required_headers=dict(job.transfer_headers or {}),
            verification_state="verified",
        )
        try:
            return preview_service.preview(candidate, request.full_stream)
        except PreviewUnavailable as exc:
            raise HTTPException(409, str(exc))


    @app.get("/api/history")
    def get_history(q: str = "", limit: int = 1000):
        return {"items": state.history(q, limit)}

    @app.delete("/api/history")
    def clear_history():
        state.clear_history()
        return {"ok": True}

    @app.delete("/api/history/{history_id}")
    def delete_history(history_id: str):
        if not state.delete_history(history_id):
            raise HTTPException(404, "History entry not found")
        return {"ok": True}

    @app.post("/api/history/{history_id}/redownload")
    def redownload_history(history_id: str):
        try:
            return job_to_dict(state.redownload_history(history_id))
        except KeyError:
            raise HTTPException(404, "History entry not found")

    @app.post("/api/history/{history_id}/open")
    def open_history(history_id: str):
        try:
            entry = state.history_entry(history_id)
        except KeyError:
            raise HTTPException(404, "History entry not found")
        path = Path(str(entry.get("output_path") or ""))
        return {"ok": _open_path(path, folder=False)}

    @app.post("/api/history/{history_id}/open-folder")
    def open_history_folder(history_id: str):
        try:
            entry = state.history_entry(history_id)
        except KeyError:
            raise HTTPException(404, "History entry not found")
        path = Path(str(entry.get("output_path") or "")).parent
        return {"ok": _open_path(path, folder=True)}

    @app.get("/api/jobs/{job_id}/duplicates")
    def job_duplicates(job_id: str):
        try:
            return {"history": state.duplicate_history_for_job(job_id), "jobs": state.canonical_duplicate_job_ids(job_id)}
        except KeyError:
            raise HTTPException(404, "Job not found")

    @app.post("/api/recovery/revalidate")
    def revalidate_recovery():
        return jobs.revalidate_environment()

    @app.get("/api/events")
    def events_stream(request: Request):
        raw = request.headers.get("last-event-id") or request.query_params.get("last_event_id") or ""
        try:
            last_event_id = int(raw) if str(raw).strip() else None
        except Exception:
            last_event_id = None

        stopped = threading.Event()
        source = event_stream(events, last_event_id, stop=stopped.is_set, idle_ticks=True)

        async def stream():
            try:
                while not stopped.is_set():
                    if await request.is_disconnected():
                        break
                    chunk = await run_in_threadpool(next, source, None)
                    if chunk is None:
                        break
                    if chunk:
                        yield chunk
            finally:
                stopped.set()
                await run_in_threadpool(source.close)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    @app.get("/api/events/status")
    def events_status():
        return {"clients": events.client_count(), "last_event_id": events.last_event_id(), "replay_size": events.replay_size}

    @app.get("/api/jobs/{job_id}/destination")
    def job_destination(job_id: str):
        try:
            return destination_info(state.get_job(job_id))
        except KeyError:
            raise HTTPException(404, "Job not found")

    @app.get("/api/detector")
    def detector_overview():
        return {
            "available": playwright_available(),
            "sessions": detector_service.sessions(),
            "vlc": vlc_status(),
        }

    @app.post("/api/detector/sessions")
    def open_detector(request: DetectorOpenRequest):
        target = str(request.url or "").strip()
        job_id = str(request.job_id or "").strip()
        if not target and job_id:
            try:
                target = state.get_job(job_id).source_url
            except KeyError:
                raise HTTPException(404, "Job not found")
        if not target:
            raise HTTPException(400, "Enter a page URL to open in the Media Detector")
        if not playwright_available():
            raise HTTPException(409, "Managed Chromium automation runtime is unavailable")
        try:
            session = detector_service.open(target, job_id, request.mode, request.headless, request.budget_seconds)
        except Exception as exc:
            raise HTTPException(409, str(exc))
        if job_id:
            try:
                state.update_job(job_id, detector_session_id=session.session_id)
            except KeyError:
                pass
        return session.snapshot()

    @app.get("/api/detector/sessions/{session_id}")
    def detector_session(session_id: str):
        try:
            return detector_service.get(session_id).snapshot()
        except KeyError:
            raise HTTPException(404, "Media Detector session not found")

    @app.post("/api/detector/sessions/{session_id}/close")
    def close_detector(session_id: str):
        try:
            return detector_service.close(session_id)
        except KeyError:
            raise HTTPException(404, "Media Detector session not found")

    @app.delete("/api/detector/sessions/{session_id}")
    def discard_detector(session_id: str):
        detector_service.discard(session_id)
        return {"ok": True}

    @app.post("/api/detector/sessions/{session_id}/candidates/{candidate_id}/adopt")
    def adopt_candidate(session_id: str, candidate_id: str, request: DetectorAdoptRequest):
        try:
            session = detector_service.get(session_id)
            candidate = session.candidate(candidate_id)
        except KeyError:
            raise HTTPException(404, "Media Detector candidate not found")
        if candidate.rejected:
            raise HTTPException(409, "This candidate was rejected; restore it before adding it to VideoHaul")
        from ..resolvers.browser_media import candidate_analysis

        try:
            analysis = candidate_analysis(candidate)
        except Exception as exc:
            raise HTTPException(409, str(exc))
        existing = next(
            (
                job for job in state.jobs
                if job.detector_session_id == session.session_id
                and job.detector_candidate_id == candidate.candidate_id
            ),
            None,
        )
        job_id = existing.job_id if existing else str(request.job_id or session.state.job_id or "").strip()
        if existing is None and job_id:
            try:
                existing = state.get_job(job_id)
            except KeyError:
                existing = None
            if existing is not None and (
                existing.detector_candidate_id
                or existing.status in {JobStatus.ANALYZING, JobStatus.DOWNLOADING, JobStatus.PAUSED, JobStatus.FINALIZING, JobStatus.DONE}
            ):
                existing = None
        if existing is None:
            existing = state.add_job(candidate.page_url or analysis.source_url)
            job_id = existing.job_id
        if existing.detector_session_id == session.session_id and existing.detector_candidate_id == candidate.candidate_id:
            started = False
            if request.start and existing.status == JobStatus.READY:
                try:
                    jobs.start(job_id)
                    started = True
                except RuntimeError as exc:
                    raise HTTPException(409, str(exc))
            return {"job_id": job_id, "created": False, "already_adopted": True, "started": started}
        selection = analysis.video_streams[0].stream_id if analysis.video_streams else None
        state.update_job(
            job_id,
            analysis=analysis,
            status=JobStatus.READY,
            failure_reason="",
            failure_class="",
            selected_video=selection,
            thumbnail_source_url=candidate.poster_or_thumbnail or "",
            detector_session_id=session.session_id,
            detector_candidate_id=candidate.candidate_id,
            transfer_headers=candidate_request_headers(candidate),
        )
        state.record_diagnostic(
            "detector_candidate_adopted",
            "Media Detector candidate became a canonical job",
            {"job_id": job_id, "candidate_id": candidate.candidate_id, "url": redact_url(candidate.discovered_url or candidate.final_url), "verification_state": candidate.verification_state},
        )
        jobs.refresh_scheduler()
        started = False
        if request.start:
            try:
                jobs.start(job_id)
                started = True
            except RuntimeError as exc:
                raise HTTPException(409, str(exc))
        return {"job_id": job_id, "created": True, "already_adopted": False, "started": started}

    @app.post("/api/detector/sessions/{session_id}/candidates/{candidate_id}/reject")
    def reject_candidate(session_id: str, candidate_id: str):
        try:
            candidate = detector_service.get(session_id).candidate(candidate_id)
        except KeyError:
            raise HTTPException(404, "Media Detector candidate not found")
        candidate.rejected = True
        state.record_diagnostic("detector_candidate_rejected", "Candidate rejected by the user", {"candidate_id": candidate.candidate_id})
        return {"candidate_id": candidate.candidate_id, "rejected": True}

    @app.post("/api/detector/sessions/{session_id}/candidates/{candidate_id}/restore")
    def restore_candidate(session_id: str, candidate_id: str):
        try:
            candidate = detector_service.get(session_id).candidate(candidate_id)
        except KeyError:
            raise HTTPException(404, "Media Detector candidate not found")
        candidate.rejected = False
        state.record_diagnostic("detector_candidate_restored", "Rejected candidate restored by the user", {"candidate_id": candidate.candidate_id})
        return {"candidate_id": candidate.candidate_id, "rejected": False}

    @app.post("/api/detector/sessions/{session_id}/candidates/{candidate_id}/reveal-url")
    def reveal_candidate_url(session_id: str, candidate_id: str):
        try:
            candidate = detector_service.get(session_id).candidate(candidate_id)
        except KeyError:
            raise HTTPException(404, "Media Detector candidate not found")
        state.record_diagnostic(
            "detector_candidate_url_revealed",
            "Candidate URL revealed for an explicit user copy action",
            {"candidate_id": candidate.candidate_id, "url": redact_url(candidate.final_url or candidate.discovered_url)},
        )
        return {"url": candidate.final_url or candidate.discovered_url, "carries_secrets": bool(candidate.required_headers or candidate.required_cookie_scope)}

    @app.post("/api/detector/sessions/{session_id}/candidates/{candidate_id}/preview")
    def preview_candidate(session_id: str, candidate_id: str, request: PreviewRequest):
        try:
            candidate = detector_service.get(session_id).candidate(candidate_id)
        except KeyError:
            raise HTTPException(404, "Media Detector candidate not found")
        try:
            return preview_service.preview(candidate, request.full_stream)
        except PreviewUnavailable as exc:
            raise HTTPException(409, str(exc))

    @app.get("/api/preview/vlc")
    def preview_vlc():
        return vlc_status()

    @app.get("/api/diagnostics")
    def get_diagnostics(limit: int = 1000, scope: str = "session"):
        value = "all" if str(scope or "").casefold() == "all" else "session"
        return {"events": state.diagnostics(limit, value), "scope": value, **state.diagnostics_scope_info()}

    @app.delete("/api/diagnostics")
    def clear_diagnostics(scope: str = "session"):
        value = "all" if str(scope or "").casefold() == "all" else "session"
        state.clear_diagnostics(value)
        return {"ok": True, "scope": value}

    @app.post("/api/diagnostics/client")
    def client_diagnostic(request: ClientDiagnosticRequest):
        state.record_diagnostic(request.kind, request.message, request.payload, request.level)
        return {"ok": True}

    @app.get("/api/diagnostics/export")
    def export_diagnostics(limit: int = 5000, scope: str = "all"):
        events = state.diagnostics(limit, "all" if str(scope or "").casefold() == "all" else "session")
        return {
            "application": "VideoHaul",
            "version": APPLICATION_VERSION,
            "exported_at": time.time(),
            "platform": {"os": os.name, "python": sys.version.split()[0]},
            "settings": _exportable_settings(state.settings.to_dict()),
            "dependencies": dependency_health(),
            "detector": {"available": playwright_available(), "sessions": detector_service.sessions()},
            "jobs": [
                {
                    "job_id": item.job_id,
                    "status": item.status.value if hasattr(item.status, "value") else str(item.status),
                    "stage": item.progress.stage.value if hasattr(item.progress.stage, "value") else str(item.progress.stage),
                    "failure_class": item.failure_class,
                    "failure_reason": item.failure_reason,
                    "thumbnail_embed_result": item.thumbnail_embed_result,
                    "downloaded_bytes": item.progress.downloaded_bytes,
                    "total_bytes": item.progress.total_bytes,
                    "elapsed_seconds": item.progress.elapsed_seconds,
                }
                for item in state.jobs
            ],
            "events": events,
            "event_count": len(events),
        }

    @app.get("/api/shell")
    def shell_state():
        return {
            "taskbar": taskbar_state(state.jobs),
            "tray": tray_state(state.settings, state.jobs),
            "exit": active_job_summary(state.jobs),
            "window": {
                "width": state.settings.window_width,
                "height": state.settings.window_height,
                "x": state.settings.window_x,
                "y": state.settings.window_y,
            },
        }

    @app.post("/api/shell/window")
    def save_window_geometry(request: WindowGeometryRequest):
        saved = persist_window_geometry(state, request.width, request.height, request.x, request.y)
        if not saved:
            raise HTTPException(400, "Window geometry could not be stored")
        return saved

    @app.get("/api/dependencies")
    def dependencies():
        return dependency_health()

    @app.post("/api/dependencies/update")
    def update_dependency_endpoint(request: DependencyActionRequest):
        busy_jobs = [job.job_id for job in state.jobs if job.status in {JobStatus.ANALYZING, JobStatus.DOWNLOADING, JobStatus.PAUSED, JobStatus.FINALIZING, JobStatus.QUEUED}]
        detector_busy = any(bool(item.get("active")) for item in detector_service.sessions())
        batch_busy = str((batch.snapshot().get("activity") or {}).get("state") or "") == "analyzing"
        if busy_jobs or detector_busy or batch_busy:
            raise HTTPException(409, "Stop active analysis, downloads, batch analysis, and Media Detector sessions before updating dependencies")
        result = update_dependency(request.name)
        state.record_diagnostic("dependency_update", result.get("detail") or result.get("state") or "", result, "info" if result.get("state") == "updated" else "warning")
        if result.get("state") == "unsupported":
            raise HTTPException(400, result.get("detail") or "Unsupported dependency")
        return result

    @app.post("/api/dependencies/rollback")
    def rollback_dependency_endpoint(request: DependencyActionRequest):
        busy_jobs = [job.job_id for job in state.jobs if job.status in {JobStatus.ANALYZING, JobStatus.DOWNLOADING, JobStatus.PAUSED, JobStatus.FINALIZING, JobStatus.QUEUED}]
        detector_busy = any(bool(item.get("active")) for item in detector_service.sessions())
        batch_busy = str((batch.snapshot().get("activity") or {}).get("state") or "") == "analyzing"
        if busy_jobs or detector_busy or batch_busy:
            raise HTTPException(409, "Stop active analysis, downloads, batch analysis, and Media Detector sessions before rolling back dependencies")
        result = rollback_dependency(request.name)
        state.record_diagnostic("dependency_rollback", result.get("detail") or result.get("state") or "", result, "info" if result.get("state") == "rolled_back" else "warning")
        return result

    @app.get("/api/updates")
    def application_updates():
        return check_application_update(APPLICATION_VERSION, bool(state.settings.app_update_checks), source_url=state.settings.update_source_url)

    @app.get("/api/updates/last")
    def last_application_update_check():
        return last_update_check()

    @app.get("/api/version")
    def version():
        return {"application": "VideoHaul", "version": APPLICATION_VERSION}

    @app.get("/api/batch")
    def get_batch():
        return batch.snapshot()

    @app.post("/api/batch/analyze")
    def analyze_batch(request: BatchAnalyzeRequest):
        try:
            return batch.analyze(
                request.text,
                request.range_spec,
                request.requested_quality,
                request.requested_audio_languages,
                request.requested_subtitle_languages,
                request.shared_settings,
                request.filename_template,
            )
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(400, str(exc))

    @app.patch("/api/batch/selection")
    def update_batch_selection(request: BatchSelectionRequest):
        try:
            return batch.update_selection(request.indexes, request.range_spec, request.mode)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.patch("/api/batch/configure")
    def configure_batch(request: BatchConfigureRequest):
        try:
            return batch.configure(request.values)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.patch("/api/batch/items/{item_id}")
    def resolve_batch_item(item_id: str, request: BatchResolveRequest):
        try:
            return batch.resolve_item(item_id, request.action, request.selected_video, request.selected_audio, request.selected_subtitles)
        except KeyError:
            raise HTTPException(404, "Batch item not found")
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/batch/items/{item_id}/reorder")
    def reorder_batch_item(item_id: str, request: ReorderRequest):
        try:
            return batch.reorder(item_id, request.index)
        except KeyError:
            raise HTTPException(404, "Batch item not found")

    @app.post("/api/batch/commit")
    def commit_batch():
        try:
            result = batch.commit()
            jobs.refresh_scheduler()
            return result
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.delete("/api/batch")
    def reset_batch():
        return batch.reset()

    @app.post("/api/jobs")
    def add_job(request: AddJobRequest):
        return job_to_dict(state.add_job(request.url))

    @app.post("/api/jobs/smart-add")
    def smart_add(request: SmartAddRequest):
        urls = extract_urls(request.text)
        existing = {job.source_url.casefold() for job in state.jobs if job.source_url}
        added = []
        duplicates = []
        for url in urls:
            if not request.add_duplicates and url.casefold() in existing:
                duplicates.append(url)
                continue
            added.append(job_to_dict(state.add_job(url)))
            existing.add(url.casefold())
        return {"jobs": added, "duplicates": duplicates, "recognized": len(urls)}

    @app.post("/api/jobs/bulk")
    def bulk_jobs(request: BulkRequest):
        allowed = {"start", "pause", "resume", "stop", "retry", "delete", "collapse", "expand", "pin", "unpin", "settings"}
        if request.action not in allowed:
            raise HTTPException(400, "Unsupported bulk action")
        requested_settings = dict(request.values or {})
        bulk_setting_names = {"destination", "speed_limit_bps", "priority"}
        if request.action == "settings":
            unknown = set(requested_settings) - bulk_setting_names
            if unknown or not requested_settings:
                raise HTTPException(400, "Bulk settings may change destination, speed limit, or priority")
        results = []
        for job_id in dict.fromkeys(request.job_ids):
            try:
                job = state.get_job(job_id)
                if request.action == "start":
                    jobs.start(job_id)
                elif request.action == "pause" and job.status == JobStatus.DOWNLOADING:
                    jobs.pause(job_id)
                elif request.action == "resume" and job.status == JobStatus.PAUSED:
                    jobs.resume(job_id)
                elif request.action == "stop" and job.status in {JobStatus.ANALYZING, JobStatus.DOWNLOADING, JobStatus.PAUSED, JobStatus.FINALIZING, JobStatus.QUEUED}:
                    if job.status == JobStatus.ANALYZING:
                        analyzer.cancel(job_id)
                    else:
                        jobs.stop(job_id)
                elif request.action == "retry" and job.status in RETRYABLE_STATUSES:
                    if job.status in ANALYSIS_FAILURE_STATUSES:
                        analyzer.analyze(job_id, refresh=True)
                    else:
                        jobs.retry(job_id)
                elif request.action == "delete":
                    if job.status == JobStatus.ANALYZING:
                        analyzer.cancel(job_id)
                        state.delete_job(job_id)
                    else:
                        jobs.delete(job_id)
                elif request.action in {"collapse", "expand"}:
                    state.set_collapsed(job_id, request.action == "collapse")
                elif request.action in {"pin", "unpin"}:
                    state.update_job(job_id, pinned=request.action == "pin")
                elif request.action == "settings":
                    if "destination" in requested_settings and job.status in {JobStatus.ANALYZING, JobStatus.DOWNLOADING, JobStatus.PAUSED, JobStatus.FINALIZING, JobStatus.DONE}:
                        raise RuntimeError("Stop this job before changing its destination")
                    values = {name: getattr(job.settings, name) for name in JobSettings.__dataclass_fields__}
                    values.update(requested_settings)
                    updated = JobSettings(**values).normalized()
                    state.update_job(job_id, settings=updated)
                    if "destination" in requested_settings:
                        state.remember_folder(updated.destination)
                results.append({"job_id": job_id, "ok": True})
            except Exception as exc:
                results.append({"job_id": job_id, "ok": False, "detail": str(exc)})
        jobs.refresh_scheduler()
        return {"results": results}

    @app.post("/api/jobs/{job_id}/duplicate")
    def duplicate_job(job_id: str):
        try:
            source = state.get_job(job_id)
            created = state.add_job("", deepcopy(source.settings))
            return job_to_dict(created)
        except KeyError:
            raise HTTPException(404, "Job not found")

    @app.post("/api/jobs/{job_id}/reset-settings")
    def reset_job_settings(job_id: str):
        try:
            job = state.get_job(job_id)
            if job.status in {JobStatus.ANALYZING, JobStatus.DOWNLOADING, JobStatus.PAUSED, JobStatus.FINALIZING, JobStatus.QUEUED}:
                raise ValueError("Stop this job before resetting its settings")
            settings = state.default_job_settings()
            state.update_job(job_id, settings=settings)
            jobs.refresh_scheduler()
            return job_to_dict(job)
        except KeyError:
            raise HTTPException(404, "Job not found")
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @app.patch("/api/jobs/{job_id}")
    def update_job(job_id: str, request: JobUpdateRequest):
        try:
            job = state.get_job(job_id)
            if request.source_url is not None and request.source_url.strip() != str(job.source_url or "").strip():
                if job.status in {JobStatus.ANALYZING, JobStatus.DOWNLOADING, JobStatus.PAUSED, JobStatus.FINALIZING, JobStatus.QUEUED}:
                    raise ValueError("Stop this job before changing its URL")
                job.source_url = request.source_url.strip()
                job.analysis = None
                job.status = JobStatus.UNANALYZED
                job.failure_reason = ""
                job.progress = ProgressState()
            if request.selected_video is not None:
                if job.status in {JobStatus.ANALYZING, JobStatus.DOWNLOADING, JobStatus.PAUSED, JobStatus.FINALIZING, JobStatus.QUEUED} and request.selected_video != job.selected_video:
                    raise ValueError("Stop this job before changing its selected stream")
                if request.selected_video == AUDIO_ONLY_SELECTOR and (not job.analysis or not job.analysis.audio_streams):
                    raise ValueError("Audio-only output is unavailable for this media")
                job.selected_video = request.selected_video
            if request.collapsed is not None:
                job.collapsed = request.collapsed
            if request.pinned is not None:
                job.pinned = request.pinned
            if request.settings is not None:
                values = job.settings.__dict__ if hasattr(job.settings, "__dict__") else {name: getattr(job.settings, name) for name in job.settings.__dataclass_fields__}
                unknown = set(request.settings) - set(JobSettings.__dataclass_fields__)
                if unknown:
                    raise ValueError(f"Unknown job settings: {', '.join(sorted(unknown))}")
                if job.status in {JobStatus.ANALYZING, JobStatus.DOWNLOADING, JobStatus.PAUSED, JobStatus.FINALIZING, JobStatus.QUEUED}:
                    changed = {name for name, value in request.settings.items() if value != values.get(name)}
                    unsafe = changed - {"speed_limit_bps", "priority"}
                    if unsafe:
                        raise ValueError("Stop this job before changing active transfer settings")
                values.update(request.settings)
                candidate = JobSettings(**values).normalized()
                audio_streams = job.analysis.audio_streams if job.analysis else []
                subtitle_tracks = job.analysis.subtitles if job.analysis else []
                audio_mode, selected_audio, audio_output = validate_audio_selection(candidate.audio_selection_mode, candidate.selected_audio, candidate.audio_output_format, audio_streams)
                subs_enabled, selected_subtitles, subtitle_mode, subtitle_format = validate_subtitle_selection(candidate.subtitles_enabled, candidate.selected_subtitles, candidate.subtitle_mode, candidate.subtitle_format, subtitle_tracks)
                candidate.audio_selection_mode = audio_mode
                candidate.selected_audio = selected_audio
                candidate.audio_output_format = audio_output
                candidate.subtitles_enabled = subs_enabled
                candidate.selected_subtitles = selected_subtitles
                candidate.subtitle_mode = subtitle_mode
                candidate.subtitle_format = subtitle_format
                job.settings = candidate
            state.update_job(job_id, source_url=job.source_url, analysis=job.analysis, status=job.status, failure_reason=job.failure_reason, progress=job.progress, selected_video=job.selected_video, collapsed=job.collapsed, pinned=job.pinned, settings=job.settings)
            jobs.refresh_scheduler()
            return job_to_dict(job)
        except KeyError:
            raise HTTPException(404, "Job not found")
        except (TypeError, ValueError) as exc:
            raise HTTPException(400, str(exc))

    @app.delete("/api/jobs/{job_id}")
    def delete_job(job_id: str):
        try:
            job = state.get_job(job_id)
            if job.status == JobStatus.ANALYZING:
                analyzer.cancel(job_id)
                state.delete_job(job_id)
            else:
                jobs.delete(job_id)
            return {"ok": True}
        except KeyError:
            raise HTTPException(404, "Job not found")
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/jobs/{job_id}/analyze")
    def analyze_job(job_id: str, refresh: bool = False):
        try:
            analyzer.analyze(job_id, refresh=refresh)
            return {"ok": True}
        except KeyError:
            raise HTTPException(404, "Job not found")
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/jobs/{job_id}/start")
    def start_job(job_id: str):
        try:
            jobs.start(job_id)
            return {"ok": True}
        except KeyError:
            raise HTTPException(404, "Job not found")
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/jobs/{job_id}/pause")
    def pause_job(job_id: str):
        try:
            jobs.pause(job_id)
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/jobs/{job_id}/resume")
    def resume_job(job_id: str):
        try:
            jobs.resume(job_id)
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/jobs/{job_id}/stop")
    def stop_job(job_id: str):
        try:
            job = state.get_job(job_id)
            if job.status == JobStatus.ANALYZING:
                analyzer.cancel(job_id)
            else:
                jobs.stop(job_id)
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str):
        try:
            job = state.get_job(job_id)
            if job.status in ANALYSIS_FAILURE_STATUSES:
                analyzer.analyze(job_id, refresh=True)
            else:
                jobs.retry(job_id)
            return {"ok": True}
        except Exception as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/jobs/{job_id}/open")
    def open_job(job_id: str):
        try:
            return {"ok": jobs.open_media(job_id)}
        except KeyError:
            raise HTTPException(404, "Job not found")

    @app.post("/api/jobs/{job_id}/open-folder")
    def open_job_folder(job_id: str, reveal: bool = False):
        try:
            info = destination_info(state.get_job(job_id))
            return {"ok": jobs.open_folder(job_id, reveal), "destination": info["destination"], "available": info["available"], "reason": info["reason"]}
        except KeyError:
            raise HTTPException(404, "Job not found")

    @app.delete("/api/jobs/{job_id}/output")
    def delete_job_output(job_id: str):
        try:
            return {"ok": jobs.delete_output(job_id)}
        except KeyError:
            raise HTTPException(404, "Job not found")
        except RuntimeError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/api/jobs/{job_id}/partial-cleanup")
    def resolve_partial_cleanup(job_id: str, request: PartialCleanupRequest):
        try:
            return jobs.resolve_partial_cleanup(job_id, request.action)
        except KeyError:
            raise HTTPException(404, "Job not found")
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @app.post("/api/jobs/{job_id}/reorder")
    def reorder_job(job_id: str, request: ReorderRequest):
        try:
            state.reorder(job_id, request.index)
            return {"ok": True}
        except KeyError:
            raise HTTPException(404, "Job not found")

    @app.post("/api/jobs/pause-all")
    def pause_all():
        jobs.pause_all()
        return {"ok": True}

    @app.post("/api/jobs/resume-all")
    def resume_all():
        jobs.resume_all()
        return {"ok": True}

    @app.post("/api/jobs/stop-all")
    def stop_all():
        for item in list(state.jobs):
            if item.status == JobStatus.ANALYZING:
                analyzer.cancel(item.job_id)
        jobs.stop_all()
        return {"ok": True}

    @app.post("/api/collapse-all/{value}")
    def collapse_all(value: bool):
        state.collapse_all(value)
        return {"ok": True}

    @app.post("/api/jobs/{job_id}/detect")
    def detect_job_media(job_id: str, mode: str = "interactive"):
        try:
            job = state.get_job(job_id)
        except KeyError:
            raise HTTPException(404, "Job not found")
        if not str(job.source_url or "").strip():
            raise HTTPException(400, "Enter a media URL")
        if not playwright_available():
            raise HTTPException(409, "Managed Chromium automation runtime is unavailable")
        try:
            session = detector_service.open(job.source_url, job_id, mode)
        except DetectorUnavailable as exc:
            raise HTTPException(409, str(exc) or "Managed Chromium automation runtime is unavailable")
        state.update_job(job_id, detector_session_id=session.session_id)
        return session.snapshot()

    @app.patch("/api/settings")
    def update_settings(request: SettingsRequest):
        updated = state.update_settings(request.values)
        jobs.refresh_scheduler()
        return updated.to_dict()

    return app


def session_cookie_name(allowed_hosts: set[str] | None) -> str:
    ports = sorted({value.rsplit(":", 1)[-1] for value in (allowed_hosts or set()) if ":" in value})
    return f"videohaul_session_{ports[0]}" if ports else "videohaul_session"


SENSITIVE_SETTING_KEYS = {"browser_cookies", "referer", "user_agent"}


def _exportable_settings(values: dict) -> dict:
    return {key: value for key, value in dict(values or {}).items() if key not in SENSITIVE_SETTING_KEYS}


def _stream_from_dict(data: dict):
    from ..models import VideoStream
    return VideoStream(**{key: value for key, value in data.items() if key in VideoStream.__dataclass_fields__})


def job_to_plain_stream(stream):
    from dataclasses import asdict
    value = asdict(stream)
    value["direct_stream_available"] = bool(value.get("direct_stream_url"))
    value["direct_stream_url"] = ""
    return value


def _audio_from_dict(data: dict):
    from ..models import AudioStream
    return AudioStream(**{key: value for key, value in data.items() if key in AudioStream.__dataclass_fields__})

def job_to_plain_audio(stream):
    from dataclasses import asdict
    value = asdict(stream)
    value["direct_stream_available"] = bool(value.get("direct_stream_url"))
    value["direct_stream_url"] = ""
    return value


def _subtitle_from_dict(data):
    from ..models import SubtitleTrack
    return SubtitleTrack(**dict(data or {}))

def _open_path(path: Path, folder: bool = False) -> bool:
    target = path if folder else path
    if folder and not target.is_dir():
        return False
    if not folder and not target.is_file():
        return False
    if os.name == "nt":
        os.startfile(str(target))
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])
    return True
