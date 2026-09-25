from __future__ import annotations

import threading

from ..dependencies import ensure_tools
from .browser_extract import BrowserExtractResolver
from .browser_media import BrowserMediaResolver
from .common import AuthenticationRequired, MediaUnavailable, MediaUnresolved, ResolverDependencyError, ResolverUnsupported, SourceRefusedRequest, finalize_analysis, validate_media_url
from .dash import DashResolver
from .direct_http import DirectHttpResolver
from .generic_html import GenericHtmlResolver
from .hls import HlsResolver
from .streamlink import StreamlinkResolver
from .ytdlp import YtDlpResolver


class ResolverManager:
    def __init__(self, resolvers: list | None = None):
        self._lock = threading.Lock()
        self._ytdlp = None
        self._custom = resolvers
        self._direct = DirectHttpResolver()
        self._hls = HlsResolver()
        self._dash = DashResolver()
        self._streamlink = StreamlinkResolver()
        self._generic = GenericHtmlResolver()
        self._browser = BrowserExtractResolver()
        self._browser_media = BrowserMediaResolver()

    def _get_ytdlp(self) -> YtDlpResolver:
        with self._lock:
            if self._ytdlp is None:
                tools = ensure_tools()
                self._ytdlp = YtDlpResolver(tools["yt_dlp"], tools["ffmpeg"], tools["deno"])
            return self._ytdlp

    def analyze(self, url: str, browser_cookies: str = "none", cancel_event=None):
        value = validate_media_url(url)
        candidates = self._custom if self._custom is not None else self._candidates(value)
        unavailable = None
        unresolved = None
        dependency = None
        refused = None
        for resolver in candidates:
            try:
                target = resolver() if callable(resolver) and not hasattr(resolver, "analyze") else resolver
                if cancel_event is not None and cancel_event.is_set():
                    raise MediaUnavailable("Media analysis cancelled")
                if hasattr(target, "supports") and not target.supports(value):
                    continue
                if isinstance(target, YtDlpResolver):
                    return finalize_analysis(target.analyze(value, browser_cookies, cancel_event=cancel_event))
                return finalize_analysis(target.analyze(value, browser_cookies))
            except AuthenticationRequired:
                raise
            except SourceRefusedRequest as exc:
                refused = refused or exc
                continue
            except ResolverUnsupported:
                continue
            except ResolverDependencyError as exc:
                dependency = exc
                continue
            except MediaUnresolved as exc:
                unresolved = exc
                continue
            except MediaUnavailable as exc:
                unavailable = exc
                continue
            except Exception as exc:
                unresolved = unresolved or MediaUnresolved(f"Could not resolve media: {exc}")
                continue
        if refused is not None:
            raise refused
        if unresolved is not None:
            raise unresolved
        if unavailable is not None:
            raise unavailable
        if dependency is not None:
            raise dependency
        raise ResolverUnsupported("No resolver could analyze this URL")

    def _candidates(self, url: str) -> list:
        specialized = []
        if self._hls.supports(url):
            specialized.append(self._hls)
        elif self._dash.supports(url):
            specialized.append(self._dash)
        elif self._direct.supports(url):
            specialized.append(self._direct)
        return [*specialized, self._get_ytdlp, self._streamlink, self._generic, self._browser, self._browser_media]
