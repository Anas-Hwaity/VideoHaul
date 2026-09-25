from __future__ import annotations

from dataclasses import replace
import math
import re

from .formats import resolution_family
from .models import AudioStream, MediaAnalysis, VideoStream

BEST_SELECTOR = "bestvideo*+bestaudio/best"
AUDIO_ONLY_SELECTOR = "audio_only"
DEVICE_PRESETS = ("phone", "tv", "archive", "editing", "small_file")
SORT_KEYS = ("resolution", "bitrate", "size", "codec", "fps", "hdr", "container")


def normalize_analysis_streams(analysis: MediaAnalysis) -> MediaAnalysis:
    videos = [normalize_video_stream(stream, analysis.duration_seconds) for stream in analysis.video_streams]
    audios = [normalize_audio_stream(stream, analysis.duration_seconds) for stream in analysis.audio_streams]
    duration = analysis.duration_seconds
    if duration is None:
        durations = [stream.duration_seconds for stream in [*videos, *audios] if stream.duration_seconds is not None]
        if durations:
            duration = max(durations)
            videos = [normalize_video_stream(stream, duration) for stream in videos]
            audios = [normalize_audio_stream(stream, duration) for stream in audios]
    return replace(analysis, duration_seconds=duration, video_streams=videos, audio_streams=audios)


def normalize_video_stream(stream: VideoStream, media_duration: float | None) -> VideoStream:
    duration = _positive_float(stream.duration_seconds)
    if duration is None:
        duration = _positive_float(media_duration)
    height = _positive_int(stream.height)
    width = _positive_int(stream.width)
    bitrate = _positive_float(stream.bitrate)
    fps = _positive_float(stream.fps)
    size = _positive_int(stream.filesize_bytes)
    estimated = bool(stream.filesize_is_estimate)
    if size is None and bitrate is not None and duration is not None:
        size = estimate_size_bytes(bitrate, duration)
        estimated = size is not None
    family = stream.resolution_family or "unknown"
    if family == "unknown" and height is not None:
        family = resolution_family(height)
    container = _clean_token(stream.container)
    codec = _clean_token(stream.codec)
    hdr = normalize_hdr(stream.hdr_mode, codec, stream.source_metadata)
    return replace(
        stream,
        resolution_family=family,
        width=width,
        height=height,
        fps=fps,
        bitrate=bitrate,
        codec=codec,
        container=container,
        hdr_mode=hdr,
        duration_seconds=duration,
        filesize_bytes=size,
        filesize_is_estimate=estimated,
    )


def normalize_audio_stream(stream: AudioStream, media_duration: float | None) -> AudioStream:
    duration = _positive_float(stream.duration_seconds)
    if duration is None:
        duration = _positive_float(media_duration)
    bitrate = _positive_float(stream.bitrate)
    size = _positive_int(stream.filesize_bytes)
    estimated = bool(stream.filesize_is_estimate)
    if size is None and bitrate is not None and duration is not None:
        size = estimate_size_bytes(bitrate, duration)
        estimated = size is not None
    return replace(
        stream,
        bitrate=bitrate,
        codec=_clean_token(stream.codec),
        container=_clean_token(stream.container),
        channels=_positive_int(stream.channels),
        sample_rate=_positive_int(stream.sample_rate),
        duration_seconds=duration,
        filesize_bytes=size,
        filesize_is_estimate=estimated,
    )


def estimate_size_bytes(bitrate_kbps: float | None, duration_seconds: float | None) -> int | None:
    bitrate = _positive_float(bitrate_kbps)
    duration = _positive_float(duration_seconds)
    if bitrate is None or duration is None:
        return None
    return max(1, int(round(bitrate * 1000.0 * duration / 8.0)))


def normalize_hdr(value: str | None, codec: str | None = None, metadata: dict | None = None) -> str | None:
    candidates = [value]
    values = dict(metadata or {})
    candidates.extend([values.get("dynamic_range"), values.get("format_note"), values.get("hdr")])
    text = " ".join(str(item or "") for item in candidates).strip().casefold()
    codec_text = str(codec or "").casefold()
    if "dolby vision" in text or "dovi" in text or "dvhe" in codec_text or "dvh1" in codec_text:
        return "Dolby Vision"
    if "hlg" in text:
        return "HLG"
    if "hdr10+" in text or "hdr10plus" in text or "hdr10 plus" in text:
        return "HDR10+"
    if "hdr10" in text or re.search(r"(?:^|\W)hdr(?:$|\W)", text):
        return "HDR10"
    if str(value or "").strip().upper() == "SDR" or re.search(r"(?:^|\W)sdr(?:$|\W)", text):
        return "SDR"
    raw = str(value or "").strip()
    if raw and raw.upper() not in {"NONE", "UNKNOWN"}:
        return raw
    return None


def best_stream(streams: list[VideoStream]) -> VideoStream | None:
    if not streams:
        return None
    return max(streams, key=_best_key)


def recommended_stream(streams: list[VideoStream]) -> VideoStream | None:
    if not streams:
        return None
    return max(streams, key=_recommended_key)


def select_preferred_stream(streams: list[VideoStream], preference: str | None) -> str | None:
    if not streams:
        return None
    value = str(preference or "best").strip().casefold()
    if value in {"", "best", "best_available"}:
        return BEST_SELECTOR
    if value == "recommended":
        selected = recommended_stream(streams)
        return selected.stream_id if selected else BEST_SELECTOR
    if value.startswith("preset:"):
        selected = select_device_preset(streams, value.split(":", 1)[1])
        return selected.stream_id if selected else BEST_SELECTOR
    match = re.fullmatch(r"(\d{3,4})p(?:(\d{2,3}))?", value)
    if match:
        target_height = int(match.group(1))
        target_fps = int(match.group(2)) if match.group(2) else None
        candidates = _height_candidates(streams, target_height)
        if target_fps is not None:
            candidates = sorted(candidates, key=lambda stream: (_fps_distance(stream, target_fps), -_best_key(stream)[4]))
            return candidates[0].stream_id if candidates else BEST_SELECTOR
        return max(candidates, key=_best_key).stream_id if candidates else BEST_SELECTOR
    selected = next((stream for stream in streams if stream.stream_id == preference), None)
    return selected.stream_id if selected else BEST_SELECTOR


def select_device_preset(streams: list[VideoStream], preset: str) -> VideoStream | None:
    value = str(preset or "").strip().casefold()
    if value not in DEVICE_PRESETS or not streams:
        return None
    if value == "archive":
        return best_stream(streams)
    if value == "phone":
        candidates = [stream for stream in streams if (stream.height or 0) <= 1080] or list(streams)
        return max(candidates, key=lambda stream: (_compatibility_score(stream), _best_key(stream)))
    if value == "tv":
        candidates = [stream for stream in streams if (stream.height or 0) <= 2160] or list(streams)
        return max(candidates, key=lambda stream: ((stream.height or 0), min(stream.fps or 0, 60), _tv_codec_score(stream), stream.bitrate or 0))
    if value == "editing":
        candidates = [stream for stream in streams if _container_score(stream) >= 3 and _editing_codec_score(stream) >= 2] or list(streams)
        return max(candidates, key=lambda stream: (_editing_codec_score(stream), _container_score(stream), _best_key(stream)))
    target = [stream for stream in streams if 360 <= (stream.height or 0) <= 720]
    candidates = target or [stream for stream in streams if (stream.height or 0) <= 720] or list(streams)
    return min(candidates, key=lambda stream: (_effective_size(stream), -(stream.height or 0), -(stream.fps or 0)))


def sort_streams(streams: list[VideoStream], key: str, descending: bool = True) -> list[VideoStream]:
    value = str(key or "resolution").casefold()
    if value not in SORT_KEYS:
        raise ValueError(f"Unsupported stream sort: {key}")
    getters = {
        "resolution": lambda stream: (stream.height or 0, stream.width or 0),
        "bitrate": lambda stream: stream.bitrate or 0,
        "size": lambda stream: stream.filesize_bytes or 0,
        "codec": lambda stream: str(stream.codec or "").casefold(),
        "fps": lambda stream: stream.fps or 0,
        "hdr": lambda stream: _hdr_score(stream),
        "container": lambda stream: str(stream.container or "").casefold(),
    }
    return sorted(streams, key=lambda stream: (getters[value](stream), stream.stream_id), reverse=bool(descending))


def _height_candidates(streams: list[VideoStream], target: int) -> list[VideoStream]:
    exact = [stream for stream in streams if stream.height == target or stream.resolution_family == f"{target}p"]
    if exact:
        return exact
    lower = [stream for stream in streams if stream.height is not None and stream.height <= target]
    if lower:
        highest = max(stream.height or 0 for stream in lower)
        return [stream for stream in lower if (stream.height or 0) == highest]
    higher = [stream for stream in streams if stream.height is not None]
    if higher:
        lowest = min(stream.height or 0 for stream in higher)
        return [stream for stream in higher if (stream.height or 0) == lowest]
    return list(streams)


def _best_key(stream: VideoStream) -> tuple:
    return (
        stream.height or 0,
        stream.width or 0,
        stream.fps or 0,
        _hdr_score(stream),
        stream.bitrate or 0,
        _codec_quality_score(stream),
        -_effective_size(stream),
        stream.stream_id,
    )


def _recommended_key(stream: VideoStream) -> tuple:
    height = stream.height or 0
    height_score = min(height, 2160) / 2160.0 * 40.0
    fps_score = min(stream.fps or 0, 60) / 60.0 * 12.0
    compatibility = _compatibility_score(stream)
    hdr = 3.0 if _hdr_score(stream) > 1 else 0.0
    bitrate = stream.bitrate or 0
    efficiency_penalty = min(8.0, bitrate / 5000.0) if bitrate else 0.0
    score = height_score + fps_score + compatibility + hdr - efficiency_penalty
    return (round(score, 6), _container_score(stream), _codec_quality_score(stream), -_effective_size(stream), stream.stream_id)


def _compatibility_score(stream: VideoStream) -> float:
    return float(_container_score(stream) * 4 + _compat_codec_score(stream) * 4)


def _container_score(stream: VideoStream) -> int:
    value = str(stream.container or "").casefold()
    if value in {"mp4", "m4v", "mov"}:
        return 4
    if value in {"webm", "mkv"}:
        return 3
    if value in {"ts", "m3u8"}:
        return 2
    return 1 if value else 0


def _compat_codec_score(stream: VideoStream) -> int:
    value = str(stream.codec or "").casefold()
    if value.startswith(("h264", "avc", "avc1")):
        return 5
    if value.startswith(("hevc", "h265", "hev", "hvc")):
        return 4
    if value.startswith(("vp9", "vp09")):
        return 3
    if value.startswith(("av1", "av01")):
        return 2
    return 1 if value else 0


def _tv_codec_score(stream: VideoStream) -> int:
    value = str(stream.codec or "").casefold()
    if value.startswith(("av1", "av01")):
        return 5
    if value.startswith(("hevc", "h265", "hev", "hvc")):
        return 4
    if value.startswith(("vp9", "vp09")):
        return 3
    if value.startswith(("h264", "avc", "avc1")):
        return 2
    return 1 if value else 0


def _editing_codec_score(stream: VideoStream) -> int:
    value = str(stream.codec or "").casefold()
    if value.startswith(("h264", "avc", "avc1")):
        return 4
    if value.startswith(("hevc", "h265", "hev", "hvc")):
        return 3
    if value.startswith(("prores", "dnx")):
        return 5
    return 1


def _codec_quality_score(stream: VideoStream) -> int:
    value = str(stream.codec or "").casefold()
    if value.startswith(("av1", "av01")):
        return 5
    if value.startswith(("hevc", "h265", "hev", "hvc")):
        return 4
    if value.startswith(("vp9", "vp09")):
        return 3
    if value.startswith(("h264", "avc", "avc1")):
        return 2
    return 1 if value else 0


def _hdr_score(stream: VideoStream) -> int:
    value = str(stream.hdr_mode or "").casefold()
    if "dolby" in value:
        return 5
    if "hdr10+" in value:
        return 4
    if "hdr" in value:
        return 3
    if "hlg" in value:
        return 2
    if "sdr" in value:
        return 1
    return 0


def _effective_size(stream: VideoStream) -> int:
    return int(stream.filesize_bytes or math.inf) if stream.filesize_bytes is not None else 2**63 - 1


def _fps_distance(stream: VideoStream, target: int) -> tuple[float, float]:
    fps = stream.fps or 0
    return (abs(fps - target), -fps)


def _clean_token(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _positive_int(value) -> int | None:
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def _positive_float(value) -> float | None:
    try:
        parsed = float(value)
    except Exception:
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed
