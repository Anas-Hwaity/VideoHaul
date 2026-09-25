from __future__ import annotations

from collections import defaultdict

from .models import AudioStream, VideoStream


FAMILY_ORDER = ["4320p", "2160p", "1440p", "1080p", "720p", "480p", "360p", "240p", "144p"]


def resolution_family(height: int | None) -> str:
    if not height:
        return "unknown"
    targets = [4320, 2160, 1440, 1080, 720, 480, 360, 240, 144]
    nearest = min(targets, key=lambda item: abs(item - int(height)))
    if int(height) > 4320:
        nearest = int(height)
    return f"{nearest}p"


def family_label(family: str) -> str:
    return {"4320p": "8K / 4320p", "2160p": "4K / 2160p"}.get(str(family), str(family))


def duration_text(seconds: float | None) -> str:
    if seconds is None:
        return "Duration unavailable"
    seconds = max(0, int(round(float(seconds))))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def filesize_text(size: int | None, estimated: bool) -> str:
    if size is None:
        return "Size unavailable"
    value = float(size)
    units = ["B", "KB", "MB", "GB", "TB"]
    unit = units[0]
    for candidate in units:
        unit = candidate
        if value < 1024 or candidate == units[-1]:
            break
        value /= 1024
    prefix = "~" if estimated else ""
    return f"{prefix}{value:.2f} {unit}"


def combined_filesize(stream: VideoStream, audio_streams: list[AudioStream] | None = None) -> tuple[int | None, bool]:
    if stream.filesize_bytes is None:
        return None, bool(stream.filesize_is_estimate)
    size = int(stream.filesize_bytes)
    estimated = bool(stream.filesize_is_estimate)
    if stream.has_audio or not audio_streams:
        return size, estimated
    available = [audio for audio in audio_streams if audio.filesize_bytes is not None]
    if not available:
        return size, True
    audio = max(available, key=lambda item: (item.bitrate or 0, item.filesize_bytes or 0, item.stream_id))
    return size + int(audio.filesize_bytes or 0), estimated or bool(audio.filesize_is_estimate)


def stream_label(stream: VideoStream, audio_streams: list[AudioStream] | None = None) -> str:
    parts = [stream.resolution_family]
    if stream.fps:
        parts.append(f"{stream.fps:g} FPS")
    if stream.hdr_mode:
        parts.append(stream.hdr_mode)
    if stream.codec:
        parts.append(stream.codec)
    if stream.container:
        parts.append(stream.container.upper())
    if stream.bitrate:
        parts.append(f"{stream.bitrate / 1000:.1f} Mbps" if stream.bitrate >= 1000 else f"{stream.bitrate:.0f} Kbps")
    parts.append(duration_text(stream.duration_seconds))
    size, estimated = combined_filesize(stream, audio_streams)
    parts.append(filesize_text(size, estimated))
    return " • ".join(parts)


def group_streams(streams: list[VideoStream]) -> list[tuple[str, list[VideoStream]]]:
    grouped: dict[str, list[VideoStream]] = defaultdict(list)
    for stream in streams:
        grouped[stream.resolution_family or "unknown"].append(stream)
    order_index = {name: index for index, name in enumerate(FAMILY_ORDER)}
    keys = sorted(grouped, key=lambda key: (order_index.get(key, 999), key))
    result = []
    for key in keys:
        values = sorted(grouped[key], key=lambda stream: (stream.fps or 0, stream.bitrate or 0, stream.filesize_bytes or 0), reverse=True)
        result.append((key, values))
    return result
