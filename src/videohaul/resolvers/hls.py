from __future__ import annotations

import re
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

from ..formats import resolution_family
from ..http_security import build_scoped_opener, scope_headers
from ..models import AudioStream, MediaAnalysis, VideoStream
from ..unicode_text import decode_external_bytes
from .common import AuthenticationRequired, MediaUnavailable, ResolverUnsupported, classify_http_status, codec_parts, finalize_analysis


class HlsResolver:
    name = "hls"

    def supports(self, url: str) -> bool:
        return urlparse(url).path.lower().endswith(".m3u8")

    def analyze(self, url: str, browser_cookies: str = "none", request_headers: dict[str, str] | None = None) -> MediaAnalysis:
        text, final_url = _fetch_text(url, request_headers)
        if "#EXTM3U" not in text[:256]:
            raise ResolverUnsupported("URL is not an HLS manifest")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        videos = []
        audios = []
        for index, line in enumerate(lines):
            if line.startswith("#EXT-X-STREAM-INF:") and index + 1 < len(lines):
                attributes = _attributes(line.split(":", 1)[1])
                uri = lines[index + 1]
                if uri.startswith("#"):
                    continue
                width, height = _resolution(attributes.get("RESOLUTION"))
                video_codec, audio_codec = codec_parts(attributes.get("CODECS"))
                bandwidth = _float(attributes.get("AVERAGE-BANDWIDTH") or attributes.get("BANDWIDTH"))
                video_range = _video_range(attributes.get("VIDEO-RANGE"))
                videos.append(VideoStream(stream_id=f"hls:v:{len(videos)}", resolution_family=resolution_family(height), width=width, height=height, fps=_float(attributes.get("FRAME-RATE")), bitrate=None if bandwidth is None else bandwidth / 1000.0, codec=video_codec, container="m3u8", hdr_mode=video_range, duration_seconds=None, has_audio=bool(audio_codec or attributes.get("AUDIO")), direct_stream_url=urljoin(final_url, uri), source_metadata={"resolver": self.name, "audio_group": attributes.get("AUDIO"), "codecs": attributes.get("CODECS"), "dynamic_range": video_range}))
            if line.startswith("#EXT-X-MEDIA:"):
                attributes = _attributes(line.split(":", 1)[1])
                if str(attributes.get("TYPE") or "").upper() != "AUDIO" or not attributes.get("URI"):
                    continue
                audios.append(AudioStream(stream_id=f"hls:a:{len(audios)}", language=attributes.get("LANGUAGE"), label=attributes.get("NAME"), codec=None, container="m3u8", duration_seconds=None, direct_stream_url=urljoin(final_url, attributes["URI"])))
        if not videos and not audios:
            videos.append(VideoStream(stream_id="hls:source", resolution_family="unknown", container="m3u8", duration_seconds=_media_duration(lines), direct_stream_url=final_url, source_metadata={"resolver": self.name}))
        title = urlparse(final_url).path.rsplit("/", 1)[-1] or "HLS stream"
        return finalize_analysis(MediaAnalysis(source_url=url, canonical_url=final_url, platform=urlparse(final_url).netloc, platform_media_id=final_url, title=title, duration_seconds=_media_duration(lines), is_live="#EXT-X-ENDLIST" not in text, video_streams=videos, audio_streams=audios, metadata={"resolver": self.name, "manifest": "hls"}, analyzed_at=time.time()))


def _fetch_text(url: str, headers: dict[str, str] | None = None) -> tuple[str, str]:
    request_headers = {"User-Agent": "VideoHaul/0.1.0"}
    request_headers.update(scope_headers(headers, url, url))
    request = urllib.request.Request(url, headers=request_headers)
    try:
        opener = build_scoped_opener(url, url, require_public=bool(headers))
        with opener.open(request, timeout=20) as response:
            raw = response.read(4 * 1024 * 1024)
            return decode_external_bytes(raw, "utf-8"), response.geturl() or url
    except urllib.error.HTTPError as exc:
        classified = classify_http_status(exc.code, getattr(exc, "headers", None))
        if classified is not None:
            raise classified from exc
        raise MediaUnavailable("HLS source is unavailable") from exc
    except Exception as exc:
        raise MediaUnavailable("HLS source is unavailable") from exc


def _attributes(value: str) -> dict[str, str]:
    result = {}
    for match in re.finditer(r'([A-Z0-9-]+)=("(?:[^"\\]|\\.)*"|[^,]*)', value):
        raw = match.group(2).strip()
        result[match.group(1)] = raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] == '"' else raw
    return result


def _resolution(value: str | None) -> tuple[int | None, int | None]:
    if not value or "x" not in value.lower():
        return None, None
    left, right = value.lower().split("x", 1)
    try:
        return int(left), int(right)
    except Exception:
        return None, None


def _float(value) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _media_duration(lines: list[str]) -> float | None:
    durations = []
    for line in lines:
        if line.startswith("#EXTINF:"):
            value = line.split(":", 1)[1].split(",", 1)[0]
            parsed = _float(value)
            if parsed is not None:
                durations.append(parsed)
    return sum(durations) if durations else None


def _video_range(value: str | None) -> str | None:
    text = str(value or "").strip().upper()
    if text == "SDR":
        return "SDR"
    if text == "HLG":
        return "HLG"
    if text == "PQ":
        return "PQ"
    return text or None
