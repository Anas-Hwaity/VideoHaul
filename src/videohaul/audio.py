from __future__ import annotations

from .models import AudioStream, VideoStream

AUDIO_AUTO = "auto"
AUDIO_OUTPUTS = ("source", "aac", "opus", "mp3", "flac")


def sort_audio_streams(streams: list[AudioStream]) -> list[AudioStream]:
    return sorted(streams, key=lambda stream: (0 if stream.is_default or stream.is_original else 1, (stream.language or "").casefold(), (stream.label or "").casefold(), -(stream.bitrate or 0), stream.stream_id))


def auto_audio_stream(streams: list[AudioStream]) -> AudioStream | None:
    if not streams:
        return None
    ordered = sort_audio_streams(streams)
    preferred = [stream for stream in ordered if stream.is_default or stream.is_original]
    pool = preferred or ordered
    return max(pool, key=lambda stream: (stream.bitrate or 0, stream.channels or 0, stream.sample_rate or 0, stream.stream_id))


def balanced_audio_stream(streams: list[AudioStream], video: VideoStream | None = None) -> AudioStream | None:
    """Choose good automatic audio without letting it dwarf a low-bandwidth video."""
    if not streams:
        return None
    ordered = sort_audio_streams(streams)
    preferred = [stream for stream in ordered if stream.is_default or stream.is_original]
    pool = preferred or ordered
    if video is None:
        return auto_audio_stream(pool)
    video_size = int(video.filesize_bytes or 0)
    known_sizes = [stream for stream in pool if int(stream.filesize_bytes or 0) > 0]
    if video_size > 0 and known_sizes:
        within_video = [stream for stream in known_sizes if int(stream.filesize_bytes or 0) <= video_size]
        pool = within_video or [min(known_sizes, key=lambda stream: int(stream.filesize_bytes or 0))]
    video_rate = float(video.bitrate or 0)
    known_rates = [stream for stream in pool if float(stream.bitrate or 0) > 0]
    if video_rate > 0 and known_rates:
        target_rate = max(64.0, min(160.0, video_rate * 0.5))
        within_rate = [stream for stream in known_rates if float(stream.bitrate or 0) <= target_rate]
        pool = within_rate or [min(known_rates, key=lambda stream: float(stream.bitrate or 0))]
    return max(pool, key=lambda stream: (stream.bitrate or 0, stream.channels or 0, stream.sample_rate or 0, stream.stream_id))


def audio_label(stream: AudioStream) -> str:
    parts = []
    language = (stream.label or stream.language or "Audio").strip()
    parts.append(language)
    if stream.is_original:
        parts.append("Original")
    elif stream.is_default:
        parts.append("Default")
    if stream.codec:
        parts.append(stream.codec)
    if stream.bitrate:
        parts.append(f"{stream.bitrate:.0f} Kbps")
    if stream.channels:
        parts.append(f"{stream.channels} ch")
    if stream.sample_rate:
        parts.append(f"{stream.sample_rate} Hz")
    return " • ".join(parts)


def validate_audio_selection(mode: str, selected: list[str], output: str, streams: list[AudioStream]) -> tuple[str, list[str], str]:
    normalized_mode = str(mode or AUDIO_AUTO).strip().casefold()
    normalized_output = str(output or "source").strip().casefold()
    if normalized_mode not in {AUDIO_AUTO, "manual"}:
        raise ValueError("Unsupported audio selection mode")
    if normalized_output not in AUDIO_OUTPUTS:
        raise ValueError("Unsupported audio output format")
    ids = {stream.stream_id for stream in streams}
    values = list(dict.fromkeys(str(value) for value in selected if str(value) in ids))
    if normalized_mode == "manual" and streams and not values:
        raise ValueError("Select at least one audio track")
    if normalized_mode == AUDIO_AUTO:
        values = []
    return normalized_mode, values, normalized_output
