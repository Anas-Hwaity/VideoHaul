from __future__ import annotations

from html.parser import HTMLParser
import json
import time
import urllib.error
import urllib.request
from urllib.parse import urljoin, urlparse

from ..models import AudioStream, MediaAnalysis, VideoStream
from ..unicode_text import decode_external_bytes
from .common import AuthenticationRequired, MediaUnavailable, ResolverUnsupported, classify_http_status, finalize_analysis


class GenericHtmlResolver:
    name = "generic_html"

    def supports(self, url: str) -> bool:
        return urlparse(url).scheme.lower() in {"http", "https"}

    def analyze(self, url: str, browser_cookies: str = "none", html: str | None = None) -> MediaAnalysis:
        final_url = url
        content = html
        if content is None:
            content, final_url = _fetch_html(url)
        parser = _MediaParser(final_url)
        parser.feed(content)
        candidates = parser.media
        if not candidates:
            raise ResolverUnsupported("No media was discoverable in page HTML")
        videos = []
        audios = []
        seen = set()
        for candidate in candidates:
            direct = candidate["url"]
            if direct in seen:
                continue
            seen.add(direct)
            kind = candidate["kind"]
            stream_id = f"html:{len(seen)}"
            if kind == "audio":
                audios.append(AudioStream(stream_id=stream_id, label=candidate.get("label"), container=_container(direct), duration_seconds=None, direct_stream_url=direct))
            else:
                videos.append(VideoStream(stream_id=stream_id, resolution_family="unknown", container=_container(direct), duration_seconds=None, direct_stream_url=direct, source_metadata={"resolver": self.name, "source": candidate.get("source")}))
        title = parser.title or urlparse(final_url).netloc or "Media page"
        return finalize_analysis(MediaAnalysis(source_url=url, canonical_url=final_url, platform=urlparse(final_url).netloc, platform_media_id=final_url, title=title, thumbnail_url=parser.thumbnail or "", video_streams=videos, audio_streams=audios, metadata={"resolver": self.name, "embedded_frames": parser.frames}, analyzed_at=time.time()))


class _MediaParser(HTMLParser):
    def __init__(self, base_url: str):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title = ""
        self.thumbnail = ""
        self.media: list[dict] = []
        self.frames: list[str] = []
        self._title_depth = 0
        self._json_depth = 0
        self._json_parts = []

    def handle_starttag(self, tag: str, attrs):
        values = {str(key).lower(): value for key, value in attrs}
        lower = tag.lower()
        if lower == "title":
            self._title_depth += 1
        if lower == "script" and str(values.get("type") or "").lower() == "application/ld+json":
            self._json_depth += 1
            self._json_parts = []
        if lower in {"video", "audio", "source"}:
            src = values.get("src")
            if src:
                mime = str(values.get("type") or "").lower()
                kind = "audio" if lower == "audio" or mime.startswith("audio/") else "video"
                self.media.append({"url": urljoin(self.base_url, src), "kind": kind, "source": lower, "label": values.get("label")})
            poster = values.get("poster")
            if poster and not self.thumbnail:
                self.thumbnail = urljoin(self.base_url, poster)
        if lower == "iframe" and values.get("src"):
            self.frames.append(urljoin(self.base_url, values["src"]))
        if lower == "meta":
            key = str(values.get("property") or values.get("name") or "").lower()
            content = values.get("content")
            if not content:
                return
            if key in {"og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"}:
                self.media.append({"url": urljoin(self.base_url, content), "kind": "video", "source": key})
            if key in {"og:audio", "og:audio:url", "og:audio:secure_url"}:
                self.media.append({"url": urljoin(self.base_url, content), "kind": "audio", "source": key})
            if key in {"og:image", "twitter:image"} and not self.thumbnail:
                self.thumbnail = urljoin(self.base_url, content)
            if key in {"og:title", "twitter:title"} and not self.title:
                self.title = content.strip()

    def handle_endtag(self, tag: str):
        lower = tag.lower()
        if lower == "title" and self._title_depth:
            self._title_depth -= 1
        if lower == "script" and self._json_depth:
            self._json_depth -= 1
            self._consume_json("".join(self._json_parts))
            self._json_parts = []

    def handle_data(self, data: str):
        if self._title_depth:
            self.title += data.strip()
        if self._json_depth:
            self._json_parts.append(data)

    def _consume_json(self, text: str) -> None:
        try:
            value = json.loads(text)
        except Exception:
            return
        stack = list(value) if isinstance(value, list) else [value]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
                continue
            if not isinstance(item, dict):
                continue
            stack.extend(value for value in item.values() if isinstance(value, (dict, list)))
            for key in ("contentUrl", "embedUrl"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate:
                    self.media.append({"url": urljoin(self.base_url, candidate), "kind": "video", "source": f"jsonld:{key}"})
            thumbnail = item.get("thumbnailUrl")
            if isinstance(thumbnail, str) and thumbnail and not self.thumbnail:
                self.thumbnail = urljoin(self.base_url, thumbnail)
            name = item.get("name")
            if isinstance(name, str) and name and not self.title:
                self.title = name


def _fetch_html(url: str) -> tuple[str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "VideoHaul/0.1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if content_type and "html" not in content_type and "xhtml" not in content_type:
                raise ResolverUnsupported("URL is not an HTML page")
            raw = response.read(4 * 1024 * 1024)
            charset = response.headers.get_content_charset() if hasattr(response.headers, "get_content_charset") else None
            return decode_external_bytes(raw, charset or "utf-8"), response.geturl() or url
    except ResolverUnsupported:
        raise
    except urllib.error.HTTPError as exc:
        classified = classify_http_status(exc.code, getattr(exc, "headers", None))
        if classified is not None:
            raise classified from exc
        raise MediaUnavailable("Page is unavailable") from exc
    except Exception as exc:
        raise MediaUnavailable("Page is unavailable") from exc


def _container(url: str) -> str:
    path = urlparse(url).path.lower()
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1] if "." in name else "unknown"
