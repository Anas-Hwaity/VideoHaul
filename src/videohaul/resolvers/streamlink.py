from __future__ import annotations

import time
from urllib.parse import urlparse

from ..models import MediaAnalysis, VideoStream
from ..formats import resolution_family
from .common import AuthenticationRequired, MediaUnavailable, ResolverDependencyError, ResolverUnsupported, finalize_analysis


class StreamlinkResolver:
    name = "streamlink"

    def supports(self, url: str) -> bool:
        return urlparse(url).scheme.lower() in {"http", "https"}

    def analyze(self, url: str, browser_cookies: str = "none") -> MediaAnalysis:
        try:
            import streamlink
        except Exception as exc:
            raise ResolverDependencyError("Streamlink is unavailable") from exc
        try:
            session = streamlink.Streamlink()
            streams = session.streams(url)
        except Exception as exc:
            text = str(exc).lower()
            if any(value in text for value in ("login", "authentication", "forbidden", "403")):
                raise AuthenticationRequired("Authentication required") from exc
            if any(value in text for value in ("no plugin", "unsupported url")):
                raise ResolverUnsupported("Streamlink does not support this URL") from exc
            raise MediaUnavailable("Streamlink could not resolve this source") from exc
        if not streams:
            raise ResolverUnsupported("Streamlink found no playable streams")
        videos = []
        seen = set()
        for name, stream in streams.items():
            try:
                direct = stream.to_url()
            except Exception:
                direct = None
            if not direct or direct in seen:
                continue
            seen.add(direct)
            height = _height(name)
            videos.append(VideoStream(stream_id=f"streamlink:{name}", resolution_family=resolution_family(height), height=height, container=_container(direct), duration_seconds=None, direct_stream_url=direct, source_metadata={"resolver": self.name, "quality": name}))
        if not videos:
            raise MediaUnavailable("Streamlink returned no usable stream URLs")
        return finalize_analysis(MediaAnalysis(source_url=url, canonical_url=url, platform=urlparse(url).netloc, platform_media_id=url, title=urlparse(url).netloc or "Stream", is_live=True, video_streams=videos, metadata={"resolver": self.name}, analyzed_at=time.time()))


def _height(name: str) -> int | None:
    text = str(name or "").lower()
    digits = ""
    for character in text:
        if character.isdigit():
            digits += character
        elif digits:
            break
    try:
        return int(digits) if digits else None
    except Exception:
        return None


def _container(url: str) -> str:
    path = urlparse(url).path.lower()
    if path.endswith(".m3u8"):
        return "m3u8"
    if "." in path.rsplit("/", 1)[-1]:
        return path.rsplit(".", 1)[-1]
    return "stream"
