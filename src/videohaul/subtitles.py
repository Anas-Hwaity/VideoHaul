from __future__ import annotations

from .models import SubtitleTrack

SUBTITLE_MODES = ("external", "embed", "both")
SUBTITLE_FORMATS = ("best", "srt", "vtt", "ass")


def sort_subtitles(tracks: list[SubtitleTrack]) -> list[SubtitleTrack]:
    def key(track: SubtitleTrack) -> tuple[int, int, str, str]:
        if track.is_original or track.is_default:
            group = 0
        elif track.is_human and not track.is_automatic:
            group = 1
        else:
            group = 2
        return group, 0 if track.is_original else 1, (track.language_name or track.language_code).casefold(), track.label.casefold()
    return sorted(tracks, key=key)


def subtitle_label(track: SubtitleTrack) -> str:
    parts = [track.language_name or track.language_code]
    if track.is_original:
        parts.append("Original")
    elif track.is_default:
        parts.append("Default")
    parts.append("Automatic" if track.is_automatic else "Human")
    return " • ".join(parts)


def validate_subtitle_selection(enabled: bool, selected: list[str], mode: str, output_format: str, tracks: list[SubtitleTrack]) -> tuple[bool, list[str], str, str]:
    normalized_mode = str(mode or "external").strip().casefold()
    normalized_format = str(output_format or "best").strip().casefold()
    if normalized_mode not in SUBTITLE_MODES:
        raise ValueError("Unsupported subtitle mode")
    if normalized_format not in SUBTITLE_FORMATS:
        raise ValueError("Unsupported subtitle format")
    ids = {track.track_id for track in tracks}
    values = list(dict.fromkeys(str(value) for value in selected if str(value) in ids))
    if enabled and not tracks:
        raise ValueError("This media has no subtitle tracks")
    if enabled and not values:
        raise ValueError("Select at least one subtitle track")
    if not enabled:
        values = []
    return bool(enabled), values, normalized_mode, normalized_format
