from __future__ import annotations

import os
from pathlib import Path
import socket
import re
import threading
import time
import urllib.error
import urllib.request

from .contracts import TransferResult
from ..file_policy import cleanup_private_staging, path_is_within, private_staging_directory, publish_staged_file, render_output_template, resolve_collision
from ..filesystem import organized_destination
from ..http_security import build_scoped_opener, scope_headers
from ..quality import AUDIO_ONLY_SELECTOR

READ_CHUNK_BYTES = 256 * 1024
REQUEST_TIMEOUT_SECONDS = 30.0


def selected_direct_stream(job):
    analysis = getattr(job, "analysis", None)
    if analysis is None:
        return None
    if str(getattr(job, "selected_video", "") or "") == AUDIO_ONLY_SELECTOR:
        selected_audio = {str(value) for value in getattr(job.settings, "selected_audio", []) if str(value)}
        for item in analysis.audio_streams:
            if not selected_audio or item.stream_id in selected_audio:
                metadata = dict(getattr(item, "source_metadata", None) or {})
                if getattr(item, "direct_stream_url", None) and (metadata.get("direct_transfer") or (analysis.metadata or {}).get("direct_transfer")):
                    return item
        return None
    stream = next((item for item in analysis.video_streams if item.stream_id == job.selected_video), None)
    if stream is None:
        return None
    metadata = dict(getattr(stream, "source_metadata", None) or {})
    if getattr(stream, "direct_stream_url", None) and (metadata.get("direct_transfer") or (analysis.metadata or {}).get("direct_transfer")):
        return stream
    return None


def is_direct_file_job(job) -> bool:
    stream = selected_direct_stream(job)
    if stream is None:
        return False
    metadata = dict(getattr(stream, "source_metadata", None) or {})
    return not str(metadata.get("manifest_kind") or "").strip()


class DirectHttpTransfer:
    """Byte-for-byte HTTP transfer for a Media Detector direct-file candidate.

    This deliberately bypasses yt-dlp extraction. The exact discovered media URL is
    requested with the private request context carried by the detector job.
    """

    def __init__(self, job, on_progress, on_stage, on_diagnostic=None, opener=None):
        self.job = job
        self.on_progress = on_progress
        self.on_stage = on_stage
        self.on_diagnostic = on_diagnostic or (lambda kind, message, payload, level="info": None)
        self._opener = opener
        self._lock = threading.RLock()
        self._pause_requested = threading.Event()
        self._stopped = threading.Event()
        self._response = None
        self.current_path = str(job.partial_path or "")
        self.output_path = ""
        self.resume_state = "supported"
        self.applied_speed_limit_bps = job.effective_speed_limit_bps
        self._staging_dir: Path | None = None
        self._final_path: Path | None = None

    def _stream(self):
        stream = selected_direct_stream(self.job)
        if stream is None:
            raise RuntimeError("The selected detector stream does not contain a direct media URL")
        return stream

    def _headers(self, target_url: str = "") -> dict[str, str]:
        headers: dict[str, str] = {}
        for name, value in dict(getattr(self.job, "transfer_headers", None) or {}).items():
            key = str(name or "").strip()
            val = str(value or "").strip()
            if key and val:
                headers[key] = val
        metadata = dict(self.job.analysis.metadata or {}) if self.job.analysis else {}
        if self.job.settings.referer and not any(key.casefold() == "referer" for key in headers):
            headers["Referer"] = str(self.job.settings.referer)
        elif metadata.get("referer") and not any(key.casefold() == "referer" for key in headers):
            headers["Referer"] = str(metadata["referer"])
        if self.job.settings.user_agent and not any(key.casefold() == "user-agent" for key in headers):
            headers["User-Agent"] = str(self.job.settings.user_agent)
        elif metadata.get("user_agent") and not any(key.casefold() == "user-agent" for key in headers):
            headers["User-Agent"] = str(metadata["user_agent"])
        origin = str(metadata.get("_transfer_origin") or target_url or "")
        return scope_headers(headers, target_url or origin, origin)

    def _paths(self) -> tuple[Path, Path]:
        destination = organized_destination(self.job)
        destination.mkdir(parents=True, exist_ok=True)
        rendered = render_output_template(self.job)
        if "%(" in rendered:
            raise RuntimeError("The output filename could not be resolved for the detected media")
        if str(self.job.settings.collision_policy or "rename").lower() == "ask":
            resolve_collision(destination, rendered, "ask")
        final = destination / rendered
        staging = private_staging_directory(destination, self.job.job_id)
        self._staging_dir = staging
        self._final_path = final
        if self.current_path and path_is_within(self.current_path, staging):
            partial = Path(self.current_path)
            partial.parent.mkdir(parents=True, exist_ok=True)
            return final, partial
        if self.current_path:
            self.on_diagnostic("legacy_partial_ignored", "Persisted partial path was outside this job's private staging directory", {"partial_path": self.current_path}, "warning")
        partial = staging / rendered
        partial.parent.mkdir(parents=True, exist_ok=True)
        self.current_path = str(partial)
        return final, partial

    def _effective_limit(self) -> int | None:
        values = [
            value for value in (self.job.settings.speed_limit_bps, self.job.effective_speed_limit_bps)
            if value is not None and int(value) > 0
        ]
        return min(map(int, values)) if values else None

    def _http_error_result(self, exc: urllib.error.HTTPError) -> TransferResult:
        code = int(getattr(exc, "code", 0) or 0)
        reason = str(getattr(exc, "reason", "") or "").strip()
        detail = f"HTTP {code}{': ' + reason if reason else ''} while requesting the detected media URL"
        if code == 401:
            component, retryable = "authentication", False
        elif code == 403:
            component, retryable = "access", False
        elif code in {404, 410}:
            component, retryable = "unavailable", False
        elif code == 429 or code >= 500:
            component, retryable = "network", True
        else:
            component, retryable = "access", False
        self.on_diagnostic(
            "direct_http_access_failed",
            "Direct media request failed",
            {"status_code": code, "component": component, "retryable": retryable},
            "warning" if retryable else "error",
        )
        return TransferResult(1, detail, component=component, retryable=retryable)

    def run(self) -> TransferResult:
        if self._stopped.is_set():
            return TransferResult(1, "Stopped", stopped=True)
        if self._pause_requested.is_set():
            return TransferResult(1, "Paused", paused=True, restart=True)
        stream = self._stream()
        url = str(stream.direct_stream_url or "")
        if not url:
            return TransferResult(1, "Detected media URL is empty", component="download", retryable=False)
        final, partial = self._paths()
        existing = partial.stat().st_size if partial.is_file() else 0
        headers = self._headers(url)
        request = urllib.request.Request(url, method="GET")
        for name, value in headers.items():
            request.add_header(name, value)
        if existing > 0:
            request.add_header("Range", f"bytes={existing}-")
        self.on_stage("preparing")
        self.on_diagnostic(
            "direct_http_started",
            "Starting direct Media Detector transfer",
            {
                "header_names": sorted(headers),
                "has_request_context": bool(headers),
                "resume_offset": existing,
                "candidate_id": str((self.job.analysis.metadata or {}).get("candidate_id") or "") if self.job.analysis else "",
            },
        )
        start = time.monotonic()
        downloaded = existing
        try:
            metadata = dict(self.job.analysis.metadata or {}) if self.job.analysis else {}
            origin = str(metadata.get("_transfer_origin") or url)
            guarded = bool(metadata.get("candidate_id") or metadata.get("resolver") == "browser_media")
            open_url = self._opener or build_scoped_opener(url, origin, guarded).open
            response = open_url(request, timeout=REQUEST_TIMEOUT_SECONDS)
            with self._lock:
                self._response = response
            with response:
                status = int(getattr(response, "status", 0) or 0)
                content_length = response.headers.get("Content-Length")
                try:
                    response_length = int(content_length) if content_length else None
                except (TypeError, ValueError):
                    response_length = None
                expected_response_length = response_length
                exact_total = None
                if existing and status == 206:
                    content_range = str(response.headers.get("Content-Range") or "")
                    match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range, re.IGNORECASE)
                    if match is None or int(match.group(1)) != existing:
                        return TransferResult(1, "Server returned an invalid range response for the saved partial file", component="network", retryable=True)
                    range_start = int(match.group(1))
                    range_end = int(match.group(2))
                    if range_end < range_start:
                        return TransferResult(1, "Server returned an invalid byte range", component="network", retryable=True)
                    expected_response_length = range_end - range_start + 1
                    if response_length is not None and response_length != expected_response_length:
                        return TransferResult(1, "Server returned contradictory range length metadata", component="network", retryable=True)
                    if match.group(3).isdigit():
                        exact_total = int(match.group(3))
                        if range_end + 1 != exact_total:
                            return TransferResult(1, "Server returned a partial range that did not extend to the end of the media", component="network", retryable=True)
                if existing and status != 206:
                    existing = 0
                    downloaded = 0
                stream_total = getattr(stream, "filesize_bytes", None)
                stream_total_exact = bool(stream_total and not bool(getattr(stream, "filesize_is_estimate", False)))
                total = exact_total or ((existing + expected_response_length) if expected_response_length is not None else stream_total)
                mode = "ab" if existing and status == 206 else "wb"
                partial.parent.mkdir(parents=True, exist_ok=True)
                self.current_path = str(partial)
                self.on_stage("downloading_audio" if self.job.selected_video == AUDIO_ONLY_SELECTOR else "downloading_video")
                with partial.open(mode) as handle:
                    while True:
                        if self._stopped.is_set():
                            return TransferResult(1, "Stopped", stopped=True)
                        if self._pause_requested.is_set():
                            return TransferResult(1, "Paused", paused=True, restart=True)
                        chunk = response.read(READ_CHUNK_BYTES)
                        if self._pause_requested.is_set():
                            return TransferResult(1, "Paused", paused=True, restart=True)
                        if not chunk:
                            break
                        handle.write(chunk)
                        downloaded += len(chunk)
                        elapsed = max(0.001, time.monotonic() - start)
                        speed = max(0.0, (downloaded - existing) / elapsed)
                        eta = max(0.0, (total - downloaded) / speed) if total and speed > 0 and downloaded <= total else None
                        self.on_progress(downloaded, total, speed, eta, elapsed, partial_path=self.current_path, resume_state=self.resume_state)
                        limit = self._effective_limit()
                        if limit:
                            expected_elapsed = max(0.0, (downloaded - existing) / limit)
                            delay = expected_elapsed - elapsed
                            if delay > 0:
                                time.sleep(min(delay, 0.5))
                    handle.flush()
                    os.fsync(handle.fileno())
            with self._lock:
                self._response = None
            if self._stopped.is_set():
                return TransferResult(1, "Stopped", stopped=True)
            if self._pause_requested.is_set():
                return TransferResult(1, "Paused", paused=True, restart=True)
            size = partial.stat().st_size if partial.is_file() else 0
            if size <= 0:
                return TransferResult(1, "Direct transfer produced an empty output file", component="download", retryable=False)
            if expected_response_length is not None and downloaded != existing + expected_response_length:
                return TransferResult(1, f"Direct transfer ended early at {downloaded} of {existing + expected_response_length} bytes", component="network", retryable=True)
            if exact_total is not None and downloaded != exact_total:
                return TransferResult(1, f"Direct transfer ended at {downloaded} of {exact_total} bytes", component="network", retryable=True)
            if expected_response_length is None and stream_total_exact and downloaded != int(stream_total):
                return TransferResult(1, f"Direct transfer ended at {downloaded} of {int(stream_total)} bytes", component="network", retryable=True)
            self.output_path = str(partial)
            elapsed = max(0.001, time.monotonic() - start)
            self.on_progress(size, size, 0.0, 0.0, elapsed, partial_path=self.current_path, resume_state=self.resume_state)
            self.on_diagnostic("direct_http_staged", "Direct Media Detector transfer completed in private staging", {"staged_path": str(partial), "bytes": size})
            return TransferResult(0, output_path=str(partial))
        except urllib.error.HTTPError as exc:
            with self._lock:
                self._response = None
            if self._pause_requested.is_set():
                return TransferResult(1, "Paused", paused=True, restart=True)
            if self._stopped.is_set():
                return TransferResult(1, "Stopped", stopped=True)
            return self._http_error_result(exc)
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            with self._lock:
                self._response = None
            if self._pause_requested.is_set():
                return TransferResult(1, "Paused", paused=True, restart=True)
            if self._stopped.is_set():
                return TransferResult(1, "Stopped", stopped=True)
            reason = getattr(exc, "reason", exc)
            detail = f"Network error while requesting the detected media URL: {reason}"
            self.on_diagnostic("direct_http_network_failed", "Direct media network request failed", {"error": str(reason)}, "warning")
            return TransferResult(1, detail, component="network", retryable=True)
        except (PermissionError, OSError) as exc:
            with self._lock:
                self._response = None
            if self._pause_requested.is_set():
                return TransferResult(1, "Paused", paused=True, restart=True)
            if self._stopped.is_set():
                return TransferResult(1, "Stopped", stopped=True)
            filename = str(getattr(exc, "filename", "") or "")
            component = "filesystem" if filename and str(partial.parent) in filename else "network"
            return TransferResult(1, str(exc) or "Direct transfer failed", component=component, retryable=component == "network")
        except Exception as exc:
            with self._lock:
                self._response = None
            if self._pause_requested.is_set():
                return TransferResult(1, "Paused", paused=True, restart=True)
            if self._stopped.is_set():
                return TransferResult(1, "Stopped", stopped=True)
            return TransferResult(1, str(exc) or "Direct transfer failed", component="download", retryable=False)


    def publish(self, staged_path: str) -> str:
        if self._staging_dir is None or self._final_path is None:
            raise RuntimeError("Direct transfer publication state is unavailable")
        produced = Path(staged_path)
        published = publish_staged_file(produced, self._final_path.parent, self.job.settings.collision_policy, self._final_path.name, self._staging_dir)
        cleanup_private_staging(self._staging_dir, self._final_path.parent, self.job.job_id)
        self.current_path = ""
        self.output_path = str(published)
        self.on_diagnostic("direct_http_published", "Validated direct transfer published", {"output_path": str(published)})
        return str(published)

    def pause(self) -> str:
        if self._stopped.is_set() or self._pause_requested.is_set():
            return "inactive"
        self._pause_requested.set()
        with self._lock:
            response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        return "restart"

    def resume(self) -> str:
        return "restart" if self._pause_requested.is_set() else "inactive"

    def stop(self) -> None:
        self._stopped.set()
        with self._lock:
            response = self._response
        if response is not None:
            try:
                response.close()
            except Exception:
                pass

    def reconfigure_speed_limit(self, value) -> bool:
        self.applied_speed_limit_bps = value
        self.job.effective_speed_limit_bps = value
        return False
