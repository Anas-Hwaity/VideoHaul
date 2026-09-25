from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

VIDEOHAUL_COMMENT = "Downloaded by VideoHaul."
IS_WINDOWS = os.name == "nt"
STREAM_TAG_CONTAINERS = {".ogg", ".oga", ".opus", ".spx"}
UNTAGGABLE_CONTAINERS = {".aac", ".ac3", ".eac3", ".dts", ".ts", ".m2ts", ".mts", ".h264", ".h265", ".ivf", ".srt", ".vtt", ".ass"}


class MetadataStampError(RuntimeError):
    pass


class MetadataStampUnsupported(MetadataStampError):
    pass


def _run(command: list[str], runner=subprocess.run, timeout: float = 300.0) -> subprocess.CompletedProcess:
    flags = 0x08000000 if IS_WINDOWS else 0
    return runner(command, capture_output=True, timeout=timeout, creationflags=flags)


def _probe_comments(path: Path, ffprobe: str | Path, runner) -> tuple[int, list[str]]:
    probe = _run([
        str(ffprobe), "-v", "error", "-show_entries", "format_tags=comment:stream_tags=comment", "-of", "json", str(path),
    ], runner, timeout=30.0)
    try:
        payload = json.loads(_text(probe.stdout) or "{}")
    except Exception as exc:
        raise MetadataStampError("FFprobe returned invalid metadata while reading the comment tag") from exc
    values = []
    sources = [dict((payload.get("format") or {}).get("tags") or {})]
    sources.extend(dict(item.get("tags") or {}) for item in payload.get("streams") or [])
    for tags in sources:
        for key, value in tags.items():
            if str(key).casefold() == "comment" and str(value).strip():
                values.append(str(value))
    return int(probe.returncode or 0), values


def combined_comment(existing: str) -> str:
    text = str(existing or "").strip()
    if not text:
        return VIDEOHAUL_COMMENT
    if VIDEOHAUL_COMMENT in text:
        return text
    return f"{text}\n\n{VIDEOHAUL_COMMENT}"


def stamp_videohaul_comment(
    media_path: str | Path,
    ffmpeg: str | Path,
    ffprobe: str | Path,
    runner=subprocess.run,
) -> str:
    media = Path(str(media_path))
    if not media.is_file() or media.stat().st_size <= 0:
        raise MetadataStampError("Completed media file was not available for the comment tag")
    suffix = media.suffix.casefold()
    if suffix in UNTAGGABLE_CONTAINERS:
        raise MetadataStampUnsupported(f"The {suffix.lstrip('.').upper()} container cannot carry a comment tag")
    _, existing = _probe_comments(media, ffprobe, runner)
    if existing and all(VIDEOHAUL_COMMENT in value for value in existing):
        return str(media)
    comment = combined_comment(existing[0] if existing else "")
    output = media.with_name(f".videohaul-tag-{media.name}")
    metadata = ["-metadata:s:a", f"comment={comment}"] if suffix in STREAM_TAG_CONTAINERS else ["-metadata", f"comment={comment}"]
    command = [str(ffmpeg), "-v", "error", "-y", "-i", str(media), "-map", "0", "-c", "copy", *metadata, str(output)]
    result = _run(command, runner)
    if int(result.returncode or 0) != 0 or not output.is_file() or output.stat().st_size <= 0:
        output.unlink(missing_ok=True)
        raise MetadataStampError(_tail(result.stderr) or "FFmpeg could not write the comment tag")
    code, written = _probe_comments(output, ffprobe, runner)
    if code != 0 or not any(VIDEOHAUL_COMMENT in value for value in written):
        output.unlink(missing_ok=True)
        raise MetadataStampUnsupported("The output container did not keep the comment tag")
    os.replace(output, media)
    return str(media)


def _text(value) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="backslashreplace")
    return str(value or "")


def _tail(value) -> str:
    lines = [line.strip() for line in _text(value).splitlines() if line.strip()]
    return lines[-1] if lines else ""
