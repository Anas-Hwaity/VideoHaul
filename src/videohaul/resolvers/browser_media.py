from __future__ import annotations

import time
from dataclasses import replace
from urllib.parse import urlparse
from uuid import uuid4

from ..media_candidates import candidate_request_headers, corroborated_main_media, verify_candidate
from ..models import DetectedMediaCandidate, MediaAnalysis, VideoStream
from ..formats import resolution_family
from ..file_policy import delivered_container
from .common import (
    AuthenticationRequired,
    MediaUnavailable,
    MediaUnresolved,
    ResolverDependencyError,
    ResolverUnsupported,
    SourceRefusedRequest,
)
from .dash import DashResolver
from .direct_http import DirectHttpResolver
from .hls import HlsResolver


class BrowserMediaResolver:
    name = "browser_media"

    def __init__(self, detector=None, verifier=None, budget_seconds: float | None = None, observer=None):
        self._detector = detector
        self._verifier = verifier or verify_candidate
        self._budget_seconds = budget_seconds
        self._observer = observer
        self._direct = DirectHttpResolver()
        self._hls = HlsResolver()
        self._dash = DashResolver()

    def supports(self, url: str) -> bool:
        return urlparse(str(url or "")).scheme.lower() in {"http", "https"}

    def _detect(self, url: str) -> tuple[list[DetectedMediaCandidate], int | None]:
        if self._detector is not None:
            outcome = self._detector(url)
            if isinstance(outcome, tuple):
                candidates, status = outcome
                return list(candidates or []), status
            return list(outcome or []), None
        from ..media_detection import detect_media, playwright_available

        if not playwright_available():
            raise ResolverDependencyError("Managed Chromium automation runtime is unavailable")
        candidates, status = detect_media(url, budget_seconds=self._budget_seconds, observer=self._observer, probe=self._verifier)
        return list(candidates or []), status

    def analyze(self, url: str, browser_cookies: str = "none") -> MediaAnalysis:
        candidates, page_status = self._detect(url)
        if page_status is not None and int(page_status) == 401:
            raise AuthenticationRequired("Authentication required")
        if page_status is not None and int(page_status) == 403 and not candidates:
            raise SourceRefusedRequest("The source refused this request without a browser session")
        if not candidates:
            if page_status is not None and int(page_status) >= 400:
                raise MediaUnavailable("Unavailable")
            raise MediaUnresolved("Could not resolve media")
        usable = [item for item in candidates if item.verification_state in {"verified", "http_verified"} and not item.rejected]
        if not usable:
            detail = next((item.verification_detail for item in candidates if item.verification_detail), "")
            if any(str(item.status_code or 0) == "401" for item in candidates):
                raise AuthenticationRequired("Authentication required")
            if any(str(item.status_code or 0) == "403" for item in candidates):
                raise SourceRefusedRequest("The source refused these media requests without a browser session")
            raise MediaUnresolved(detail or "Could not resolve media")
        primary = self._select_primary(usable)
        if primary is None:
            raise MediaUnresolved("Could not confidently identify the main media on this page")
        analysis = self._analysis_for(primary, browser_cookies)
        analysis.source_url = str(url)
        analysis.canonical_url = analysis.canonical_url or str(url)
        analysis.metadata = dict(analysis.metadata or {})
        analysis.metadata["resolver"] = self.name
        analysis.metadata["detected_candidates"] = len(candidates)
        analysis.metadata["candidate_id"] = primary.candidate_id
        analysis.metadata["discovery_method"] = primary.discovery_method
        analysis.metadata["candidate_host"] = primary.host
        analysis.metadata["candidate_evidence"] = list(primary.confidence_evidence)
        analysis.metadata["direct_transfer"] = True
        analysis.metadata["_transfer_headers"] = dict(primary.required_headers or {})
        analysis.metadata["_transfer_origin"] = str(primary.final_url or primary.discovered_url or "")
        kind = manifest_kind(primary.container, primary.final_url or primary.discovered_url)
        for stream in analysis.video_streams:
            stream.source_metadata = dict(stream.source_metadata or {})
            stream.source_metadata["direct_transfer"] = True
            stream.source_metadata["manifest_kind"] = kind
            stream.source_metadata["verification_state"] = primary.verification_state
            stream.source_metadata["detector_guarded"] = True
        if primary.required_headers:
            analysis.metadata["request_context_required"] = True
        if primary.referer:
            analysis.metadata["referer"] = primary.referer
        if primary.user_agent:
            analysis.metadata["user_agent"] = primary.user_agent
        if not analysis.title:
            analysis.title = primary.title or primary.page_title or _title_from_url(primary.final_url or url)
        if not analysis.thumbnail_url and primary.poster_or_thumbnail:
            analysis.thumbnail_url = primary.poster_or_thumbnail
        if analysis.duration_seconds is None and primary.duration_seconds is not None:
            analysis.duration_seconds = primary.duration_seconds
        analysis.is_live = analysis.is_live or primary.is_live
        if not analysis.platform:
            analysis.platform = primary.host or "web"
        if analysis.expires_at is None and primary.expiry_if_known is not None:
            analysis.expires_at = primary.expiry_if_known
        return analysis

    def _select_primary(self, usable: list[DetectedMediaCandidate]) -> DetectedMediaCandidate | None:
        leader = next((item for item in usable if item.likely_main_media), None)
        if leader is None:
            return None
        if not corroborated_main_media(leader):
            return None
        return leader

    def _analysis_for(self, candidate: DetectedMediaCandidate, browser_cookies: str) -> MediaAnalysis:
        target = candidate.final_url or candidate.discovered_url
        chain = []
        if candidate.media_kind == "hls":
            chain = [self._hls]
        elif candidate.media_kind == "dash":
            chain = [self._dash]
        else:
            chain = [self._direct]
        for resolver in chain:
            try:
                if candidate.media_kind in {"hls", "dash"}:
                    return resolver.analyze(target, browser_cookies, candidate_request_headers(candidate))
                return resolver.analyze(target, browser_cookies)
            except (AuthenticationRequired, ResolverDependencyError):
                raise
            except (MediaUnavailable, ResolverUnsupported, Exception):
                continue
        return candidate_analysis(candidate)


def manifest_kind(container: str, url: str = "") -> str:
    text = str(container or "").strip().lower().lstrip(".")
    target = str(url or "").lower()
    if text in {"m3u8", "m3u", "hls"} or ".m3u8" in target:
        return "hls"
    if text in {"mpd", "dash"} or ".mpd" in target:
        return "dash"
    if text in {"ism", "f4m"}:
        return text
    return ""


def candidate_analysis(candidate: DetectedMediaCandidate) -> MediaAnalysis:
    if candidate.verification_state not in {"verified", "http_verified"}:
        raise MediaUnresolved("Detected media must pass public-network verification before it can be adopted")
    target = candidate.final_url or candidate.discovered_url
    if not target:
        raise MediaUnresolved("Could not resolve media")
    height = candidate.height
    stream = VideoStream(
        stream_id=f"candidate-{candidate.candidate_id[:12]}",
        resolution_family=resolution_family(height),
        width=candidate.width,
        height=height,
        fps=candidate.fps,
        bitrate=candidate.bitrate,
        codec=candidate.video_codec or None,
        container=delivered_container(candidate.container, False) or None,
        hdr_mode=candidate.hdr_mode or None,
        duration_seconds=candidate.duration_seconds,
        filesize_bytes=candidate.filesize_bytes,
        filesize_is_estimate=bool(candidate.filesize_is_estimate),
        has_audio=bool(candidate.audio_codec),
        direct_stream_url=target,
        source_metadata={
            "discovery_method": candidate.discovery_method,
            "verification_state": candidate.verification_state,
            "source_frame_url": candidate.source_frame_url,
            "direct_transfer": True,
            "manifest_kind": manifest_kind(candidate.container, target),
            "source_container": str(candidate.container or ""),
            "detector_guarded": True,
        },
    )
    analysis = MediaAnalysis(
        analysis_id=uuid4().hex,
        metadata={"resolver": "browser_media", "direct_transfer": True, "candidate_id": candidate.candidate_id, "referer": candidate.referer or "", "user_agent": candidate.user_agent or "", "_transfer_headers": candidate_request_headers(candidate), "_transfer_origin": target},
        source_url=candidate.page_url or target,
        canonical_url=candidate.page_url or target,
        platform=candidate.host or "web",
        platform_media_id="",
        title=candidate.title or candidate.page_title or _title_from_url(target),
        thumbnail_url=candidate.poster_or_thumbnail or "",
        duration_seconds=candidate.duration_seconds,
        is_live=bool(candidate.is_live),
        video_streams=[stream],
        analyzed_at=time.time(),
        expires_at=candidate.expiry_if_known,
    )
    return replace(analysis)


def _title_from_url(url: str) -> str:
    try:
        path = urlparse(str(url or "")).path
    except Exception:
        path = ""
    name = path.rsplit("/", 1)[-1]
    return name or "Detected media"
