from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
from uuid import uuid4

from .contracts import TransferResult
from ..audio import balanced_audio_stream
from ..dependencies import ensure_tools, js_runtime_args
from ..file_policy import MANIFEST_DELIVERED_AUDIO_CONTAINER, MANIFEST_DELIVERED_CONTAINER, cleanup_private_staging, path_is_within, private_staging_directory, publish_staged_group, render_output_template, resolve_collision
from ..filesystem import estimated_job_bytes, organized_destination
from ..diagnostics import redact_command
from ..http_security import SENSITIVE_FORWARD_HEADERS
from ..quality import AUDIO_ONLY_SELECTOR, BEST_SELECTOR
from ..unicode_text import decode_external_bytes
from .capabilities import probe_capabilities
from .telemetry import (
    ANSI_ESCAPE,
    COMPONENT_PREFIX,
    FILE_PREFIX,
    PROGRESS_PREFIX,
    PROGRESS_TEMPLATE,
    LineFramer,
    ProgressTracker,
    component_stage,
    marker_payload,
    parse_destination,
    parse_download_line,
    parse_merge_target,
    parse_resume_offset,
    parse_structured_progress,
)

READ_CHUNK_BYTES = 65536
DISK_FALLBACK_AFTER_SECONDS = 2.0
STALL_WARNING_AFTER_SECONDS = 10.0
WATCHDOG_INTERVAL_SECONDS = 1.0


class YtDlpTransfer:
    def __init__(self, job, on_progress, on_stage, on_diagnostic=None):
        self.job = job
        self.on_progress = on_progress
        self.on_stage = on_stage
        self.process: subprocess.Popen | None = None
        self._paused = False
        self._paused_fallback = False
        self._stopped = False
        self._reconfigure = False
        self.published_companions: list[str] = []
        self._lock = threading.RLock()
        self.current_path = str(job.partial_path or "")
        self.resume_state = self._resume_capability()
        self.output_path = ""
        self.applied_speed_limit_bps = job.effective_speed_limit_bps
        self.on_diagnostic = on_diagnostic or (lambda kind, message, payload, level="info": None)
        self._path_report = Path(tempfile.gettempdir()) / f"videohaul-{job.job_id}-{uuid4().hex}.path"
        self._component = ""
        self._capabilities = None
        self._expected_output = ""
        self._expected_directory = ""
        self._planned_total_bytes = estimated_job_bytes(job)
        self._planned_total_is_estimate = self._planned_size_is_estimate()
        self._display_total_bytes = self._planned_total_bytes
        self._display_total_is_estimate = self._planned_total_is_estimate
        self._component_downloaded: dict[str, int] = {}
        self._active_progress_component = ""
        self._multiple_components = "+" in self._format_selector()
        self._staging_dir: Path | None = None
        self._final_destination: Path | None = None


    def _planned_size_is_estimate(self) -> bool:
        analysis = self.job.analysis
        if not analysis:
            return False
        if self.job.selected_video == AUDIO_ONLY_SELECTOR:
            selected = {str(value) for value in self.job.settings.selected_audio if str(value)}
            tracks = [item for item in analysis.audio_streams if not selected or item.stream_id in selected]
            return any(bool(item.filesize_is_estimate) for item in tracks if item.filesize_bytes is not None)
        stream = next((item for item in analysis.video_streams if item.stream_id == self.job.selected_video), None)
        if stream is None:
            return False
        if stream.has_audio or self.job.settings.video_only:
            return bool(stream.filesize_is_estimate)
        audios = [item for item in analysis.audio_streams if item.filesize_bytes is not None]
        if not audios:
            return bool(stream.filesize_is_estimate)
        audio = max(audios, key=lambda item: (item.bitrate or 0, item.filesize_bytes or 0, item.stream_id))
        return bool(stream.filesize_is_estimate or audio.filesize_is_estimate)

    def _known_stream_stage(self, stream_id: str) -> str:
        value = str(stream_id or "").strip()
        analysis = self.job.analysis
        if not value or not analysis:
            return ""
        if any(item.stream_id == value for item in analysis.audio_streams):
            return "downloading_audio"
        if any(item.stream_id == value for item in analysis.video_streams):
            return "downloading_video"
        return ""

    def _stream_id_from_path(self, path: str) -> str:
        name = Path(str(path or "")).name
        analysis = self.job.analysis
        if not name or not analysis:
            return ""
        ids = [item.stream_id for item in [*analysis.audio_streams, *analysis.video_streams] if str(item.stream_id or "")]
        for stream_id in sorted(ids, key=len, reverse=True):
            escaped = re.escape(str(stream_id))
            if re.search(rf"(?:^|[._-])f?{escaped}(?:[._-]|$)", name, re.IGNORECASE):
                return str(stream_id)
        return ""

    def _stage_from_path(self, path: str) -> str:
        stream_id = self._stream_id_from_path(path)
        stage = self._known_stream_stage(stream_id)
        if stage:
            return stage
        suffix = Path(str(path or "")).suffix.casefold()
        if suffix == ".part":
            suffix = Path(Path(str(path or "")).stem).suffix.casefold()
        if suffix in {".m4a", ".mp3", ".aac", ".flac", ".wav", ".opus", ".ogg", ".oga", ".weba"}:
            return "downloading_audio"
        if suffix in {".mp4", ".m4v", ".mov", ".avi", ".mkv", ".ts"}:
            return "downloading_video"
        return ""

    def _component_stage_for_sample(self, sample: dict) -> str:
        filename = str(sample.get("filename") or "").strip()
        stage = self._stage_from_path(filename) if filename else ""
        if stage:
            return stage
        format_id = str(sample.get("format_id") or "").strip()
        stage = self._known_stream_stage(format_id)
        if stage:
            return stage
        stage = component_stage(sample)
        if stage:
            return stage
        return self._component

    def _set_component_stage(self, stage: str) -> None:
        value = str(stage or "").strip()
        if value and value != self._component:
            self._component = value
            self.on_stage(value)

    def _progress_component_key(self, sample: dict) -> str:
        format_id = str(sample.get("format_id") or "").strip()
        sample_filename = str(sample.get("filename") or "").strip()
        stream_id = self._stream_id_from_path(sample_filename) if sample_filename else ""
        if stream_id:
            return f"stream:{stream_id}"
        known_stage = self._known_stream_stage(format_id)
        if format_id and known_stage:
            return f"stream:{format_id}"
        if not format_id and not sample_filename and self._active_progress_component:
            return self._active_progress_component
        filename = sample_filename or str(self.current_path or "").strip()
        if format_id and filename:
            return f"{format_id}|{Path(filename).name}"
        if format_id:
            return f"format:{format_id}"
        if filename:
            return f"file:{Path(filename).name}"
        return f"stage:{self._component or 'download'}"

    def _display_progress(self, emitted: dict, sample: dict) -> tuple[int, int | None, bool]:
        key = self._progress_component_key(sample)
        self._active_progress_component = key
        raw_downloaded = max(0, int(emitted.get("downloaded_bytes") or 0))
        self._component_downloaded[key] = max(self._component_downloaded.get(key, 0), raw_downloaded)
        downloaded = sum(self._component_downloaded.values())
        if self._display_total_bytes is None and not self._multiple_components:
            raw_total = emitted.get("total_bytes")
            if raw_total is not None and int(raw_total) > 0:
                self._display_total_bytes = int(raw_total)
                self._display_total_is_estimate = bool(emitted.get("total_is_estimate"))
        return downloaded, self._display_total_bytes, bool(self._display_total_is_estimate)

    def _resume_capability(self) -> str:
        analysis = self.job.analysis
        if analysis and analysis.is_live:
            return "restart_required"
        streams = []
        if analysis:
            streams.extend(analysis.video_streams)
            streams.extend(analysis.audio_streams)
        selected = {str(self.job.selected_video or ""), *map(str, self.job.settings.selected_audio)}
        protocols = {
            str(item.source_metadata.get("protocol") or "").casefold()
            for item in streams
            if item.stream_id in selected and hasattr(item, "source_metadata")
        }
        if any(value.startswith(("http", "m3u8", "dash")) for value in protocols):
            return "supported"
        if any(getattr(item, "direct_stream_url", None) for item in streams if item.stream_id in selected):
            return "supported"
        return "uncertain"

    def _format_selector(self) -> str:
        selected_audio = [str(value) for value in self.job.settings.selected_audio if str(value)]
        if self.job.selected_video == AUDIO_ONLY_SELECTOR:
            if selected_audio:
                return "+".join(selected_audio)
            return "bestaudio/best"
        if self.job.selected_video and self._is_selector_expression(str(self.job.selected_video)):
            if self.job.settings.video_only:
                return "bestvideo"
            if selected_audio:
                return "bestvideo*+" + "+".join(selected_audio) + "/best"
            return str(self.job.selected_video)
        if self.job.selected_video:
            base = str(self.job.selected_video)
            if self.job.settings.video_only:
                return base
            if selected_audio:
                return "+".join([base, *selected_audio])
            analysis = self.job.analysis
            stream = next((item for item in analysis.video_streams if item.stream_id == base), None) if analysis else None
            if stream and stream.has_audio:
                return base
            if stream and stream.audio_reference:
                return f"{base}+{stream.audio_reference}"
            automatic_audio = balanced_audio_stream(analysis.audio_streams, stream) if analysis else None
            if automatic_audio is not None:
                return f"{base}+{automatic_audio.stream_id}"
            return f"{base}+bestaudio/best"
        return "bestvideo" if self.job.settings.video_only else "bestvideo*+bestaudio/best"

    def _is_selector_expression(self, value: str) -> bool:
        analysis = self.job.analysis
        known = {item.stream_id for item in [*(analysis.video_streams if analysis else []), *(analysis.audio_streams if analysis else [])]}
        if value in known:
            return False
        return value == BEST_SELECTOR or any(character in value for character in "/+*[]")

    def _selected_stream(self):
        analysis = self.job.analysis
        if not analysis:
            return None
        if self.job.selected_video == AUDIO_ONLY_SELECTOR:
            selected = [str(value) for value in self.job.settings.selected_audio if str(value)]
            for item in analysis.audio_streams:
                if not selected or item.stream_id in selected:
                    return item
            return None
        return next((item for item in analysis.video_streams if item.stream_id == self.job.selected_video), None)

    def transfer_source(self) -> dict:
        stream = self._selected_stream()
        stream_metadata = dict(getattr(stream, "source_metadata", None) or {}) if stream is not None else {}
        analysis_metadata = dict(self.job.analysis.metadata or {}) if self.job.analysis else {}
        marked = bool(stream_metadata.get("direct_transfer")) or bool(analysis_metadata.get("direct_transfer"))
        direct = str(getattr(stream, "direct_stream_url", "") or "") if stream is not None else ""
        if direct and marked:
            return {"url": direct, "format_selector": "", "direct": True, "stream_id": str(getattr(stream, "stream_id", ""))}
        return {"url": str(self.job.source_url or ""), "format_selector": self._format_selector(), "direct": False, "stream_id": str(self.job.selected_video or "")}

    def _manifest_kind(self) -> str:
        stream = self._selected_stream()
        metadata = dict(getattr(stream, "source_metadata", None) or {}) if stream is not None else {}
        return str(metadata.get("manifest_kind") or "")

    def _request_context_args(self) -> list[str]:
        metadata = dict(self.job.analysis.metadata or {}) if self.job.analysis else {}
        args: list[str] = []
        private_headers = dict(getattr(self.job, "transfer_headers", None) or {})
        for name, value in private_headers.items():
            header_name = str(name or "").strip()
            header_value = str(value or "").strip()
            if header_name and header_value and header_name.casefold() not in SENSITIVE_FORWARD_HEADERS:
                args.extend(["--add-header", f"{header_name}:{header_value}"])
        referer = str(self.job.settings.referer or metadata.get("referer") or "").strip()
        if referer and not any(str(name).casefold() == "referer" for name in private_headers):
            args.extend(["--referer", referer])
        user_agent = str(self.job.settings.user_agent or metadata.get("user_agent") or "").strip()
        if user_agent and not any(str(name).casefold() == "user-agent" for name in private_headers):
            args.extend(["--user-agent", user_agent])
        return args

    def _subtitle_args(self) -> list[str]:
        settings = self.job.settings
        if not settings.subtitles_enabled or not settings.selected_subtitles:
            return []
        analysis = self.job.analysis
        tracks = {item.track_id: item for item in analysis.subtitles} if analysis else {}
        selected = [tracks[value] for value in settings.selected_subtitles if value in tracks]
        if not selected:
            return []
        languages = []
        for track in selected:
            if track.language_code not in languages:
                languages.append(track.language_code)
        args = ["--write-subs", "--sub-langs", ",".join(languages)]
        if any(track.is_automatic for track in selected):
            args.append("--write-auto-subs")
        subtitle_format = str(settings.subtitle_format or "best").lower()
        if subtitle_format != "best":
            args.extend(["--sub-format", subtitle_format, "--convert-subs", subtitle_format])
        if settings.subtitle_mode in {"embed", "both"}:
            args.append("--embed-subs")
        if settings.subtitle_mode == "both":
            args.append("--keep-subs")
        return args

    def build_command(self, tools: dict[str, str], capabilities=None) -> list[str]:
        destination = organized_destination(self.job)
        destination.mkdir(parents=True, exist_ok=True)
        rendered = render_output_template(self.job)
        self._final_destination = destination
        if "%(" not in rendered and str(self.job.settings.collision_policy or "rename").lower() == "ask":
            resolve_collision(destination, rendered, "ask")
        staging = private_staging_directory(destination, self.job.job_id)
        self._staging_dir = staging
        output_template = str(staging / rendered)
        retries = max(0, int(self.job.settings.max_retries))
        args = [
            tools["yt_dlp"],
            "--ignore-config",
            "--no-quiet",
            "--progress",
            "--newline",
            "--no-playlist",
            "--no-color",
            "--encoding",
            "utf-8",
            "--progress-delta",
            "1",
            "--continue",
            "--part",
            "--retries",
            str(retries),
            "--fragment-retries",
            str(retries),
            "--file-access-retries",
            str(retries),
            "--extractor-retries",
            str(retries),
            "--retry-sleep",
            "http:linear=1::2",
            "--retry-sleep",
            "fragment:exp=1:20",
            "--progress-template",
            f"download:{PROGRESS_TEMPLATE}",
            "--print-to-file",
            f"after_move:{FILE_PREFIX}%(filepath)j",
            str(self._path_report),
            "--ffmpeg-location",
            tools["ffmpeg"],
            *js_runtime_args(tools.get("deno", "")),
            "-o",
            output_template,
        ]
        source = self.transfer_source()
        if source["format_selector"]:
            args.extend(["-f", source["format_selector"]])
        policy = str(self.job.settings.collision_policy or "rename").lower()
        if policy == "overwrite":
            args.append("--force-overwrites")
        else:
            args.append("--no-overwrites")
        speed_limits = [value for value in (self.job.settings.speed_limit_bps, self.job.effective_speed_limit_bps) if value is not None and int(value) > 0]
        if speed_limits:
            args.extend(["--limit-rate", str(min(map(int, speed_limits)))])
        if self.job.settings.browser_cookies and self.job.settings.browser_cookies.lower() != "none":
            args.extend(["--cookies-from-browser", self.job.settings.browser_cookies])
        args.extend(self._request_context_args())
        if self.job.settings.preserve_metadata:
            args.append("--embed-metadata")
        if self.job.settings.preserve_chapters:
            args.append("--embed-chapters")
        codec_sort = {
            "h264": "vcodec:h264",
            "h265": "vcodec:h265",
            "vp9": "vcodec:vp9",
            "av1": "vcodec:av01",
        }.get(str(self.job.settings.preferred_codec or "auto").lower())
        if codec_sort:
            args.extend(["--format-sort", codec_sort])
        if self.job.settings.keep_original_streams:
            args.append("--keep-video")
        container = str(self.job.settings.container or "auto").lower()
        if container not in {"", "auto", "source"}:
            args.extend(["--merge-output-format", container])
        elif self._manifest_kind():
            audio_only = self.job.selected_video == AUDIO_ONLY_SELECTOR
            delivered = MANIFEST_DELIVERED_AUDIO_CONTAINER if audio_only else MANIFEST_DELIVERED_CONTAINER
            args.extend(["--merge-output-format", delivered, "--remux-video", delivered])
        if self.job.selected_video == AUDIO_ONLY_SELECTOR:
            audio_output = str(self.job.settings.audio_output_format or "source").lower()
            if audio_output not in {"", "source"}:
                args.extend(["--extract-audio", "--audio-format", audio_output])
        args.extend(self._subtitle_args())
        if capabilities is not None:
            args, dropped = capabilities.filter(args)
            if dropped:
                self.on_diagnostic(
                    "transfer_options_unsupported",
                    "Some yt-dlp options are not supported by the installed build and were omitted",
                    {"dropped": dropped, "version": capabilities.version},
                    "warning",
                )
        args.extend(["--", source["url"]])
        return args

    def _child_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8:strict"
        return env

    def _decode_line(self, raw) -> str:
        return decode_external_bytes(raw)

    def _marker_payload(self, text: str, prefix: str) -> str | None:
        return marker_payload(text, prefix)

    def _decode_path_payload(self, payload: str) -> str:
        value = str(payload or "").strip()
        if not value:
            return ""
        try:
            parsed = json.loads(value)
            if isinstance(parsed, str):
                return parsed
        except Exception:
            pass
        return value

    def _path_from_report(self) -> str:
        try:
            if not self._path_report.is_file():
                return ""
            values = decode_external_bytes(self._path_report.read_bytes()).splitlines()
            for line in reversed(values):
                payload = self._marker_payload(line, FILE_PREFIX)
                if payload is not None:
                    return self._decode_path_payload(payload)
        except Exception as exc:
            self.on_diagnostic("transfer_path_report_failed", "Could not read yt-dlp final-path report", {"error": str(exc)}, "warning")
        return ""

    def _final_output_path(self) -> str:
        report = self._path_from_report()
        if report:
            if report != self.output_path:
                self.on_diagnostic("transfer_output_path_recovered", "Final output path recovered from yt-dlp side channel", {"stdout_path": self.output_path, "reported_path": report})
            self.output_path = report
        return self.output_path

    def _cleanup_path_report(self) -> None:
        try:
            self._path_report.unlink(missing_ok=True)
        except Exception:
            pass

    def run(self) -> TransferResult:
        with self._lock:
            if self._stopped:
                return TransferResult(1, "Stopped", stopped=True)
            if self._paused_fallback:
                return TransferResult(1, "Paused", paused=True)
        self._cleanup_path_report()
        self.on_stage("preparing")
        try:
            tools = ensure_tools()
            capabilities = probe_capabilities(tools["yt_dlp"])
            self._capabilities = capabilities
            missing = capabilities.missing_progress_options()
            if missing:
                self.on_diagnostic(
                    "transfer_progress_options_missing",
                    "The installed yt-dlp build does not support every progress option VideoHaul relies on",
                    {"missing": missing, "version": capabilities.version},
                    "warning",
                )
            args = self.build_command(tools, capabilities)
            source = self.transfer_source()
            self._expected_output = self._expected_output_path(args)
            self.on_diagnostic(
                "transfer_command_ready",
                "yt-dlp command prepared",
                {
                    "command": redact_command(args),
                    "mode": "audio_only" if self.job.selected_video == AUDIO_ONLY_SELECTOR else "video",
                    "destination": self.job.settings.destination,
                    "transfer_source": redact_command([source["url"]])[0],
                    "direct_stream": source["direct"],
                    "page_url": redact_command([str(self.job.source_url or "")])[0],
                    "yt_dlp_version": capabilities.version,
                    "progress_reporting": "template" if "--progress-template" in args else "download_line",
                },
            )
        except FileExistsError as exc:
            self.on_diagnostic("transfer_setup_failed", "Output collision prevented transfer start", {"error": str(exc)}, "error")
            return TransferResult(2, f"Output already exists: {exc}", component="collision")
        except Exception as exc:
            self.on_diagnostic("transfer_setup_failed", "Transfer setup failed", {"error": str(exc)}, "error")
            return TransferResult(2, str(exc), component="setup")
        flags = 0
        kwargs = {}
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000
        else:
            kwargs["start_new_session"] = True
        try:
            with self._lock:
                if self._stopped:
                    return TransferResult(1, "Stopped", stopped=True)
                if self._paused_fallback:
                    return TransferResult(1, "Paused", paused=True)
                self.process = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=False,
                    bufsize=0,
                    creationflags=flags,
                    env=self._child_env(),
                    **kwargs,
                )
        except Exception as exc:
            self.on_diagnostic("transfer_process_start_failed", "yt-dlp process could not start", {"error": str(exc), "command": redact_command(args)}, "error")
            return TransferResult(2, str(exc), component="setup")
        self.on_diagnostic("transfer_process_started", "yt-dlp process started", {"pid": self.process.pid})
        self.on_stage("starting")
        output = deque(maxlen=80)
        process_stdout = self.process.stdout
        if process_stdout is None:
            return TransferResult(2, "yt-dlp stdout pipe was not created", component="setup")
        tracker = ProgressTracker()
        self._tracker = tracker
        framer = LineFramer()
        finished = threading.Event()
        watchdog = threading.Thread(target=self._watchdog, args=(tracker, finished), name="videohaul-transfer-watchdog", daemon=True)
        watchdog.start()
        try:
            while True:
                chunk = process_stdout.read(READ_CHUNK_BYTES)
                if not chunk:
                    break
                for raw_line in framer.feed(chunk):
                    self._handle_line(raw_line, output, tracker)
            for raw_line in framer.flush():
                self._handle_line(raw_line, output, tracker)
        finally:
            finished.set()
            watchdog.join(timeout=5)
        returncode = self.process.wait()
        self._report_telemetry_summary(tracker)
        self._final_output_path()
        detail = "\n".join(output)
        self.on_diagnostic("transfer_process_exited", "yt-dlp process exited", {"returncode": returncode, "output_path": self.output_path, "tail": list(output)[-12:]}, "info" if returncode == 0 else "error")
        self._cleanup_path_report()
        if self._paused_fallback:
            return TransferResult(returncode, detail, self.output_path, paused=True)
        if self._stopped:
            return TransferResult(returncode, detail, self.output_path, stopped=True)
        if self._reconfigure:
            return TransferResult(returncode, detail, self.output_path, component="bandwidth", restart=True)
        if returncode == 0:
            produced = Path(str(self.output_path or ""))
            if self._staging_dir is None or not path_is_within(produced, self._staging_dir):
                return TransferResult(2, "yt-dlp reported an output outside the private staging directory", self.output_path, component="filesystem", retryable=False)
            if not produced.is_file() or produced.stat().st_size <= 0:
                return TransferResult(2, "yt-dlp reported success without a usable staged output", self.output_path, component="download", retryable=False)
            self.on_diagnostic("transfer_output_staged", "yt-dlp completed in private staging", {"staged_path": self.output_path})
            return TransferResult(0, detail, self.output_path)
        component, retryable = classify_failure(detail)
        return TransferResult(returncode, detail, self.output_path, component, retryable)

    def publish(self, reported: str) -> str:
        staging = self._staging_dir
        destination = self._final_destination
        if staging is None or destination is None or not reported:
            raise RuntimeError("yt-dlp publication state is unavailable")
        produced = Path(reported)
        try:
            relative = produced.resolve().relative_to(staging.resolve())
        except Exception as exc:
            raise RuntimeError("Refusing to publish yt-dlp output from outside the private staging directory") from exc
        if not produced.is_file():
            raise FileNotFoundError(str(produced))
        if produced.stat().st_size <= 0:
            raise RuntimeError(f"Refusing to publish an empty output file: {produced}")
        policy = str(self.job.settings.collision_policy or "rename").lower()
        target_directory = destination / relative.parent
        target_directory.mkdir(parents=True, exist_ok=True)
        prefix = produced.stem
        siblings = [
            sibling for sibling in sorted(produced.parent.iterdir())
            if sibling.is_file()
            and sibling != produced
            and sibling.suffix.casefold() not in {".part", ".ytdl"}
            and sibling.name.startswith(prefix)
        ]
        main_target = resolve_collision(target_directory, relative.name, policy) if policy != "overwrite" else target_directory / relative.name
        entries: list[tuple[Path, str, str]] = [(produced, main_target.name, "overwrite" if policy == "overwrite" else "ask")]
        for sibling in siblings:
            companion_name = main_target.stem + sibling.name[len(prefix):]
            companion_policy = "overwrite" if policy == "overwrite" else "rename"
            entries.append((sibling, companion_name, companion_policy))
        published = publish_staged_group(entries, target_directory, staging)
        final = published[0]
        self.published_companions = [str(path) for path in published[1:]]
        cleanup_private_staging(staging, destination, self.job.job_id)
        self.on_diagnostic("transfer_output_published", "Finished file and companions moved from private staging", {"output_path": str(final), "companion_paths": list(self.published_companions), "collision_policy": policy})
        return str(final)

    def _handle_line(self, raw_line: bytes, output, tracker: ProgressTracker) -> None:
        text = self._decode_line(raw_line).rstrip("\r\n")
        if not text:
            return
        output.append(text)
        file_payload = self._marker_payload(text, FILE_PREFIX)
        if file_payload is not None:
            self.output_path = self._decode_path_payload(file_payload)
            self.on_diagnostic("transfer_output_path_reported", "yt-dlp reported final output path", {"output_path": self.output_path})
            return
        progress_payload = self._marker_payload(text, PROGRESS_PREFIX)
        if progress_payload is not None:
            sample = parse_structured_progress(progress_payload)
            if sample is None:
                self.on_diagnostic("transfer_progress_parse_failed", "Could not parse yt-dlp progress telemetry", {"line": text[:1000]}, "warning")
                return
            self._set_component_stage(self._component_stage_for_sample(sample))
            self._accept_sample(tracker, sample)
            return
        component_payload = self._marker_payload(text, COMPONENT_PREFIX)
        if component_payload is not None:
            fields = component_payload.split("|")
            sample = {
                "format_id": fields[0] if fields else "",
                "vcodec": fields[1] if len(fields) > 1 else "",
                "acodec": fields[2] if len(fields) > 2 else "",
                "filename": self.current_path,
            }
            self._set_component_stage(self._component_stage_for_sample(sample))
            return
        destination = parse_destination(text)
        if destination:
            self.current_path = destination
            self._set_component_stage(self._stage_from_path(destination))
            return
        merged = parse_merge_target(text)
        if merged:
            self.output_path = merged
            return
        offset = parse_resume_offset(text)
        if offset is not None:
            tracker.note_resume_offset(offset)
            self.on_diagnostic("transfer_resumed_at_offset", "Transfer resumed from an existing partial file", {"offset_bytes": offset})
            return
        sample = parse_download_line(text)
        if sample is not None:
            self._accept_sample(tracker, sample)
            return
        if "[info] Writing video subtitles" in text or "[download] Downloading subtitles" in text:
            self.on_stage("downloading_subtitles")
        elif "[Merger]" in text or "Merging formats" in text:
            self.on_stage("merging")
        elif "[Metadata]" in text or "[EmbedSubtitle]" in text or "[ThumbnailsConvertor]" in text or "[ExtractAudio]" in text:
            self.on_stage("finalizing")

    def _accept_sample(self, tracker: ProgressTracker, sample: dict) -> None:
        emitted = tracker.accept(sample)
        if emitted is None:
            return
        if emitted.get("filename"):
            self.current_path = str(emitted["filename"])
        downloaded, total, total_is_estimate = self._display_progress(emitted, sample)
        self.on_diagnostic(
            "transfer_progress",
            "yt-dlp progress",
            {
                "downloaded_bytes": downloaded,
                "total_bytes": total,
                "total_is_estimate": total_is_estimate,
                "backend_downloaded_bytes": emitted["downloaded_bytes"],
                "backend_total_bytes": emitted["total_bytes"],
                "backend_total_is_estimate": emitted["total_is_estimate"],
                "speed_bps": emitted["speed_bps"],
                "eta_seconds": emitted["eta_seconds"],
                "elapsed_seconds": emitted["elapsed_seconds"],
                "source": emitted["source"],
                "partial_path": self.current_path,
                "component_key": self._active_progress_component,
            },
        )
        try:
            self.on_progress(
                downloaded,
                total,
                emitted["speed_bps"],
                emitted["eta_seconds"],
                emitted["elapsed_seconds"],
                partial_path=self.current_path,
                resume_state=self.resume_state,
                total_is_estimate=total_is_estimate,
            )
        except TypeError:
            self.on_progress(
                downloaded,
                total,
                emitted["speed_bps"],
                emitted["eta_seconds"],
                emitted["elapsed_seconds"],
                partial_path=self.current_path,
                resume_state=self.resume_state,
            )

    def _expected_output_path(self, args: list[str]) -> str:
        values = list(args)
        for index, token in enumerate(values):
            if token == "-o" and index + 1 < len(values):
                target = str(values[index + 1] or "")
                self._expected_directory = str(Path(target).parent) if target else ""
                return "" if "%(" in target else target
        return ""

    def _partial_candidates(self) -> list[Path]:
        values: list[Path] = []
        seen: set[str] = set()
        for raw in (self.current_path, self.output_path, self._expected_output):
            text = str(raw or "").strip()
            if not text:
                continue
            for value in (text, text + ".part"):
                if value not in seen:
                    seen.add(value)
                    values.append(Path(value))
        return values

    def _scanned_partials(self) -> list[Path]:
        directory = str(self._expected_directory or "").strip()
        if not directory:
            return []
        try:
            root = Path(directory)
            if not root.is_dir():
                return []
            values = [item for item in root.iterdir() if item.is_file() and item.suffix.casefold() in {".part", ".ytdl"}]
        except Exception:
            return []
        values.sort(key=lambda item: self._safe_mtime(item), reverse=True)
        return [item for item in values if item.suffix.casefold() == ".part"][:4]

    @staticmethod
    def _safe_mtime(path: Path) -> float:
        try:
            return float(path.stat().st_mtime)
        except Exception:
            return 0.0

    def _disk_sample(self) -> dict | None:
        for candidate in [*self._partial_candidates(), *self._scanned_partials()]:
            try:
                if not candidate.is_file():
                    continue
                size = candidate.stat().st_size
            except Exception:
                continue
            if size > 0:
                return {"source": "disk", "downloaded_bytes": int(size), "filename": str(candidate)}
        return None

    def _watchdog(self, tracker: ProgressTracker, finished: threading.Event) -> None:
        warned = False
        while not finished.wait(WATCHDOG_INTERVAL_SECONDS):
            process = self.process
            if process is None or process.poll() is not None:
                return
            if self._paused or self._stopped:
                continue
            silent = tracker.silent_for()
            if silent < DISK_FALLBACK_AFTER_SECONDS:
                warned = False
                continue
            sample = self._disk_sample()
            if sample is not None:
                self._accept_sample(tracker, sample)
                warned = False
                continue
            if silent >= STALL_WARNING_AFTER_SECONDS and not warned:
                warned = True
                self.on_diagnostic(
                    "transfer_progress_stalled",
                    "No transfer telemetry has been observed recently",
                    {
                        "silent_seconds": round(silent, 1),
                        "telemetry_seen": tracker.has_telemetry(),
                        "sources": sorted(tracker.sources),
                        "partial_path": self.current_path,
                    },
                    "warning",
                )
            self._report_silence(tracker, silent)

    def _report_silence(self, tracker: ProgressTracker, silent: float) -> None:
        if tracker.has_telemetry():
            return
        try:
            self.on_progress(
                0,
                None,
                None,
                None,
                max(0.0, float(silent)),
                partial_path=self.current_path,
                resume_state=self.resume_state,
                message=f"Downloading, no progress reported by yt-dlp for {int(silent)}s",
            )
        except TypeError:
            return

    def _report_telemetry_summary(self, tracker: ProgressTracker) -> None:
        self.on_diagnostic(
            "transfer_telemetry_summary",
            "Transfer telemetry summary",
            {
                "updates": tracker.updates,
                "sources": sorted(tracker.sources),
                "downloaded_bytes": tracker.downloaded_bytes,
                "total_bytes": tracker.total_bytes,
            },
            "info" if tracker.has_telemetry() else "error",
        )

    def pause(self) -> str:
        with self._lock:
            if self._paused_fallback:
                return "restart"
            if self._stopped:
                return "inactive"
            self._paused_fallback = True
            process = self.process
            active = process is not None and process.poll() is None
        if active:
            self._terminate_tree()
        return "restart"

    def resume(self) -> str:
        with self._lock:
            return "restart" if self._paused_fallback else "inactive"

    def stop(self) -> None:
        with self._lock:
            self._stopped = True
            self._paused = False
            process = self.process
            active = process is not None and process.poll() is None
        if active:
            self._terminate_tree()

    def reconfigure_speed_limit(self, value: int | None) -> bool:
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
        self._terminate_tree()
        return True

    def _terminate_tree(self) -> None:
        with self._lock:
            process = self.process
        if process is None or process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except Exception:
                process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                )
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except Exception:
                    process.kill()
            process.wait(timeout=8)


DETERMINISTIC_FAILURES = (
    "unsupported url",
    "is not a valid url",
    "no video formats found",
    "requested format is not available",
    "unable to extract",
    "no such option",
    "video unavailable",
    "this video is private",
    "is not available in your country",
    "removed by the uploader",
    "account has been terminated",
    "no such file or directory",
)

AUTHENTICATION_EVIDENCE = (
    "sign in",
    "signin",
    "log in",
    "login required",
    "requires authentication",
    "use --cookies",
    "--cookies-from-browser",
    "private video",
    "members-only",
    "subscribe to this channel",
    "paid content",
    "this video is only available to",
    "http error 401",
    "401 unauthorized",
)


def classify_failure(detail: str) -> tuple[str, bool]:
    value = str(detail or "").casefold()
    if any(token in value for token in DETERMINISTIC_FAILURES):
        return "unsupported", False
    if any(token in value for token in AUTHENTICATION_EVIDENCE):
        return "authentication", False
    if any(token in value for token in ("no space left", "disk full", "permission denied", "access is denied")):
        return "filesystem", False
    if any(token in value for token in ("already exists", "file exists")):
        return "collision", False
    if any(token in value for token in ("merger", "merge", "ffmpeg")):
        return "merge", True
    if "subtitle" in value:
        return "subtitle", True
    if any(token in value for token in ("audio", "extractaudio")):
        return "audio", True
    if any(token in value for token in ("fragment", "segment")):
        return "fragment", True
    if any(token in value for token in ("timed out", "timeout", "network", "connection", "failed to resolve", "getaddrinfo", "name or service not known", "http error 429", "rate limit")):
        return "network", True
    if "http error 403" in value or "forbidden" in value:
        return "access", True
    return "download", True
