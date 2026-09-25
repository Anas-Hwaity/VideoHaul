from __future__ import annotations

import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time

from .contracts import TransferResult
from .http_direct import selected_direct_stream
from ..dependencies import ensure_ffmpeg
from ..file_policy import cleanup_private_staging, path_is_within, private_staging_directory, publish_staged_file, render_output_template, resolve_collision
from ..filesystem import organized_destination
from ..http_security import GuardedMediaRelay, scope_headers
from ..quality import AUDIO_ONLY_SELECTOR
from ..unicode_text import decode_external_bytes


def is_direct_manifest_job(job) -> bool:
    stream = selected_direct_stream(job)
    if stream is None:
        return False
    metadata = dict(getattr(stream, "source_metadata", None) or {})
    return bool(str(metadata.get("manifest_kind") or "").strip())


class DirectManifestTransfer:
    """Transfer detector-discovered HLS/DASH media through a guarded local relay."""

    def __init__(self, job, on_progress, on_stage, on_diagnostic=None):
        self.job = job
        self.on_progress = on_progress
        self.on_stage = on_stage
        self.on_diagnostic = on_diagnostic or (lambda kind, message, payload, level="info": None)
        self.process: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._pause_requested = False
        self._stopped = False
        self.current_path = str(job.partial_path or "")
        self.output_path = ""
        self.resume_state = "restart_required"
        self.applied_speed_limit_bps = job.effective_speed_limit_bps
        self._staging_dir: Path | None = None
        self._final_path: Path | None = None
        self._reconfigure = False
        self._relay: GuardedMediaRelay | None = None

    def _stream(self):
        stream = selected_direct_stream(self.job)
        if stream is None:
            raise RuntimeError("The selected detector manifest does not contain a direct media URL")
        return stream


    def _requires_guarded_relay(self) -> bool:
        metadata = dict(self.job.analysis.metadata or {}) if self.job.analysis else {}
        stream_metadata = dict(getattr(self._stream(), "source_metadata", None) or {})
        return bool(stream_metadata.get("detector_guarded") or metadata.get("candidate_id") or metadata.get("resolver") == "browser_media")

    def _verification_state(self) -> str:
        stream_metadata = dict(getattr(self._stream(), "source_metadata", None) or {})
        return str(stream_metadata.get("verification_state") or "").strip().casefold()

    def _headers(self) -> dict[str, str]:
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
        stream = self._stream()
        target = str(stream.direct_stream_url or "")
        origin = str(metadata.get("_transfer_origin") or target)
        return scope_headers(headers, target, origin)

    def _paths(self) -> tuple[Path, Path]:
        destination = organized_destination(self.job)
        destination.mkdir(parents=True, exist_ok=True)
        rendered = render_output_template(self.job)
        if "%(" in rendered:
            raise RuntimeError("The output filename could not be resolved for the detected manifest")
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
        staged = staging / rendered
        partial = staged.with_name(staged.stem + ".part" + staged.suffix)
        partial.parent.mkdir(parents=True, exist_ok=True)
        self.current_path = str(partial)
        return final, partial

    def _ffmpeg(self) -> str:
        directory = Path(ensure_ffmpeg())
        name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        return str(directory / name)

    def build_command(self) -> tuple[list[str], Path, Path]:
        stream = self._stream()
        url = str(stream.direct_stream_url or "")
        if not url:
            raise RuntimeError("Detected manifest URL is empty")
        final, partial = self._paths()
        headers = self._headers()
        args = [self._ffmpeg(), "-hide_banner", "-nostdin", "-y"]
        if self._requires_guarded_relay():
            if self._verification_state() not in {"verified", "http_verified"}:
                raise RuntimeError("Detected manifest must pass public-network verification before transfer")
            if self._relay is None:
                raise RuntimeError("Guarded manifest relay is unavailable")
            url = self._relay.url(url)
            headers = {}
        if headers:
            args.extend(["-headers", "".join(f"{name}: {value}\r\n" for name, value in headers.items())])
        args.extend([
            "-i", url,
            "-map", "0:v?",
            "-map", "0:a?",
            "-map", "0:s?",
            "-c", "copy",
            "-progress", "pipe:1",
            "-nostats",
            str(partial),
        ])
        return args, final, partial

    def _failure(self, stderr: str) -> TransferResult:
        text = str(stderr or "").strip()
        lowered = text.casefold()
        status_match = re.search(r"(?:http[^\d]{0,12}|server returned\s+)([45]\d\d)", lowered)
        status = int(status_match.group(1)) if status_match else 0
        if status == 401 or "401 unauthorized" in lowered:
            component, retryable = "authentication", False
        elif status == 403 or "403 forbidden" in lowered:
            component, retryable = "access", False
        elif status in {404, 410}:
            component, retryable = "unavailable", False
        elif status == 429 or status >= 500:
            component, retryable = "network", True
        elif any(token in lowered for token in ("timed out", "connection reset", "connection refused", "temporary failure", "failed to resolve")):
            component, retryable = "network", True
        else:
            component, retryable = "download", False
        detail = (text.splitlines()[-1] if text else "ffmpeg could not transfer the detected manifest")
        if status and f"{status}" not in detail:
            detail = f"HTTP {status}: {detail}"
        return TransferResult(1, detail, component=component, retryable=retryable)

    def run(self) -> TransferResult:
        with self._lock:
            if self._stopped:
                return TransferResult(1, "Stopped", stopped=True)
            if self._pause_requested:
                return TransferResult(1, "Paused", paused=True, restart=True)
        try:
            if self._requires_guarded_relay():
                metadata = dict(self.job.analysis.metadata or {}) if self.job.analysis else {}
                stream = self._stream()
                target = str(stream.direct_stream_url or "")
                origin = str(metadata.get("_transfer_origin") or target)
                self._relay = GuardedMediaRelay(self._headers(), origin)
                self._relay.start()
            args, final, partial = self.build_command()
        except Exception as exc:
            return TransferResult(1, str(exc), component="dependency" if "FFmpeg" in str(exc) else "download", retryable=False)
        headers = self._headers()
        stream = self._stream()
        self.on_stage("preparing")
        self.on_stage("downloading_audio" if self.job.selected_video == AUDIO_ONLY_SELECTOR else "downloading_video")
        self.on_diagnostic(
            "direct_manifest_started",
            "Starting direct Media Detector manifest transfer",
            {
                "manifest_kind": str((getattr(stream, "source_metadata", None) or {}).get("manifest_kind") or ""),
                "header_names": sorted(headers),
                "has_request_context": bool(headers),
                "candidate_id": str((self.job.analysis.metadata or {}).get("candidate_id") or "") if self.job.analysis else "",
            },
        )
        started = time.monotonic()
        try:
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            with self._lock:
                if self._stopped:
                    return TransferResult(1, "Stopped", stopped=True)
                if self._pause_requested:
                    return TransferResult(1, "Paused", paused=True, restart=True)
                self.process = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    creationflags=creationflags,
                    start_new_session=(os.name != "nt"),
                )
            stderr_chunks = []
            def read_stderr() -> None:
                stream = self.process.stderr if self.process is not None else None
                if stream is None:
                    return
                while True:
                    chunk = stream.read(8192)
                    if not chunk:
                        break
                    stderr_chunks.append(chunk)
                    if sum(len(item) for item in stderr_chunks) > 262144:
                        del stderr_chunks[:-8]
            stderr_thread = threading.Thread(target=read_stderr, daemon=True, name=f"videohaul-ffmpeg-stderr-{self.job.job_id[:8]}")
            stderr_thread.start()
            progress_values = {}
            stdout_stream = self.process.stdout
            while stdout_stream is not None:
                raw = stdout_stream.readline()
                if not raw:
                    if self.process.poll() is not None:
                        break
                    continue
                text = decode_external_bytes(raw).strip()
                if "=" not in text:
                    continue
                key, value = text.split("=", 1)
                progress_values[key] = value
                if key != "progress":
                    continue
                elapsed = max(0.001, time.monotonic() - started)
                size = partial.stat().st_size if partial.is_file() else 0
                speed = size / elapsed
                total = getattr(stream, "filesize_bytes", None)
                eta = max(0.0, (int(total) - size) / speed) if total and speed > 0 and size <= int(total) else None
                self.on_progress(size, int(total) if total else None, speed, eta, elapsed, partial_path=self.current_path, resume_state=self.resume_state, total_is_estimate=bool(getattr(stream, "filesize_is_estimate", False)))
            returncode = int(self.process.wait() or 0)
            stderr_thread.join(timeout=2.0)
            stderr_text = decode_external_bytes(b"".join(stderr_chunks))
            if self._reconfigure:
                return TransferResult(returncode or 1, "Bandwidth limit changed; restarting transfer with a rate-limited engine", component="bandwidth", restart=True)
            if self._stopped:
                return TransferResult(1, "Stopped", stopped=True)
            if self._pause_requested:
                return TransferResult(1, "Paused", paused=True, restart=True)
            if returncode != 0:
                result = self._failure(stderr_text)
                self.on_diagnostic(
                    "direct_manifest_failed",
                    "Direct manifest transfer failed",
                    {"component": result.component, "retryable": result.retryable},
                    "warning" if result.retryable else "error",
                )
                return result
            if not partial.is_file() or partial.stat().st_size <= 0:
                return TransferResult(1, "ffmpeg reported success but did not create a usable detected-media output", component="filesystem", retryable=False)
            self.current_path = str(partial)
            self.output_path = str(partial)
            elapsed = max(0.001, time.monotonic() - started)
            size = partial.stat().st_size
            self.on_progress(size, size, 0.0, 0.0, elapsed, partial_path=self.current_path, resume_state=self.resume_state)
            self.on_diagnostic("direct_manifest_staged", "Direct manifest transfer completed in private staging", {"staged_path": str(partial), "bytes": size})
            return TransferResult(0, output_path=str(partial))
        except (PermissionError, OSError) as exc:
            return TransferResult(1, str(exc), component="filesystem", retryable=False)
        finally:
            with self._lock:
                self.process = None
            relay = self._relay
            self._relay = None
            if relay is not None:
                relay.stop()

    def _terminate(self) -> None:
        with self._lock:
            process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                    )
                else:
                    os.killpg(process.pid, signal.SIGKILL)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
            try:
                process.wait(timeout=3)
            except Exception:
                pass


    def publish(self, staged_path: str) -> str:
        if self._staging_dir is None or self._final_path is None:
            raise RuntimeError("Manifest transfer publication state is unavailable")
        produced = Path(staged_path)
        published = publish_staged_file(produced, self._final_path.parent, self.job.settings.collision_policy, self._final_path.name, self._staging_dir)
        cleanup_private_staging(self._staging_dir, self._final_path.parent, self.job.job_id)
        self.current_path = ""
        self.output_path = str(published)
        self.on_diagnostic("direct_manifest_published", "Validated direct manifest transfer published", {"output_path": str(published)})
        return str(published)

    def pause(self) -> str:
        self._pause_requested = True
        self._terminate()
        return "restart"

    def resume(self) -> str:
        return "restart"

    def stop(self) -> None:
        self._stopped = True
        self._terminate()

    def reconfigure_speed_limit(self, value) -> bool:
        with self._lock:
            if self.applied_speed_limit_bps == value:
                return False
            self.applied_speed_limit_bps = value
            self.job.effective_speed_limit_bps = value
            process = self.process
            active = process is not None and process.poll() is None
            if not active:
                return False
            self._reconfigure = True
        self._terminate()
        return True
