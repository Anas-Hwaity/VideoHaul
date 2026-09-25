from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from ..formats import resolution_family
from ..models import AudioStream, MediaAnalysis, VideoStream
from .common import MediaUnavailable, ResolverUnsupported, finalize_analysis


VIDEO_EXTENSIONS = {".mp4", ".m4v", ".webm", ".mov", ".mkv", ".avi", ".ts"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac", ".wav"}


class DirectHttpResolver:
    name = "direct_http"

    def supports(self, url: str) -> bool:
        suffix = _suffix(url)
        guessed, _ = mimetypes.guess_type(urlparse(url).path)
        return suffix in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS or str(guessed or "").startswith(("video/", "audio/"))

    def analyze(self, url: str, browser_cookies: str = "none") -> MediaAnalysis:
        metadata = self._probe(url)
        content_type = metadata["content_type"]
        final_url = metadata["final_url"]
        suffix = _suffix(final_url)
        kind = "video" if content_type.startswith("video/") or suffix in VIDEO_EXTENSIONS else "audio" if content_type.startswith("audio/") or suffix in AUDIO_EXTENSIONS else ""
        if not kind:
            raise ResolverUnsupported("URL is not a recognized direct media file")
        size = metadata["size"]
        identifier = f"direct:{suffix.lstrip('.') or kind}"
        video_streams = []
        audio_streams = []
        facts = probe_facts(final_url)
        duration = facts.get("duration")
        if kind == "video":
            height = facts.get("height")
            video_streams.append(VideoStream(stream_id=identifier, resolution_family=resolution_family(height), width=facts.get("width"), height=height, fps=facts.get("fps"), codec=facts.get("vcodec"), has_audio=bool(facts.get("acodec")), container=suffix.lstrip(".") or _subtype(content_type), duration_seconds=duration, filesize_bytes=size, direct_stream_url=final_url, source_metadata={"content_type": content_type, "resolver": self.name}))
        else:
            audio_streams.append(AudioStream(stream_id=identifier, codec=facts.get("acodec"), container=suffix.lstrip(".") or _subtype(content_type), duration_seconds=duration, filesize_bytes=size, direct_stream_url=final_url))
        title = urlparse(final_url).path.rsplit("/", 1)[-1] or "Direct media"
        return finalize_analysis(MediaAnalysis(source_url=url, canonical_url=final_url, platform=urlparse(final_url).netloc, platform_media_id=final_url, title=title, video_streams=video_streams, audio_streams=audio_streams, metadata={"content_type": content_type, "resolver": self.name}, analyzed_at=time.time()))

    def _probe(self, url: str) -> dict:
        headers = {"User-Agent": "VideoHaul/0.1.0"}
        request = urllib.request.Request(url, headers=headers, method="HEAD")
        try:
            response = urllib.request.urlopen(request, timeout=20)
        except Exception:
            request = urllib.request.Request(url, headers={**headers, "Range": "bytes=0-0"})
            try:
                response = urllib.request.urlopen(request, timeout=20)
            except urllib.error.HTTPError as exc:
                from .common import classify_http_status
                classified = classify_http_status(exc.code, getattr(exc, "headers", None))
                if classified is not None:
                    raise classified from exc
                raise MediaUnavailable("Direct media is unavailable") from exc
            except Exception as exc:
                raise MediaUnavailable("Direct media is unavailable") from exc
        with response:
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            size = _integer(response.headers.get("Content-Length"))
            final_url = response.geturl() or url
        return {"content_type": content_type, "size": size, "final_url": final_url}


def probe_facts(url: str, runner=subprocess.run) -> dict:
    try:
        from ..dependencies import ensure_ffmpeg

        directory = Path(ensure_ffmpeg())
        executable = directory / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
        result = runner(
            [str(executable), "-v", "error", "-show_format", "-show_streams", "-of", "json", "-rw_timeout", "15000000", url],
            capture_output=True, text=True, encoding="utf-8", errors="backslashreplace", timeout=25,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        if int(result.returncode or 0) != 0:
            return {}
        payload = json.loads(result.stdout or "{}")
    except Exception:
        return {}
    facts: dict = {}
    for stream in payload.get("streams") or []:
        kind = stream.get("codec_type")
        if kind == "video" and "vcodec" not in facts and not (stream.get("disposition") or {}).get("attached_pic"):
            facts["vcodec"] = stream.get("codec_name") or None
            facts["width"] = _integer(stream.get("width"))
            facts["height"] = _integer(stream.get("height"))
            facts["fps"] = _rate(stream.get("avg_frame_rate") or stream.get("r_frame_rate"))
        elif kind == "audio" and "acodec" not in facts:
            facts["acodec"] = stream.get("codec_name") or None
    try:
        duration = float((payload.get("format") or {}).get("duration"))
        facts["duration"] = duration if duration > 0 else None
    except Exception:
        pass
    return facts


def _rate(value) -> float | None:
    try:
        numerator, _, denominator = str(value or "").partition("/")
        rate = float(numerator) / float(denominator or 1)
        return round(rate, 3) if rate > 0 else None
    except Exception:
        return None


def _integer(value) -> int | None:
    try:
        return int(value)
    except Exception:
        return None


def _suffix(url: str) -> str:
    path = urlparse(url).path.lower()
    dot = path.rfind(".")
    return path[dot:] if dot >= 0 else ""


def _subtype(content_type: str) -> str:
    return content_type.split("/", 1)[-1] if "/" in content_type else ""
