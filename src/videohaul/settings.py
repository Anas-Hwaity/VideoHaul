from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

from .paths import DEFAULT_DOWNLOAD_DIR

RETIRED_UPDATE_SOURCES = {"https://api.github.com/repos/videohaul/videohaul/releases/latest"}


@dataclass(slots=True)
class AppSettings:
    theme: str = "dark"
    interface_mode: str = "simple"
    compact_mode: bool = False
    history_enabled: bool = True
    default_download_folder: str = str(DEFAULT_DOWNLOAD_DIR)
    default_quality: str = "best"
    max_concurrent_downloads: int = 3
    global_speed_limit_bps: int | None = None
    fair_share_mode: str = "equal"
    prioritized_job_id: str | None = None
    notifications: bool = True
    completion_sound: bool = False
    default_completion_action: str = "none"
    auto_collapse_completed: bool = False
    auto_expand_failed: bool = True
    thumbnail_visibility: bool = True
    embed_source_thumbnail_default: bool = True
    tag_download_comment_default: bool = True
    app_update_checks: bool = True
    minimize_to_tray: bool = False
    browser_dashboard: bool = True
    window_width: int = 1180
    window_height: int = 820
    window_x: int | None = None
    window_y: int | None = None
    recent_download_folders: list[str] = field(default_factory=list)
    default_organization_rule: str = "none"
    default_filename_template: str = "%(title)s.%(ext)s"
    default_container: str = "auto"
    default_preferred_codec: str = "auto"
    default_preserve_metadata: bool = True
    default_preserve_chapters: bool = True
    default_subtitle_format: str = "best"
    default_partial_cleanup: str = "keep"
    default_max_retries: int = 3
    default_retry_backoff_seconds: float = 1.0
    default_browser_cookies: str = "none"
    default_keep_original_streams: bool = False
    default_completion_move_destination: str = ""
    default_completion_command: str = ""
    update_source_url: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "AppSettings":
        data = dict(data or {})
        valid = {field.name for field in cls.__dataclass_fields__.values()}
        value = cls(**{key: value for key, value in data.items() if key in valid})
        value.theme = value.theme if value.theme in {"dark", "light", "balanced"} else "dark"
        value.interface_mode = value.interface_mode if value.interface_mode in {"simple", "advanced"} else "simple"
        value.default_download_folder = str(value.default_download_folder or DEFAULT_DOWNLOAD_DIR)
        try:
            value.max_concurrent_downloads = max(1, min(64, int(value.max_concurrent_downloads)))
        except Exception:
            value.max_concurrent_downloads = 3
        try:
            parsed_limit = int(value.global_speed_limit_bps) if value.global_speed_limit_bps is not None else None
        except Exception:
            parsed_limit = None
        value.global_speed_limit_bps = parsed_limit if parsed_limit and parsed_limit > 0 else None
        if value.fair_share_mode not in {"equal", "finish_one_fastest", "prioritize_selected"}:
            value.fair_share_mode = "equal"
        if value.default_completion_action not in {"none", "open_file", "open_folder", "play_file", "move_file", "custom_command", "sleep", "shutdown"}:
            value.default_completion_action = "none"
        if value.prioritized_job_id is not None:
            value.prioritized_job_id = str(value.prioritized_job_id) or None
        folders = value.recent_download_folders if isinstance(value.recent_download_folders, list) else []
        seen = set()
        normalized_folders = []
        for item in folders:
            text = str(item or "").strip()
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                normalized_folders.append(text)
        value.recent_download_folders = normalized_folders[:12]
        if value.default_organization_rule not in {"none", "creator", "platform", "date", "playlist"}:
            value.default_organization_rule = "none"
        value.default_filename_template = str(value.default_filename_template or "%(title)s.%(ext)s")
        value.default_container = value.default_container if value.default_container in {"auto", "source", "mp4", "mkv", "webm"} else "auto"
        value.default_preferred_codec = value.default_preferred_codec if value.default_preferred_codec in {"auto", "h264", "h265", "vp9", "av1"} else "auto"
        value.default_subtitle_format = value.default_subtitle_format if value.default_subtitle_format in {"best", "srt", "vtt", "ass"} else "best"
        value.default_partial_cleanup = value.default_partial_cleanup if value.default_partial_cleanup in {"keep", "remove", "ask"} else "keep"
        try:
            value.default_max_retries = max(0, min(20, int(value.default_max_retries)))
        except Exception:
            value.default_max_retries = 3
        try:
            value.default_retry_backoff_seconds = max(0.0, min(3600.0, float(value.default_retry_backoff_seconds)))
        except Exception:
            value.default_retry_backoff_seconds = 1.0
        value.default_browser_cookies = str(value.default_browser_cookies or "none")
        value.default_completion_move_destination = str(value.default_completion_move_destination or "")
        value.default_completion_command = str(value.default_completion_command or "")
        value.update_source_url = str(value.update_source_url or "").strip()
        if value.update_source_url in RETIRED_UPDATE_SOURCES or not value.update_source_url.startswith("https://"):
            value.update_source_url = ""
        return value
