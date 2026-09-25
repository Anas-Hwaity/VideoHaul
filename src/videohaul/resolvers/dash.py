from __future__ import annotations

import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

from ..formats import resolution_family
from ..http_security import build_scoped_opener, scope_headers
from ..models import AudioStream, MediaAnalysis, VideoStream
from ..unicode_text import decode_external_bytes
from .common import AuthenticationRequired, MediaUnavailable, ResolverUnsupported, finalize_analysis, parse_iso8601_duration


class DashResolver:
    name = "dash"

    def supports(self, url: str) -> bool:
        return urlparse(url).path.lower().endswith(".mpd")

    def analyze(self, url: str, browser_cookies: str = "none", request_headers: dict[str, str] | None = None) -> MediaAnalysis:
        text, final_url = _fetch_text(url, request_headers)
        try:
            root = ET.fromstring(text)
        except ET.ParseError as exc:
            raise ResolverUnsupported("URL is not a DASH manifest") from exc
        if _local(root.tag) != "MPD":
            raise ResolverUnsupported("URL is not a DASH manifest")
        duration = parse_iso8601_duration(root.attrib.get("mediaPresentationDuration"))
        videos = []
        audios = []
        for adaptation in [node for node in root.iter() if _local(node.tag) == "AdaptationSet"]:
            content_type = str(adaptation.attrib.get("contentType") or "").lower()
            mime_type = str(adaptation.attrib.get("mimeType") or "").lower()
            language = adaptation.attrib.get("lang")
            adaptation_base = _first_base(adaptation)
            for representation in [node for node in adaptation if _local(node.tag) == "Representation"]:
                rep_mime = str(representation.attrib.get("mimeType") or mime_type).lower()
                rep_type = content_type or ("video" if rep_mime.startswith("video/") else "audio" if rep_mime.startswith("audio/") else "")
                base = _first_base(representation) or adaptation_base or ""
                direct = urljoin(final_url, base) if base else final_url
                stream_id = str(representation.attrib.get("id") or f"dash:{rep_type}:{len(videos) + len(audios)}")
                bandwidth = _float(representation.attrib.get("bandwidth"))
                codec = representation.attrib.get("codecs") or adaptation.attrib.get("codecs")
                if rep_type == "video":
                    width = _int(representation.attrib.get("width") or adaptation.attrib.get("width"))
                    height = _int(representation.attrib.get("height") or adaptation.attrib.get("height"))
                    hdr_mode = _dash_hdr(adaptation, representation, codec)
                    videos.append(VideoStream(stream_id=stream_id, resolution_family=resolution_family(height), width=width, height=height, fps=_frame_rate(representation.attrib.get("frameRate") or adaptation.attrib.get("frameRate")), bitrate=None if bandwidth is None else bandwidth / 1000.0, codec=codec, container=_container(rep_mime), hdr_mode=hdr_mode, duration_seconds=duration, direct_stream_url=direct, source_metadata={"resolver": self.name, "mime_type": rep_mime, "dynamic_range": hdr_mode}))
                elif rep_type == "audio":
                    audios.append(AudioStream(stream_id=stream_id, language=language, bitrate=None if bandwidth is None else bandwidth / 1000.0, codec=codec, container=_container(rep_mime), channels=_audio_channels(adaptation, representation), sample_rate=_int(representation.attrib.get("audioSamplingRate") or adaptation.attrib.get("audioSamplingRate")), duration_seconds=duration, direct_stream_url=direct))
        if not videos and not audios:
            raise MediaUnavailable("DASH manifest contains no playable representations")
        title = urlparse(final_url).path.rsplit("/", 1)[-1] or "DASH stream"
        dynamic = str(root.attrib.get("type") or "").lower() == "dynamic"
        return finalize_analysis(MediaAnalysis(source_url=url, canonical_url=final_url, platform=urlparse(final_url).netloc, platform_media_id=final_url, title=title, duration_seconds=duration, is_live=dynamic, video_streams=videos, audio_streams=audios, metadata={"resolver": self.name, "manifest": "dash"}, analyzed_at=time.time()))


def _fetch_text(url: str, headers: dict[str, str] | None = None) -> tuple[str, str]:
    request_headers = {"User-Agent": "VideoHaul/0.1.0"}
    request_headers.update(scope_headers(headers, url, url))
    request = urllib.request.Request(url, headers=request_headers)
    try:
        opener = build_scoped_opener(url, url, require_public=bool(headers))
        with opener.open(request, timeout=20) as response:
            raw = response.read(8 * 1024 * 1024)
            return decode_external_bytes(raw, "utf-8"), response.geturl() or url
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise AuthenticationRequired("Authentication required") from exc
        raise MediaUnavailable("DASH source is unavailable") from exc
    except Exception as exc:
        raise MediaUnavailable("DASH source is unavailable") from exc


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_base(node) -> str | None:
    for child in node:
        if _local(child.tag) == "BaseURL" and child.text:
            return child.text.strip()
    return None


def _int(value) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _float(value) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _frame_rate(value) -> float | None:
    text = str(value or "")
    if "/" in text:
        left, right = text.split("/", 1)
        try:
            return float(left) / float(right)
        except Exception:
            return None
    return _float(text)


def _container(mime_type: str) -> str:
    return "mp4" if "mp4" in mime_type else "webm" if "webm" in mime_type else "mpd"


def _audio_channels(adaptation, representation) -> int | None:
    for node in list(representation) + list(adaptation):
        if _local(node.tag) == "AudioChannelConfiguration":
            value = node.attrib.get("value")
            parsed = _int(value)
            if parsed is not None:
                return parsed
    return None


def _dash_hdr(adaptation, representation, codec: str | None) -> str | None:
    codec_text = str(codec or "").casefold()
    if "dvhe" in codec_text or "dvh1" in codec_text:
        return "Dolby Vision"
    values = []
    for node in (adaptation, representation):
        for child in node:
            local = _local(child.tag)
            if local not in {"SupplementalProperty", "EssentialProperty"}:
                continue
            scheme = str(child.attrib.get("schemeIdUri") or "").casefold()
            value = str(child.attrib.get("value") or "").strip()
            values.append((scheme, value))
    for scheme, value in values:
        lower = value.casefold()
        if "dolby" in scheme or "dovi" in lower:
            return "Dolby Vision"
        if "transfer" in scheme or "cicp" in scheme:
            if value == "18":
                return "HLG"
            if value == "16":
                return "PQ"
        if lower in {"hlg", "arib-std-b67"}:
            return "HLG"
        if lower in {"pq", "smpte2084", "smpte-st-2084"}:
            return "PQ"
    return None
