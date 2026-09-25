from __future__ import annotations

import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from ..http_security import build_scoped_opener, require_public_http_url, scope_headers

ARTWORK_USER_AGENT = "Mozilla/5.0 (compatible; VideoHaul/0.1.0)"
MAX_ARTWORK_BYTES = 12 * 1024 * 1024
IS_WINDOWS = os.name == "nt"

ATTACHED_PICTURE_CONTAINERS = {".mp4", ".m4v", ".mov", ".m4a", ".m4b"}
ID3_CONTAINERS = {".mp3"}
ATTACHMENT_CONTAINERS = {".mkv", ".mka"}
UNSUPPORTED_CONTAINERS = {".webm", ".ogg", ".oga", ".opus", ".ts", ".wav", ".flv", ".avi"}

IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", "jpeg", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", "png", ".png"),
    (b"GIF87a", "gif", ".gif"),
    (b"GIF89a", "gif", ".gif"),
    (b"BM", "bmp", ".bmp"),
)


class ThumbnailResult:
    def __init__(self, state: str, detail: str = "", output_path: str = "", source_url: str = ""):
        self.state = str(state)
        self.detail = str(detail or "")
        self.output_path = str(output_path or "")
        self.source_url = str(source_url or "")

    @property
    def embedded(self) -> bool:
        return self.state == "embedded"

    def to_dict(self) -> dict:
        return {"state": self.state, "detail": self.detail, "output_path": self.output_path, "source_url": self.source_url}

    def __repr__(self) -> str:
        return f"ThumbnailResult({self.state!r}, {self.detail!r})"


def identify_image(payload: bytes) -> tuple[str, str] | None:
    for signature, name, suffix in IMAGE_SIGNATURES:
        if payload.startswith(signature):
            return name, suffix
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "webp", ".webp"
    return None


def container_support(path: str | Path) -> str:
    suffix = Path(str(path)).suffix.lower()
    if suffix in ATTACHED_PICTURE_CONTAINERS:
        return "attached_picture"
    if suffix in ID3_CONTAINERS:
        return "id3"
    if suffix in ATTACHMENT_CONTAINERS:
        return "attachment"
    if suffix in UNSUPPORTED_CONTAINERS:
        return "unsupported"
    return "unsupported"


def fetch_artwork(url: str, destination: Path, opener=None, timeout: float = 30.0, headers: dict | None = None) -> tuple[Path, str]:
    target = str(url or "").strip()
    if not target:
        raise ValueError("No source thumbnail URL is available")
    if not target.lower().startswith(("http://", "https://")):
        raise ValueError("Unsupported thumbnail URL scheme")
    guarded_target = require_public_http_url(target) if opener is None else target
    request = urllib.request.Request(guarded_target, method="GET")
    request.add_header("User-Agent", ARTWORK_USER_AGENT)
    for key, value in scope_headers(headers, guarded_target, target).items():
        request.add_header(str(key), str(value))
    open_url = build_scoped_opener(guarded_target, target, True).open if opener is None else opener
    with open_url(request, timeout=timeout) as response:
        if opener is None:
            require_public_http_url(str(response.geturl() or guarded_target))
        payload = response.read(MAX_ARTWORK_BYTES + 1)
    if len(payload) > MAX_ARTWORK_BYTES:
        raise ValueError("Source thumbnail exceeded the safe artwork size limit")
    if not payload:
        raise ValueError("Source thumbnail response was empty")
    identified = identify_image(payload)
    if identified is None:
        raise ValueError("Source thumbnail is not a valid image")
    name, suffix = identified
    destination.parent.mkdir(parents=True, exist_ok=True)
    artwork = destination.with_suffix(suffix)
    artwork.write_bytes(payload)
    return artwork, name


def _run(command: list[str], runner=subprocess.run, timeout: float = 300.0) -> subprocess.CompletedProcess:
    flags = 0x08000000 if IS_WINDOWS else 0
    return runner(command, capture_output=True, timeout=timeout, creationflags=flags)


def _embed_command(ffmpeg: str, media: Path, artwork: Path, output: Path, mode: str, image_codec: str) -> list[str]:
    codec = "mjpeg" if image_codec in {"jpeg", "jpg"} else "png"
    if mode == "attachment":
        mime = "image/jpeg" if codec == "mjpeg" else "image/png"
        return [
            str(ffmpeg), "-v", "error", "-y",
            "-i", str(media),
            "-map", "0", "-c", "copy",
            "-attach", str(artwork),
            "-metadata:s:t:0", f"mimetype={mime}",
            "-metadata:s:t:0", "filename=cover" + artwork.suffix,
            str(output),
        ]
    if mode == "id3":
        return [
            str(ffmpeg), "-v", "error", "-y",
            "-i", str(media), "-i", str(artwork),
            "-map", "0:a", "-map", "1:v",
            "-c", "copy", "-id3v2_version", "3",
            "-metadata:s:v", "title=Album cover",
            "-metadata:s:v", "comment=Cover (front)",
            str(output),
        ]
    artwork_video_index = 0 if media.suffix.lower() in {".m4a", ".m4b"} else 1
    return [
        str(ffmpeg), "-v", "error", "-y",
        "-i", str(media), "-i", str(artwork),
        "-map", "0:v:0?", "-map", "0:a?", "-map", "0:s?", "-map", "0:d?", "-map", "1:v:0",
        "-c", "copy", f"-c:v:{artwork_video_index}", "mjpeg",
        f"-disposition:v:{artwork_video_index}", "attached_pic",
        f"-metadata:s:v:{artwork_video_index}", "title=VideoHaul source thumbnail",
        f"-metadata:s:v:{artwork_video_index}", "comment=Cover (front)",
        str(output),
    ]


def embed_source_thumbnail(
    media_path: str | Path,
    thumbnail_url: str,
    ffmpeg: str | Path,
    runner=subprocess.run,
    opener=None,
    refresher=None,
    headers: dict | None = None,
) -> ThumbnailResult:
    media = Path(str(media_path))
    if not media.is_file() or media.stat().st_size <= 0:
        return ThumbnailResult("failed", "Completed media file was not available for artwork embedding", str(media), str(thumbnail_url or ""))
    mode = container_support(media)
    if mode == "unsupported":
        return ThumbnailResult("unsupported_container", f"{media.suffix.lower() or 'This container'} cannot carry cover artwork", str(media), str(thumbnail_url or ""))
    url = str(thumbnail_url or "").strip()
    if not url:
        return ThumbnailResult("no_source_thumbnail", "The source did not expose a public thumbnail or poster", str(media), "")
    workspace = media.parent / f".videohaul-artwork-{media.stem[:40]}"
    artwork = None
    try:
        try:
            artwork, image_codec = fetch_artwork(url, workspace, opener, headers=headers)
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as first_error:
            refreshed = ""
            if refresher is not None:
                try:
                    refreshed = str(refresher() or "")
                except Exception:
                    refreshed = ""
            if not refreshed or refreshed == url:
                return ThumbnailResult("artwork_unavailable", f"Source thumbnail could not be retrieved: {first_error}", str(media), url)
            url = refreshed
            artwork, image_codec = fetch_artwork(url, workspace, opener, headers=headers)
        output = media.with_name(f".videohaul-cover-{media.name}")
        command = _embed_command(str(ffmpeg), media, artwork, output, mode, image_codec)
        result = _run(command, runner)
        if int(result.returncode or 0) != 0 or not output.is_file() or output.stat().st_size <= 0:
            output.unlink(missing_ok=True)
            detail = _tail(result.stderr) or "FFmpeg rejected the artwork embedding request"
            return ThumbnailResult("embed_failed", detail, str(media), url)
        os.replace(output, media)
        return ThumbnailResult("embedded", "Source thumbnail embedded into the downloaded media", str(media), url)
    except Exception as exc:
        return ThumbnailResult("embed_failed", str(exc) or "Artwork embedding failed", str(media), url)
    finally:
        if artwork is not None:
            try:
                Path(artwork).unlink(missing_ok=True)
            except Exception:
                pass


def _tail(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        from ..unicode_text import decode_external_bytes

        text = decode_external_bytes(value)
    else:
        text = str(value or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""
