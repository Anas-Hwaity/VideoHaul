from __future__ import annotations

import json
from pathlib import Path
import subprocess


class ValidationError(RuntimeError):
    pass


def probe_media(path: str | Path, ffprobe: str | Path, timeout: float = 30.0, runner=subprocess.run) -> dict:
    target = Path(path)
    if not target.is_file() or target.stat().st_size <= 0:
        raise ValidationError("Output file is missing or empty")
    result = runner(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-of",
            "json",
            str(target),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = str(result.stderr or result.stdout or "").strip()
        raise ValidationError(detail or "FFprobe rejected the output")
    try:
        payload = json.loads(result.stdout or "{}")
    except Exception as exc:
        raise ValidationError("FFprobe returned invalid metadata") from exc
    streams = list(payload.get("streams") or [])
    if not streams:
        raise ValidationError("FFprobe found no media streams")
    return payload


def validate_output(path: str | Path, ffprobe: str | Path, require_video: bool, require_audio: bool, require_subtitles: bool = False, runner=subprocess.run) -> str:
    payload = probe_media(path, ffprobe, runner=runner)
    kinds = {str(item.get("codec_type") or "") for item in payload.get("streams") or []}
    if require_video and "video" not in kinds:
        raise ValidationError("Validated output has no video stream")
    if require_audio and "audio" not in kinds:
        raise ValidationError("Validated output has no audio stream")
    if require_subtitles and "subtitle" not in kinds:
        raise ValidationError("Validated output has no embedded subtitle stream")
    return str(Path(path))
