from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from urllib.parse import parse_qs, urlparse

from ..formats import resolution_family
from ..models import AudioStream, MediaAnalysis, SubtitleTrack, VideoStream
from .common import AuthenticationRequired, MediaUnavailable, ResolverUnsupported, SourceRefusedRequest, authentication_from_text, finalize_analysis
from ..dependencies import js_runtime_args


class YtDlpResolver:
    name = "yt_dlp"

    def __init__(self, executable: str, ffmpeg_location: str, deno_path: str):
        self.executable = executable
        self.ffmpeg_location = ffmpeg_location
        self.deno_path = deno_path

    def supports(self, url: str) -> bool:
        return urlparse(url).scheme.lower() in {"http", "https"}

    def analyze(self, url: str, browser_cookies: str = "none", cancel_event=None) -> MediaAnalysis:
        args = [
            self.executable,
            "--ignore-config",
            "--dump-single-json",
            "--no-warnings",
            "--encoding",
            "utf-8",
        ]
        if _looks_like_playlist(url):
            args.append("--flat-playlist")
            if _is_youtube_radio_mix(url):
                args.extend(["--playlist-end", "10"])
        else:
            args.append("--no-playlist")
        args.extend(["--ffmpeg-location", self.ffmpeg_location, *js_runtime_args(self.deno_path), "--", url])
        if browser_cookies and browser_cookies.lower() != "none":
            args[1:1] = ["--cookies-from-browser", browser_cookies]
        try:
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8:strict"
            creationflags = (0x08000000 | 0x00000200) if os.name == "nt" else 0
            process = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="backslashreplace",
                env=env,
                creationflags=creationflags,
                start_new_session=os.name != "nt",
            )
            stdout, stderr = _communicate_analysis(process, cancel_event, 120.0)
        except FileNotFoundError as exc:
            from .common import ResolverDependencyError
            raise ResolverDependencyError("yt-dlp is unavailable") from exc
        if process.returncode != 0:
            error = (stderr or stdout or "Unable to analyze media").strip()
            lower = error.lower()
            if authentication_from_text(error):
                raise AuthenticationRequired("Authentication required")
            if "http error 403" in lower or "forbidden" in lower:
                raise SourceRefusedRequest("The source refused this request without a browser session")
            if any(value in lower for value in ("unsupported url", "no suitable extractor")):
                raise ResolverUnsupported("yt-dlp does not support this URL")
            raise MediaUnavailable("Media is unavailable")
        try:
            payload = json.loads(stdout)
        except Exception as exc:
            raise MediaUnavailable("Extractor returned invalid metadata") from exc
        return finalize_analysis(self._normalize(payload, url))

    def _normalize(self, payload: dict, url: str) -> MediaAnalysis:
        if str(payload.get("_type") or "").lower() in {"playlist", "multi_video"}:
            return self._normalize_playlist(payload, url)
        duration = _float(payload.get("duration"))
        original_language = str(payload.get("language") or "")
        videos: list[VideoStream] = []
        audios: list[AudioStream] = []
        for item in payload.get("formats") or []:
            format_id = str(item.get("format_id") or "")
            vcodec = str(item.get("vcodec") or "none")
            acodec = str(item.get("acodec") or "none")
            if not format_id or (vcodec == "none" and acodec == "none"):
                continue
            item_duration = _float(item.get("duration"))
            if item_duration is None:
                item_duration = duration
            size, estimated = _size(item)
            if vcodec != "none":
                videos.append(
                    VideoStream(
                        stream_id=format_id,
                        resolution_family=resolution_family(_int(item.get("height"))),
                        width=_int(item.get("width")),
                        height=_int(item.get("height")),
                        fps=_float(item.get("fps")),
                        bitrate=_float(item.get("tbr")),
                        codec=vcodec,
                        container=str(item.get("ext") or ""),
                        hdr_mode=_hdr(item),
                        duration_seconds=item_duration,
                        filesize_bytes=size,
                        filesize_is_estimate=estimated,
                        has_audio=acodec != "none",
                        audio_reference=None,
                        direct_stream_url=str(item.get("url") or "") or None,
                        source_metadata={
                            "format_note": item.get("format_note"),
                            "dynamic_range": item.get("dynamic_range"),
                            "protocol": item.get("protocol"),
                            "language": item.get("language"),
                            "resolver": self.name,
                        },
                    )
                )
            elif acodec != "none":
                language = str(item.get("language") or "")
                preference = _int(item.get("language_preference"))
                audios.append(
                    AudioStream(
                        stream_id=format_id,
                        language=language or None,
                        label=str(item.get("format_note") or item.get("format") or "") or None,
                        bitrate=_float(item.get("abr") or item.get("tbr")),
                        codec=acodec,
                        container=str(item.get("ext") or ""),
                        channels=_int(item.get("audio_channels")),
                        sample_rate=_int(item.get("asr")),
                        duration_seconds=item_duration,
                        filesize_bytes=size,
                        filesize_is_estimate=estimated,
                        direct_stream_url=str(item.get("url") or "") or None,
                        is_original=bool(original_language and language.casefold() == original_language.casefold()),
                        is_default=bool(preference is not None and preference > -1),
                    )
                )
        subtitles = _subtitles(payload)
        original_url = str(payload.get("webpage_url") or payload.get("original_url") or url)
        playlist = _playlist_reference(payload)
        metadata = {
            "description": payload.get("description") or "",
            "extractor": payload.get("extractor") or "",
            "extractor_key": payload.get("extractor_key") or "",
            "availability": payload.get("availability"),
            "live_status": payload.get("live_status"),
            "categories": list(payload.get("categories") or []),
            "tags": list(payload.get("tags") or []),
            "resolver": self.name,
        }
        return MediaAnalysis(
            source_url=url,
            canonical_url=original_url,
            platform=str(payload.get("extractor_key") or payload.get("extractor") or urlparse(url).netloc),
            platform_media_id=str(payload.get("id") or ""),
            title=str(payload.get("title") or "Untitled media"),
            creator=str(payload.get("uploader") or payload.get("creator") or ""),
            channel=str(payload.get("channel") or ""),
            upload_date=str(payload.get("upload_date") or ""),
            thumbnail_url=str(payload.get("thumbnail") or ""),
            duration_seconds=duration,
            is_live=bool(payload.get("is_live")),
            playlist=playlist,
            video_streams=videos,
            audio_streams=audios,
            subtitles=subtitles,
            chapters=list(payload.get("chapters") or []),
            metadata=metadata,
            auth_state="authenticated" if payload.get("availability") in {"subscriber_only", "premium_only"} else "none",
            analyzed_at=time.time(),
        )

    def _normalize_playlist(self, payload: dict, url: str) -> MediaAnalysis:
        entries = []
        for index, entry in enumerate(payload.get("entries") or [], start=1):
            if not isinstance(entry, dict):
                continue
            entries.append(
                {
                    "index": _int(entry.get("playlist_index")) or index,
                    "id": str(entry.get("id") or ""),
                    "title": str(entry.get("title") or "Untitled media"),
                    "url": str(entry.get("webpage_url") or entry.get("url") or ""),
                    "duration_seconds": _float(entry.get("duration")),
                    "thumbnail_url": str(entry.get("thumbnail") or ""),
                }
            )
        canonical = str(payload.get("webpage_url") or payload.get("original_url") or url)
        return MediaAnalysis(
            source_url=url,
            canonical_url=canonical,
            platform=str(payload.get("extractor_key") or payload.get("extractor") or urlparse(url).netloc),
            platform_media_id=str(payload.get("id") or ""),
            title=str(payload.get("title") or "Playlist"),
            creator=str(payload.get("uploader") or payload.get("creator") or ""),
            channel=str(payload.get("channel") or ""),
            thumbnail_url=str(payload.get("thumbnail") or ""),
            playlist={"id": str(payload.get("id") or ""), "title": str(payload.get("title") or "Playlist"), "count": len(entries), "entries": entries},
            metadata={"resolver": self.name, "extractor": payload.get("extractor") or "", "extractor_key": payload.get("extractor_key") or ""},
            analyzed_at=time.time(),
        )



def _terminate_analysis_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, creationflags=0x08000000)
        except Exception:
            try:
                process.kill()
            except Exception:
                return
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except Exception:
            try:
                process.terminate()
            except Exception:
                return
    try:
        process.wait(timeout=3)
    except Exception:
        try:
            if os.name != "nt":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except Exception:
            return


def _communicate_analysis(process: subprocess.Popen, cancel_event, timeout: float) -> tuple[str, str]:
    deadline = time.monotonic() + max(0.1, float(timeout))
    while True:
        if cancel_event is not None and cancel_event.is_set():
            _terminate_analysis_process(process)
            raise MediaUnavailable("Media analysis cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_analysis_process(process)
            raise MediaUnavailable("Media analysis timed out")
        try:
            return process.communicate(timeout=min(0.25, remaining))
        except subprocess.TimeoutExpired:
            continue

def _playlist_reference(payload: dict) -> dict | None:
    playlist_id = payload.get("playlist_id")
    playlist_title = payload.get("playlist_title")
    index = _int(payload.get("playlist_index"))
    count = _int(payload.get("n_entries"))
    if not any(value is not None and value != "" for value in (playlist_id, playlist_title, index, count)):
        return None
    return {"id": str(playlist_id or ""), "title": str(playlist_title or ""), "index": index, "count": count}


def _int(value):
    try:
        return int(value)
    except Exception:
        return None


def _float(value):
    try:
        return float(value)
    except Exception:
        return None


def _size(item: dict) -> tuple[int | None, bool]:
    exact = _int(item.get("filesize"))
    if exact is not None:
        return exact, False
    approximate = _int(item.get("filesize_approx"))
    if approximate is not None:
        return approximate, True
    return None, False


def _hdr(item: dict) -> str | None:
    value = str(item.get("dynamic_range") or "").strip()
    if not value or value.upper() in {"SDR", "NONE"}:
        return "SDR" if value.upper() == "SDR" else None
    return value


def _subtitles(payload: dict) -> list[SubtitleTrack]:
    result: list[SubtitleTrack] = []
    requested = payload.get("requested_subtitles") or {}
    human = payload.get("subtitles") or {}
    automatic = payload.get("automatic_captions") or {}
    original_language = str(payload.get("language") or "")
    for language, items in human.items():
        formats = sorted({str(item.get("ext") or "") for item in items if item.get("ext")})
        language_name = next((str(item.get("name") or "").strip() for item in items if str(item.get("name") or "").strip()), str(language))
        label = next((str(item.get("format_note") or "").strip() for item in items if str(item.get("format_note") or "").strip()), "")
        result.append(SubtitleTrack(track_id=f"human:{language}", language_code=str(language), language_name=language_name, label=label, is_original=bool(original_language and str(language).casefold() == original_language.casefold()), is_default=str(language) in requested, is_human=True, is_automatic=False, formats=formats, source="human"))
    for language, items in automatic.items():
        formats = sorted({str(item.get("ext") or "") for item in items if item.get("ext")})
        language_name = next((str(item.get("name") or "").strip() for item in items if str(item.get("name") or "").strip()), str(language))
        label = next((str(item.get("format_note") or "").strip() for item in items if str(item.get("format_note") or "").strip()), "")
        result.append(SubtitleTrack(track_id=f"auto:{language}", language_code=str(language), language_name=language_name, label=label, is_original=False, is_default=False, is_human=False, is_automatic=True, formats=formats, source="automatic"))
    return result



def _is_youtube_radio_mix(url: str) -> bool:
    parsed = urlparse(url)
    host = parsed.netloc.casefold()
    query = parse_qs(parsed.query)
    playlist = str((query.get("list") or [""])[0]).casefold()
    return ("youtube.com" in host or "youtu.be" in host) and (playlist.startswith("rd") or str((query.get("start_radio") or [""])[0]) == "1")

def _looks_like_playlist(url: str) -> bool:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    return bool(query.get("list")) or parsed.path.rstrip("/").lower().endswith("/playlist")
