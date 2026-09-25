from __future__ import annotations

import json
import ipaddress
import re
import socket
import time
import urllib.error
import urllib.request

from .http_security import ScopedRedirectHandler, build_scoped_opener, require_public_http_url
from urllib.parse import parse_qsl, urlparse, urlunparse

from .models import DetectedMediaCandidate
from .resolvers.common import infer_expiry
from .unicode_text import decode_external_bytes

DIRECT_VIDEO_EXTENSIONS = {".mp4", ".m4v", ".webm", ".mkv", ".mov", ".ogv", ".avi", ".flv", ".ts", ".3gp"}
DIRECT_AUDIO_EXTENSIONS = {".m4a", ".mp3", ".aac", ".opus", ".ogg", ".oga", ".flac", ".wav", ".weba"}
HLS_EXTENSIONS = {".m3u8", ".m3u"}
DASH_EXTENSIONS = {".mpd"}
SEGMENT_EXTENSIONS = {".ts", ".m4s", ".cmfv", ".cmfa", ".aac", ".vtt", ".init"}

VIDEO_MIME_PREFIXES = ("video/",)
AUDIO_MIME_PREFIXES = ("audio/",)
HLS_MIME_TYPES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
    "application/mpegurl",
}
DASH_MIME_TYPES = {"application/dash+xml", "video/vnd.mpeg.dash.mpd"}

GENERIC_MIME_TYPES = {
    "",
    "text/plain",
    "text/html",
    "application/octet-stream",
    "binary/octet-stream",
    "application/json",
    "application/xml",
    "text/xml",
    "application/x-www-form-urlencoded",
}

SNIFF_BYTES = 8192
SNIFFABLE_RESOURCE_TYPES = {"", "xhr", "fetch", "other", "media"}
HTML_SNIFFABLE_RESOURCE_TYPES = {"xhr", "fetch"}

SUBTITLE_SNIFF_TYPES = {"webvtt", "ass"}

VOLATILE_QUERY_KEYS = {
    "range",
    "rn",
    "rbuf",
    "sq",
    "cver",
    "alr",
    "ump",
    "_nc_cb",
    "bytestart",
    "byteend",
    "start",
    "end",
    "offset",
    "seq",
    "segment",
    "chunk",
    "t",
    "_",
}

SECRET_QUERY_KEYS = {
    "token",
    "access_token",
    "auth",
    "authorization",
    "key",
    "signature",
    "sig",
    "hmac",
    "password",
    "session",
    "sessionid",
    "policy",
    "credential",
    "secret",
    "apikey",
    "api_key",
    "x-amz-algorithm",
    "x-amz-credential",
    "x-amz-date",
    "x-amz-expires",
    "x-amz-security-token",
    "x-amz-signature",
    "x-amz-signedheaders",
    "x-goog-algorithm",
    "x-goog-credential",
    "x-goog-date",
    "x-goog-expires",
    "x-goog-signature",
    "x-goog-signedheaders",
    "cloudfront-policy",
    "cloudfront-signature",
    "cloudfront-key-pair-id",
    "key-pair-id",
    "keypairid",
    "expires",
    "expiry",
    "jwt",
    "hdnts",
    "hdntl",
}

def secret_query_key(value: str) -> bool:
    key = str(value or "").casefold()
    return key in SECRET_QUERY_KEYS or key.startswith(("x-amz-", "x-goog-", "cloudfront-"))


ADVERTISING_HINTS = (
    "/ads/",
    "/ad/",
    "adserver",
    "doubleclick",
    "googlesyndication",
    "adsystem",
    "/preroll",
    "/midroll",
    "/bumper",
    "/promo/",
    "/trailer/",
    "/intro/",
    "/tracking",
    "analytics",
    "beacon",
)

OBSERVATION_NOISE_HINTS = (
    "cookie-sync",
    "cookiesync",
    "/sync/",
    "/usersync",
    "cm-notify",
    "pixel.",
    "/pixel",
    "/collect",
    "/beacon",
    "rubiconproject",
    "openx.net",
    "criteo",
    "casalemedia",
    "smartadserver",
    "doubleclick",
    "googlesyndication",
    "taboola",
)

RESOLUTION_HINT = re.compile(r"(?<!\d)(\d{3,4})[pP](?!\d)")
SEGMENT_HINT = re.compile(r"(?:seg(?:ment)?[-_]?\d+|chunk[-_]?\d+|frag(?:ment)?[-_]?\d+|/\d{4,}\.(?:ts|m4s)$)", re.IGNORECASE)


def path_extension(url: str) -> str:
    try:
        path = urlparse(str(url or "")).path
    except Exception:
        return ""
    index = path.rfind(".")
    if index < 0:
        return ""
    return path[index:].lower()


def normalized_mime(value: str) -> str:
    return str(value or "").split(";", 1)[0].strip().lower()


def classify_media(url: str, mime_type: str = "") -> str:
    mime = normalized_mime(mime_type)
    extension = path_extension(url)
    if mime in HLS_MIME_TYPES or extension in HLS_EXTENSIONS:
        return "hls"
    if mime in DASH_MIME_TYPES or extension in DASH_EXTENSIONS:
        return "dash"
    if mime.startswith(VIDEO_MIME_PREFIXES):
        return "video"
    if mime.startswith(AUDIO_MIME_PREFIXES):
        return "audio"
    if extension in DIRECT_VIDEO_EXTENSIONS:
        return "video"
    if extension in DIRECT_AUDIO_EXTENSIONS:
        return "audio"
    return "unknown"


def is_media_reference(url: str, mime_type: str = "") -> bool:
    return classify_media(url, mime_type) != "unknown"


def sniff_media_body(payload: bytes | str) -> str:
    if isinstance(payload, str):
        data = payload.encode("utf-8", "surrogatepass")
    else:
        data = bytes(payload or b"")
    if not data:
        return ""
    head = data[:SNIFF_BYTES]
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n")
    if stripped.startswith(b"#EXTM3U"):
        return "hls"
    if stripped.startswith(b"WEBVTT"):
        return "webvtt"
    if stripped.startswith(b"[Script Info]"):
        return "ass"
    lowered = stripped[:2048].lower()
    if stripped.startswith(b"<?xml") or stripped.startswith(b"<mpd"):
        if b"<mpd" in lowered or b"urn:mpeg:dash:schema" in lowered:
            return "dash"
    if b"<mpd" in lowered and b"urn:mpeg:dash" in lowered:
        return "dash"
    return ""


def is_generic_mime(mime_type: str) -> bool:
    return normalized_mime(mime_type) in GENERIC_MIME_TYPES


def should_sniff(url: str, mime_type: str = "", resource_type: str = "") -> bool:
    if classify_media(url, mime_type) != "unknown":
        return False
    kind = str(resource_type or "").lower()
    if kind not in SNIFFABLE_RESOURCE_TYPES:
        return False
    if not is_generic_mime(mime_type):
        return False
    if normalized_mime(mime_type) == "text/html" and kind not in HTML_SNIFFABLE_RESOURCE_TYPES:
        return False
    return True


def kind_for_sniffed(sniffed: str) -> str:
    value = str(sniffed or "")
    if value in {"hls", "dash"}:
        return value
    return "unknown"


def is_manifest_kind(kind: str) -> bool:
    return str(kind or "") in {"hls", "dash"}


def looks_like_segment(url: str, mime_type: str = "") -> bool:
    value = str(url or "")
    extension = path_extension(value)
    if extension in HLS_EXTENSIONS or extension in DASH_EXTENSIONS:
        return False
    if SEGMENT_HINT.search(value):
        return True
    if extension in SEGMENT_EXTENSIONS and extension not in DIRECT_VIDEO_EXTENSIONS - {".ts"}:
        return True
    return False


def looks_like_advertising(url: str) -> bool:
    lowered = str(url or "").lower()
    return any(hint in lowered for hint in ADVERTISING_HINTS)


def looks_like_observation_noise(url: str) -> bool:
    lowered = str(url or "").casefold()
    return any(hint in lowered for hint in OBSERVATION_NOISE_HINTS)


def redact_url(url: str) -> str:
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return ""
    netloc = parsed.netloc
    if parsed.username is not None or parsed.password is not None:
        host = parsed.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = host + (f":{port}" if port else "")
    pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        pairs.append(f"{key}=redacted" if secret_query_key(key) else f"{key}={value}")
    return urlunparse(parsed._replace(netloc=netloc, query="&".join(pairs)))


def filename_from_disposition(value: str) -> str:
    text = str(value or "")
    if not text:
        return ""
    match = re.search(r"filename\*\s*=\s*[^\']*\'[^\']*\'([^;]+)", text, re.IGNORECASE)
    if match:
        from urllib.parse import unquote

        return unquote(match.group(1).strip().strip('"')).strip()
    match = re.search(r'filename\s*=\s*"([^"]+)"', text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r"filename\s*=\s*([^;]+)", text, re.IGNORECASE)
    if match:
        return match.group(1).strip().strip('"')
    return ""


def url_carries_secrets(url: str) -> bool:
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return False
    if parsed.username is not None or parsed.password is not None:
        return True
    return any(secret_query_key(key) for key, _ in parse_qsl(parsed.query, keep_blank_values=True))


def dedup_key(url: str) -> str:
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return str(url or "").casefold()
    pairs = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in VOLATILE_QUERY_KEYS and not secret_query_key(key)]
    query = "&".join(f"{key}={value}" for key, value in sorted(pairs))
    netloc = parsed.netloc.casefold()
    path = parsed.path
    return urlunparse(("", netloc, path, "", query, "")).casefold()


def host_of(url: str) -> str:
    try:
        return urlparse(str(url or "")).netloc
    except Exception:
        return ""


def build_candidate(
    page_url: str,
    discovered_url: str,
    discovery_method: str,
    mime_type: str = "",
    final_url: str = "",
    source_frame_url: str = "",
    frame_depth: int = 0,
    request_method: str = "GET",
    status_code: int | None = None,
    page_title: str = "",
    poster_or_thumbnail: str = "",
    referer: str = "",
    user_agent: str = "",
    required_headers: dict | None = None,
    required_cookie_scope: str = "",
    filesize_bytes: int | None = None,
) -> DetectedMediaCandidate:
    resolved = str(final_url or discovered_url or "")
    kind = classify_media(resolved, mime_type)
    extension = path_extension(resolved).lstrip(".")
    candidate = DetectedMediaCandidate(
        page_url=str(page_url or ""),
        page_title=str(page_title or ""),
        discovered_url=str(discovered_url or ""),
        final_url=resolved,
        source_frame_url=str(source_frame_url or ""),
        frame_depth=max(0, int(frame_depth or 0)),
        request_method=str(request_method or "GET").upper(),
        status_code=status_code,
        mime_type=normalized_mime(mime_type),
        media_kind=kind,
        container=extension,
        host=host_of(resolved),
        is_manifest=is_manifest_kind(kind),
        poster_or_thumbnail=str(poster_or_thumbnail or ""),
        referer=str(referer or ""),
        user_agent=str(user_agent or ""),
        required_headers=dict(required_headers or {}),
        required_cookie_scope=str(required_cookie_scope or ""),
        filesize_bytes=filesize_bytes,
        discovery_method=str(discovery_method or ""),
        discovered_at=time.time(),
    )
    expiry = infer_expiry([resolved])
    if expiry is not None:
        candidate.expiry_if_known = expiry
    match = RESOLUTION_HINT.search(resolved)
    if match:
        try:
            candidate.height = int(match.group(1))
        except Exception:
            candidate.height = None
    return candidate


def merge_candidate(existing: DetectedMediaCandidate, incoming: DetectedMediaCandidate) -> DetectedMediaCandidate:
    for name in (
        "final_url",
        "mime_type",
        "container",
        "title",
        "page_title",
        "video_codec",
        "audio_codec",
        "hdr_mode",
        "poster_or_thumbnail",
        "referer",
        "user_agent",
        "required_cookie_scope",
        "verification_detail",
        "host",
        "source_frame_url",
        "resource_type",
        "initiator_url",
        "content_disposition",
        "suggested_filename",
        "sniffed_type",
        "language",
        "split_audio_url",
    ):
        if not getattr(existing, name) and getattr(incoming, name):
            setattr(existing, name, getattr(incoming, name))
    if incoming.frame_depth > existing.frame_depth:
        existing.frame_depth = incoming.frame_depth
    if incoming.variants and not existing.variants:
        existing.variants = list(incoming.variants)
        existing.variant_count = incoming.variant_count
    if incoming.first_seen_at and (not existing.first_seen_at or incoming.first_seen_at < existing.first_seen_at):
        existing.first_seen_at = incoming.first_seen_at
    if incoming.last_seen_at and incoming.last_seen_at > existing.last_seen_at:
        existing.last_seen_at = incoming.last_seen_at
    channels = [part for part in str(existing.discovery_channel or "").split("+") if part]
    for part in str(incoming.discovery_channel or "").split("+"):
        if part and part not in channels:
            channels.append(part)
    existing.discovery_channel = "+".join(channels)
    for name in ("width", "height", "fps", "duration_seconds", "bitrate", "audio_channels", "audio_sample_rate", "filesize_bytes", "expiry_if_known", "status_code"):
        if getattr(existing, name) is None and getattr(incoming, name) is not None:
            setattr(existing, name, getattr(incoming, name))
    if existing.media_kind == "unknown" and incoming.media_kind != "unknown":
        existing.media_kind = incoming.media_kind
        existing.is_manifest = incoming.is_manifest
    if incoming.required_headers:
        merged = dict(incoming.required_headers)
        merged.update(existing.required_headers)
        existing.required_headers = merged
    if incoming.is_live:
        existing.is_live = True
    if incoming.filesize_is_estimate and existing.filesize_bytes is None:
        existing.filesize_is_estimate = True
    if incoming.verification_state != "unverified" and existing.verification_state == "unverified":
        existing.verification_state = incoming.verification_state
    methods = [part for part in str(existing.discovery_method or "").split("+") if part]
    for part in str(incoming.discovery_method or "").split("+"):
        if part and part not in methods:
            methods.append(part)
    existing.discovery_method = "+".join(methods)
    return existing


class CandidateCollection:
    def __init__(self, limit: int = 200):
        self.limit = max(1, int(limit))
        self._order: list[str] = []
        self._items: dict[str, DetectedMediaCandidate] = {}
        self._segment_counts: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._order)

    def add(self, candidate: DetectedMediaCandidate) -> DetectedMediaCandidate | None:
        target = candidate.final_url or candidate.discovered_url
        if not target:
            return None
        if looks_like_segment(target, candidate.mime_type):
            parent = dedup_key(target.rsplit("/", 1)[0])
            self._segment_counts[parent] = self._segment_counts.get(parent, 0) + 1
            return None
        key = dedup_key(target)
        current = self._items.get(key)
        if current is not None:
            return merge_candidate(current, candidate)
        if len(self._order) >= self.limit:
            return None
        self._items[key] = candidate
        self._order.append(key)
        return candidate

    def segment_activity(self, candidate: DetectedMediaCandidate) -> int:
        target = str(candidate.final_url or candidate.discovered_url)
        if not target:
            return 0
        prefix = dedup_key(target.rsplit("/", 1)[0])
        total = 0
        for parent, count in self._segment_counts.items():
            if parent == prefix or parent.startswith(prefix + "/"):
                total += int(count)
        return total

    def values(self) -> list[DetectedMediaCandidate]:
        return [self._items[key] for key in self._order]


def score_candidate(candidate: DetectedMediaCandidate, page_duration: float | None = None, segment_activity: int = 0) -> DetectedMediaCandidate:
    evidence: list[str] = []
    score = 0.0
    if candidate.media_kind in {"video", "hls", "dash"}:
        score += 3.0
        evidence.append(f"Media kind {candidate.media_kind}")
    elif candidate.media_kind == "audio":
        score += 1.0
        evidence.append("Audio-only media kind")
    if candidate.verification_state == "verified":
        score += 3.0
        evidence.append("Verified by probe")
    elif candidate.verification_state == "probe_failed":
        score -= 2.0
        evidence.append("Probe could not confirm this candidate")
    if candidate.duration_seconds is not None:
        if candidate.duration_seconds >= 120:
            score += 2.5
            evidence.append(f"Duration {int(candidate.duration_seconds)}s is feature length")
        elif candidate.duration_seconds >= 30:
            score += 0.5
            evidence.append(f"Duration {int(candidate.duration_seconds)}s")
        else:
            score -= 2.0
            evidence.append(f"Short duration {int(candidate.duration_seconds)}s suggests a clip or advert")
    if page_duration and candidate.duration_seconds:
        delta = abs(float(page_duration) - float(candidate.duration_seconds))
        if delta <= max(3.0, float(page_duration) * 0.05):
            score += 3.0
            evidence.append("Duration agrees with page metadata")
        elif delta > float(page_duration) * 0.5:
            score -= 1.5
            evidence.append("Duration disagrees with page metadata")
    if candidate.height:
        if candidate.height >= 480:
            score += 1.5
            evidence.append(f"Height {candidate.height}px")
        else:
            score -= 0.5
            evidence.append(f"Low height {candidate.height}px")
    if candidate.filesize_bytes is not None:
        if candidate.filesize_bytes >= 5 * 1024 * 1024:
            score += 1.0
            evidence.append("Substantial payload size")
        elif candidate.filesize_bytes < 512 * 1024:
            score -= 1.5
            evidence.append("Very small payload size")
    if "currentsrc" in str(candidate.discovery_method or "").lower():
        score += 2.0
        evidence.append("Bound to an active player element")
    if "network" in str(candidate.discovery_method or "").lower():
        score += 0.5
        evidence.append("Observed as a browser network response")
    if segment_activity >= 3:
        score += 1.5
        evidence.append(f"{segment_activity} segment requests observed for this source")
    if looks_like_advertising(candidate.final_url or candidate.discovered_url):
        score -= 4.0
        evidence.append("URL matches advertising or tracking patterns")
    if candidate.frame_depth:
        evidence.append(f"Discovered in nested frame depth {candidate.frame_depth}")
    if candidate.is_live:
        evidence.append("Live source")
    candidate.confidence_score = round(score, 3)
    candidate.confidence_evidence = evidence
    return candidate


FEATURE_LENGTH_SECONDS = 120.0
CORROBORATION_EVIDENCE = ("duration agrees with page metadata", "bound to an active player element")


def corroborated_main_media(candidate: DetectedMediaCandidate) -> bool:
    evidence = {str(item).strip().casefold() for item in (candidate.confidence_evidence or [])}
    if candidate.verification_state == "probe_failed" or "duration disagrees with page metadata" in evidence:
        return False
    if any(token in evidence for token in CORROBORATION_EVIDENCE):
        return True
    if candidate.duration_seconds is not None and float(candidate.duration_seconds) >= FEATURE_LENGTH_SECONDS:
        return True
    return False


FAMILY_QUERY_KEYS = {"entryid", "entry_id", "assetid", "asset_id", "mediaid", "media_id", "videoid", "video_id"}
FAMILY_PATH = re.compile(r"/(?:entryid|entry_id|assetid|asset_id|mediaid|media_id|videoid|video_id)/([^/?]+)", re.IGNORECASE)


def media_family_key(candidate: DetectedMediaCandidate) -> str:
    target = candidate.final_url or candidate.discovered_url
    try:
        parsed = urlparse(str(target or ""))
    except Exception:
        return ""
    for name, value in parse_qsl(parsed.query, keep_blank_values=True):
        if name.casefold() in FAMILY_QUERY_KEYS and value:
            return f"{parsed.netloc.casefold()}:{name.casefold()}:{value.casefold()}"
    match = FAMILY_PATH.search(parsed.path)
    if match:
        return f"{parsed.netloc.casefold()}:path:{match.group(1).casefold()}"
    return ""


def group_media_families(candidates: list[DetectedMediaCandidate]) -> list[DetectedMediaCandidate]:
    result: list[DetectedMediaCandidate] = []
    families: dict[str, DetectedMediaCandidate] = {}
    for candidate in candidates:
        candidate.grouped_candidate_count = 1
        key = media_family_key(candidate) if candidate.is_manifest else ""
        if not key:
            result.append(candidate)
            continue
        representative = families.get(key)
        if representative is None:
            families[key] = candidate
            result.append(candidate)
            continue
        representative.grouped_candidate_count += 1
        representative.variant_count = max(representative.variant_count, candidate.variant_count)
        if not representative.variants and candidate.variants:
            representative.variants = list(candidate.variants)
        representative.confidence_evidence = [
            *representative.confidence_evidence,
            f"Grouped {representative.grouped_candidate_count} related manifest candidates into one media family",
        ]
    return result


def rank_candidates(candidates: list[DetectedMediaCandidate], page_duration: float | None = None, collection: CandidateCollection | None = None) -> list[DetectedMediaCandidate]:
    for candidate in candidates:
        activity = collection.segment_activity(candidate) if collection is not None else 0
        score_candidate(candidate, page_duration, activity)
        candidate.likely_main_media = False
    ordered = group_media_families(sorted(candidates, key=lambda item: (-item.confidence_score, item.discovered_at, item.final_url or item.discovered_url)))
    if ordered and ordered[0].confidence_score > 0:
        leader = ordered[0]
        runner_up = ordered[1].confidence_score if len(ordered) > 1 else None
        if (runner_up is None or leader.confidence_score > runner_up) and corroborated_main_media(leader):
            leader.likely_main_media = True
            leader.confidence_evidence = [*leader.confidence_evidence, "Highest factual evidence score among detected candidates"]
    return ordered


PROBE_TIMEOUT_SECONDS = 20.0
PROBE_USER_AGENT = "Mozilla/5.0 (compatible; VideoHaul/0.1.0)"


def candidate_request_headers(candidate: DetectedMediaCandidate) -> dict[str, str]:
    headers: dict[str, str] = {}
    for key, value in (candidate.required_headers or {}).items():
        name = str(key or "").strip()
        if name and str(value or "").strip():
            headers[name] = str(value)
    if candidate.referer and not any(key.lower() == "referer" for key in headers):
        headers["Referer"] = candidate.referer
    if candidate.user_agent and not any(key.lower() == "user-agent" for key in headers):
        headers["User-Agent"] = candidate.user_agent
    if not any(key.lower() == "user-agent" for key in headers):
        headers["User-Agent"] = PROBE_USER_AGENT
    return headers


def require_public_probe_url(url: str) -> str:
    return require_public_http_url(url)


class PublicProbeRedirectHandler(ScopedRedirectHandler):
    def __init__(self):
        super().__init__(require_public=True)


def http_probe(url: str, headers: dict[str, str] | None = None, timeout: float = PROBE_TIMEOUT_SECONDS, opener=None) -> dict:
    target = require_public_probe_url(url) if opener is None else str(url or "")
    request = urllib.request.Request(target, method="GET")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    request.add_header("Range", "bytes=0-0")
    if opener is None:
        guarded = build_scoped_opener(target, target, require_public=True)
        open_url = guarded.open
    else:
        open_url = opener
    with open_url(request, timeout=timeout) as response:
        info = response.headers
        body = response.read(SNIFF_BYTES)
        status = int(getattr(response, "status", 0) or 0)
        final_url = str(response.geturl() or url)
        if opener is None:
            require_public_probe_url(final_url)
    content_range = str(info.get("Content-Range") or "")
    total = None
    if "/" in content_range:
        tail = content_range.rsplit("/", 1)[-1].strip()
        if tail.isdigit():
            total = int(tail)
    if total is None:
        raw_length = info.get("Content-Length")
        try:
            total = int(raw_length) if raw_length and status != 206 else None
        except Exception:
            total = None
    return {
        "status": status,
        "final_url": final_url,
        "mime_type": normalized_mime(info.get("Content-Type") or ""),
        "content_disposition": str(info.get("Content-Disposition") or ""),
        "total_bytes": total,
        "accept_ranges": str(info.get("Accept-Ranges") or "").lower() == "bytes",
        "body_prefix": body,
    }


def verify_candidate(
    candidate: DetectedMediaCandidate,
    opener=None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> DetectedMediaCandidate:
    headers = candidate_request_headers(candidate)
    target = candidate.final_url or candidate.discovered_url
    notes: list[str] = []
    try:
        probe = http_probe(target, headers, timeout, opener)
    except urllib.error.HTTPError as exc:
        candidate.verification_state = "probe_failed"
        candidate.verification_detail = f"HTTP {exc.code}"
        candidate.status_code = int(exc.code)
        return candidate
    except Exception as exc:
        candidate.verification_state = "probe_failed"
        candidate.verification_detail = str(exc) or "Candidate did not respond"
        return candidate
    candidate.status_code = probe["status"]
    if probe["final_url"] and probe["final_url"] != target:
        candidate.final_url = probe["final_url"]
        candidate.host = host_of(probe["final_url"])
        notes.append("Followed redirect to final URL")
    if probe["mime_type"]:
        candidate.mime_type = probe["mime_type"]
        resolved = classify_media(candidate.final_url, probe["mime_type"])
        if resolved != "unknown":
            candidate.media_kind = resolved
            candidate.is_manifest = is_manifest_kind(resolved)
    disposition = str(probe.get("content_disposition") or "")
    if disposition:
        candidate.content_disposition = disposition
        suggested = filename_from_disposition(disposition)
        if suggested:
            candidate.suggested_filename = suggested
            if not candidate.title:
                candidate.title = suggested
    if candidate.media_kind == "unknown" or is_generic_mime(candidate.mime_type):
        sniffed = sniff_media_body(probe.get("body_prefix") or b"")
        if sniffed:
            candidate.sniffed_type = sniffed
            notes.append(f"Server reported {candidate.mime_type or 'no content type'} but the body is {sniffed}")
            resolved = kind_for_sniffed(sniffed)
            if resolved != "unknown":
                candidate.media_kind = resolved
                candidate.is_manifest = True
    if probe["total_bytes"] is not None:
        candidate.filesize_bytes = probe["total_bytes"]
        candidate.filesize_is_estimate = False
        notes.append("Exact size reported by the server")
    if probe["accept_ranges"]:
        notes.append("Server supports ranged resume")
    candidate.verification_state = "http_verified"
    candidate.verification_detail = "; ".join(notes) or "Reachable media response"
    return candidate


def _positive_float(value) -> float | None:
    try:
        number = float(value)
    except Exception:
        return None
    return number if number > 0 else None


def _positive_int(value) -> int | None:
    try:
        number = int(float(value))
    except Exception:
        return None
    return number if number > 0 else None


def _frame_rate(value) -> float | None:
    text = str(value or "").strip()
    if not text or text in {"0/0", "0"}:
        return None
    if "/" in text:
        numerator, _, denominator = text.partition("/")
        try:
            top = float(numerator)
            bottom = float(denominator)
        except Exception:
            return None
        return round(top / bottom, 3) if bottom else None
    return _positive_float(text)
