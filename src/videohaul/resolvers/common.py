from __future__ import annotations

from dataclasses import replace
import re
import time
from urllib.parse import parse_qs, urlparse

from ..models import MediaAnalysis
from ..quality import normalize_analysis_streams


class ResolverError(RuntimeError):
    pass


class ResolverUnsupported(ResolverError):
    pass


class MediaUnavailable(ResolverError):
    pass


class MediaUnresolved(ResolverError):
    pass


class AuthenticationRequired(ResolverError):
    pass


class ResolverDependencyError(ResolverError):
    pass


class SourceRefusedRequest(ResolverError):
    pass


AUTHENTICATION_EVIDENCE = (
    "sign in to confirm",
    "sign in to view",
    "login required",
    "requires authentication",
    "use --cookies",
    "cookies-from-browser",
    "members-only",
    "subscribe to this channel",
    "paid content",
    "premium subscribers",
    "this video is private",
    "private video",
)


def classify_http_status(status: int, headers=None, body: str = ""):
    code = int(status or 0)
    challenge = ""
    if headers is not None:
        try:
            challenge = str(headers.get("WWW-Authenticate") or "")
        except Exception:
            challenge = ""
    if code == 401 or challenge:
        return AuthenticationRequired("Authentication required")
    if code == 402:
        return AuthenticationRequired("Authentication required")
    if code == 403:
        lowered = str(body or "").casefold()
        if any(value in lowered for value in AUTHENTICATION_EVIDENCE):
            return AuthenticationRequired("Authentication required")
        return SourceRefusedRequest("The source refused this request without a browser session")
    return None


def authentication_from_text(text: str) -> bool:
    lowered = str(text or "").casefold()
    return any(value in lowered for value in AUTHENTICATION_EVIDENCE)


def validate_media_url(url: str) -> str:
    value = str(url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Enter a valid http or https media URL")
    return value


def infer_expiry(urls: list[str]) -> float | None:
    values = []
    now = time.time()
    for value in urls:
        try:
            query = parse_qs(urlparse(value).query)
        except Exception:
            continue
        for name in ("expire", "expires", "exp"):
            for raw in query.get(name, []):
                try:
                    timestamp = float(raw)
                except Exception:
                    continue
                if timestamp > now - 300 and timestamp < now + 60 * 60 * 24 * 365 * 10:
                    values.append(timestamp)
    return min(values) if values else None


def finalize_analysis(analysis: MediaAnalysis) -> MediaAnalysis:
    if isinstance(analysis, MediaAnalysis):
        analysis = normalize_analysis_streams(analysis)
    if not analysis.analyzed_at:
        analysis = replace(analysis, analyzed_at=time.time())
    if analysis.expires_at is None:
        urls = [stream.direct_stream_url or "" for stream in analysis.video_streams]
        urls.extend(stream.direct_stream_url or "" for stream in analysis.audio_streams)
        expiry = infer_expiry([value for value in urls if value])
        if expiry is not None:
            analysis = replace(analysis, expires_at=expiry)
    return analysis


def codec_parts(value: str | None) -> tuple[str | None, str | None]:
    pieces = [item.strip() for item in str(value or "").split(",") if item.strip()]
    video = None
    audio = None
    for piece in pieces:
        lower = piece.lower()
        if lower.startswith(("avc", "hvc", "hev", "av01", "vp8", "vp9", "dvhe", "dvh1")):
            video = piece
        elif lower.startswith(("mp4a", "opus", "vorbis", "ac-3", "ec-3", "aac")):
            audio = piece
    return video, audio


def parse_iso8601_duration(value: str | None) -> float | None:
    match = re.fullmatch(r"P(?:(?P<days>[\d.]+)D)?(?:T(?:(?P<hours>[\d.]+)H)?(?:(?P<minutes>[\d.]+)M)?(?:(?P<seconds>[\d.]+)S)?)?", str(value or ""))
    if not match:
        return None
    days = float(match.group("days") or 0)
    hours = float(match.group("hours") or 0)
    minutes = float(match.group("minutes") or 0)
    seconds = float(match.group("seconds") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds
