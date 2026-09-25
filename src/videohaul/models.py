from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4


class JobStatus(str, Enum):
    UNANALYZED = "unanalyzed"
    ANALYZING = "analyzing"
    READY = "ready"
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    FINALIZING = "finalizing"
    DONE = "done"
    FAILED = "failed"
    AUTH_REQUIRED = "auth_required"
    UNRESOLVED = "unresolved"
    UNAVAILABLE = "unavailable"
    DEPENDENCY_REPAIR = "dependency_repair"
    STOPPED = "stopped"


class Stage(str, Enum):
    IDLE = "idle"
    ANALYZING = "analyzing"
    RESOLVING_SOURCE = "resolving_source"
    LOADING_BROWSER = "loading_browser"
    WAITING_FOR_PAGE = "waiting_for_page"
    DETECTING_MEDIA = "detecting_media"
    PREPARING_DOWNLOADER = "preparing_downloader"
    STARTING_DOWNLOADER = "starting_downloader"
    QUEUED = "queued"
    DOWNLOADING_VIDEO = "downloading_video"
    DOWNLOADING_AUDIO = "downloading_audio"
    DOWNLOADING_SUBTITLES = "downloading_subtitles"
    WAITING_TO_RETRY = "waiting_to_retry"
    MERGING = "merging"
    MUXING = "muxing"
    EMBEDDING_METADATA = "embedding_metadata"
    EMBEDDING_THUMBNAIL = "embedding_thumbnail"
    POST_PROCESSING = "post_processing"
    FINALIZING = "finalizing"
    VALIDATING = "validating"
    DONE = "done"
    FAILED = "failed"
    PAUSED = "paused"
    STOPPED = "stopped"


ANALYSIS_FAILURE_STATUSES = frozenset(
    {JobStatus.UNAVAILABLE, JobStatus.AUTH_REQUIRED, JobStatus.UNRESOLVED, JobStatus.DEPENDENCY_REPAIR}
)
RETRYABLE_STATUSES = frozenset({JobStatus.FAILED, JobStatus.STOPPED, *ANALYSIS_FAILURE_STATUSES})
DETECTOR_ELIGIBLE_STATUSES = frozenset(
    {JobStatus.UNRESOLVED, JobStatus.UNAVAILABLE, JobStatus.AUTH_REQUIRED, JobStatus.FAILED, JobStatus.UNANALYZED, JobStatus.READY}
)


@dataclass(slots=True)
class ProgressState:
    stage: Stage = Stage.IDLE
    determinate: bool = False
    fraction: float | None = None
    downloaded_bytes: int = 0
    total_bytes: int | None = None
    total_is_estimate: bool = False
    speed_bps: float | None = None
    eta_seconds: float | None = None
    elapsed_seconds: float = 0.0
    terminal: bool = False
    message: str = ""

    def normalized(self) -> "ProgressState":
        fraction = self.fraction
        if not self.determinate:
            fraction = None
        elif fraction is not None:
            fraction = max(0.0, min(float(fraction), 1.0))
        return ProgressState(
            stage=self.stage,
            determinate=self.determinate,
            fraction=fraction,
            downloaded_bytes=max(0, int(self.downloaded_bytes or 0)),
            total_bytes=None if self.total_bytes is None else max(0, int(self.total_bytes)),
            total_is_estimate=bool(self.total_is_estimate),
            speed_bps=None if self.speed_bps is None else max(0.0, float(self.speed_bps)),
            eta_seconds=None if self.eta_seconds is None else max(0.0, float(self.eta_seconds)),
            elapsed_seconds=max(0.0, float(self.elapsed_seconds or 0.0)),
            terminal=bool(self.terminal),
            message=str(self.message or ""),
        )


@dataclass(slots=True)
class VideoStream:
    stream_id: str
    resolution_family: str
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bitrate: float | None = None
    codec: str | None = None
    container: str | None = None
    hdr_mode: str | None = None
    duration_seconds: float | None = None
    filesize_bytes: int | None = None
    filesize_is_estimate: bool = False
    has_audio: bool = False
    audio_reference: str | None = None
    direct_stream_url: str | None = None
    source_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AudioStream:
    stream_id: str
    language: str | None = None
    label: str | None = None
    bitrate: float | None = None
    codec: str | None = None
    container: str | None = None
    channels: int | None = None
    sample_rate: int | None = None
    duration_seconds: float | None = None
    filesize_bytes: int | None = None
    filesize_is_estimate: bool = False
    direct_stream_url: str | None = None
    is_original: bool = False
    is_default: bool = False


@dataclass(slots=True)
class SubtitleTrack:
    track_id: str
    language_code: str
    language_name: str
    label: str = ""
    is_original: bool = False
    is_default: bool = False
    is_human: bool = True
    is_automatic: bool = False
    formats: list[str] = field(default_factory=list)
    source: str = ""


@dataclass(slots=True)
class MediaAnalysis:
    analysis_id: str = field(default_factory=lambda: uuid4().hex)
    source_url: str = ""
    canonical_url: str = ""
    platform: str = ""
    platform_media_id: str = ""
    title: str = ""
    creator: str = ""
    channel: str = ""
    upload_date: str = ""
    thumbnail_url: str = ""
    duration_seconds: float | None = None
    is_live: bool = False
    playlist: dict[str, Any] | None = None
    video_streams: list[VideoStream] = field(default_factory=list)
    audio_streams: list[AudioStream] = field(default_factory=list)
    subtitles: list[SubtitleTrack] = field(default_factory=list)
    chapters: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    auth_state: str = "none"
    analyzed_at: float = 0.0
    expires_at: float | None = None


@dataclass(slots=True)
class DetectedMediaCandidate:
    candidate_id: str = field(default_factory=lambda: uuid4().hex)
    page_url: str = ""
    page_title: str = ""
    discovered_url: str = ""
    final_url: str = ""
    source_frame_url: str = ""
    frame_depth: int = 0
    request_method: str = "GET"
    status_code: int | None = None
    mime_type: str = ""
    sniffed_type: str = ""
    content_disposition: str = ""
    suggested_filename: str = ""
    resource_type: str = ""
    initiator_url: str = ""
    discovery_channel: str = ""
    media_kind: str = "unknown"
    container: str = ""
    title: str = ""
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    duration_seconds: float | None = None
    filesize_bytes: int | None = None
    filesize_is_estimate: bool = False
    video_codec: str = ""
    audio_codec: str = ""
    audio_channels: int | None = None
    audio_sample_rate: int | None = None
    hdr_mode: str = ""
    bitrate: float | None = None
    is_manifest: bool = False
    is_live: bool = False
    poster_or_thumbnail: str = ""
    host: str = ""
    required_headers: dict[str, str] = field(default_factory=dict)
    required_cookie_scope: str = ""
    referer: str = ""
    user_agent: str = ""
    expiry_if_known: float | None = None
    discovery_method: str = ""
    verification_state: str = "unverified"
    verification_detail: str = ""
    confidence_evidence: list[str] = field(default_factory=list)
    confidence_score: float = 0.0
    likely_main_media: bool = False
    rejected: bool = False
    language: str = ""
    split_audio_url: str = ""
    variants: list[dict[str, Any]] = field(default_factory=list)
    variant_count: int = 0
    grouped_candidate_count: int = 1
    segment_activity: int = 0
    discovered_at: float = 0.0
    first_seen_at: float = 0.0
    last_seen_at: float = 0.0

    def public_dict(self) -> dict[str, Any]:
        from .media_candidates import redact_url, url_carries_secrets

        value = asdict(self)
        value.pop("required_headers", None)
        value.pop("required_cookie_scope", None)
        value["has_required_headers"] = bool(self.required_headers)
        value["has_required_cookie_scope"] = bool(self.required_cookie_scope)
        value["url_carries_secrets"] = url_carries_secrets(self.final_url) or url_carries_secrets(self.discovered_url)
        value["discovered_url"] = redact_url(self.discovered_url)
        value["final_url"] = redact_url(self.final_url)
        value["source_frame_url"] = redact_url(self.source_frame_url)
        value["initiator_url"] = redact_url(self.initiator_url)
        value["poster_or_thumbnail"] = redact_url(self.poster_or_thumbnail)
        return value


@dataclass(slots=True)
class DetectorSessionState:
    session_id: str = field(default_factory=lambda: uuid4().hex)
    job_id: str = ""
    page_url: str = ""
    mode: str = "automatic"
    stage: str = "starting"
    message: str = ""
    active: bool = True
    headless: bool = True
    started_at: float = 0.0
    finished_at: float | None = None
    frames_seen: int = 0
    network_events_seen: int = 0
    main_status: int | None = None
    candidates: list[DetectedMediaCandidate] = field(default_factory=list)
    error: str = ""

    def public_dict(self) -> dict[str, Any]:
        from .media_candidates import redact_url

        return {
            "session_id": self.session_id,
            "job_id": self.job_id,
            "page_url": redact_url(self.page_url),
            "mode": self.mode,
            "stage": self.stage,
            "message": self.message,
            "active": self.active,
            "headless": self.headless,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "frames_seen": self.frames_seen,
            "network_events_seen": self.network_events_seen,
            "main_status": self.main_status,
            "error": self.error,
            "candidates": [item.public_dict() for item in self.candidates],
        }


@dataclass(slots=True)
class JobSettings:
    destination: str = ""
    filename_template: str = "%(title)s.%(ext)s"
    container: str = "auto"
    speed_limit_bps: int | None = None
    priority: str = "normal"
    subtitles_enabled: bool = False
    subtitle_mode: str = "external"
    selected_subtitles: list[str] = field(default_factory=list)
    selected_audio: list[str] = field(default_factory=list)
    audio_selection_mode: str = "auto"
    audio_output_format: str = "source"
    subtitle_format: str = "best"
    scheduled_start: float | None = None
    notify_complete: bool = True
    sound_complete: bool = False
    completion_action: str = "none"
    completion_move_destination: str = ""
    completion_command: str = ""
    collision_policy: str = "rename"
    preserve_metadata: bool = True
    preserve_chapters: bool = True
    embed_thumbnail: bool = True
    tag_download_comment: bool = True
    partial_cleanup: str = "keep"
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0
    browser_cookies: str = "none"
    referer: str = ""
    user_agent: str = ""
    incognito: bool = False
    organization_rule: str = "none"
    preferred_codec: str = "auto"
    video_only: bool = False
    keep_original_streams: bool = False

    def normalized(self) -> "JobSettings":
        self.destination = str(self.destination or "")
        self.filename_template = str(self.filename_template or "%(title)s.%(ext)s")
        try:
            speed = int(self.speed_limit_bps) if self.speed_limit_bps is not None else None
        except Exception:
            speed = None
        self.speed_limit_bps = speed if speed and speed > 0 else None
        self.priority = self.priority if self.priority in {"high", "normal", "low"} else "normal"
        self.collision_policy = self.collision_policy if self.collision_policy in {"rename", "overwrite", "ask"} else "rename"
        self.container = self.container if self.container in {"auto", "source", "mp4", "mkv", "webm"} else "auto"
        self.partial_cleanup = self.partial_cleanup if self.partial_cleanup in {"keep", "remove", "ask"} else "keep"
        self.organization_rule = self.organization_rule if self.organization_rule in {"none", "creator", "platform", "date", "playlist"} else "none"
        self.preferred_codec = self.preferred_codec if self.preferred_codec in {"auto", "h264", "h265", "vp9", "av1"} else "auto"
        self.video_only = bool(self.video_only)
        self.keep_original_streams = bool(self.keep_original_streams)
        allowed_completion = {"none", "open_file", "open_folder", "play_file", "move_file", "custom_command", "sleep", "shutdown"}
        self.completion_action = self.completion_action if self.completion_action in allowed_completion else "none"
        self.completion_move_destination = str(self.completion_move_destination or "")
        self.completion_command = str(self.completion_command or "")
        try:
            self.max_retries = max(0, min(20, int(self.max_retries)))
        except Exception:
            self.max_retries = 3
        try:
            self.retry_backoff_seconds = max(0.0, min(3600.0, float(self.retry_backoff_seconds)))
        except Exception:
            self.retry_backoff_seconds = 1.0
        return self


@dataclass(slots=True)
class DownloadJob:
    job_id: str = field(default_factory=lambda: uuid4().hex)
    source_url: str = ""
    analysis: MediaAnalysis | None = None
    selected_video: str | None = None
    settings: JobSettings = field(default_factory=JobSettings)
    status: JobStatus = JobStatus.UNANALYZED
    progress: ProgressState = field(default_factory=ProgressState)
    queue_position: int = 0
    collapsed: bool = False
    pinned: bool = False
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    failure_reason: str = ""
    failure_class: str = ""
    thumbnail_source_url: str = ""
    thumbnail_embed_result: str = "not_attempted"
    thumbnail_embed_detail: str = ""
    detector_session_id: str = ""
    detector_candidate_id: str = ""
    output_path: str = ""
    companion_paths: list[str] = field(default_factory=list)
    partial_path: str = ""
    resume_state: str = "uncertain"
    retry_state: dict[str, Any] = field(default_factory=dict)
    transfer_headers: dict[str, str] = field(default_factory=dict)
    effective_speed_limit_bps: int | None = None


def dataclass_dict(value: Any) -> dict[str, Any]:
    return asdict(value)
