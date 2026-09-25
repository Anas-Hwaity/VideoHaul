from __future__ import annotations

import json
import re
import time

PROGRESS_PREFIX = "__VH_PROGRESS__"
FILE_PREFIX = "__VH_FILE__"
COMPONENT_PREFIX = "__VH_COMPONENT__"
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

PROGRESS_FIELDS = (
    "status",
    "downloaded_bytes",
    "total_bytes",
    "total_bytes_estimate",
    "speed",
    "eta",
    "elapsed",
    "fragment_index",
    "fragment_count",
    "format_id",
    "vcodec",
    "acodec",
    "tmpfilename",
)

PROGRESS_NAMESPACE = {
    "format_id": "info",
    "vcodec": "info",
    "acodec": "info",
}

PROGRESS_TEMPLATE = PROGRESS_PREFIX + "|".join(
    f"%({PROGRESS_NAMESPACE.get(name, 'progress')}.{name})s" for name in PROGRESS_FIELDS
)

UNKNOWN_TOKENS = {"", "na", "n/a", "none", "unknown", "nan", "-"}

SIZE_UNITS = {
    "b": 1,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "k": 1024,
    "m": 1024**2,
    "g": 1024**3,
}

DOWNLOAD_LINE = re.compile(
    r"\[download\]\s+(?P<percent>[\d.]+)%\s+of\s+(?P<estimate>~\s*)?(?P<total>[\d.]+\s*[a-zA-Z]+)"
    r"(?:\s+in\s+(?P<taken>[\d:]+))?"
    r"(?:\s+at\s+(?P<speed>[\d.]+\s*[a-zA-Z/]+|Unknown\s*B/s|Unknown))?"
    r"(?:\s+ETA\s+(?P<eta>[\d:]+|Unknown|NA))?"
    r"(?:\s*\(frag\s+(?P<frag>\d+)\s*/\s*(?P<fragtotal>\d+)\))?",
    re.IGNORECASE,
)

RESUME_LINE = re.compile(r"\[download\]\s+Resuming download at byte\s+(?P<offset>\d+)", re.IGNORECASE)
DESTINATION_LINE = re.compile(r"\[download\]\s+Destination:\s*(?P<path>.+)$", re.IGNORECASE)
MERGE_TARGET_LINE = re.compile(r"Merging formats into\s+\"(?P<path>.+)\"\s*$", re.IGNORECASE)
ALREADY_DOWNLOADED_LINE = re.compile(r"\[download\]\s+(?P<path>.+?)\s+has already been downloaded\s*$", re.IGNORECASE)


class LineFramer:
    def __init__(self):
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        if not chunk:
            return []
        self._buffer.extend(chunk)
        lines: list[bytes] = []
        start = 0
        for index, byte in enumerate(self._buffer):
            if byte in (10, 13):
                if index > start:
                    lines.append(bytes(self._buffer[start:index]))
                start = index + 1
        if start:
            del self._buffer[:start]
        return lines

    def flush(self) -> list[bytes]:
        if not self._buffer:
            return []
        remainder = bytes(self._buffer)
        self._buffer.clear()
        return [remainder] if remainder.strip() else []

    def pending(self) -> bytes:
        return bytes(self._buffer)


def clean_text(value: str) -> str:
    return ANSI_ESCAPE.sub("", str(value or "")).strip()


def marker_payload(text: str, prefix: str) -> str | None:
    clean = clean_text(text)
    index = clean.find(prefix)
    if index < 0:
        return None
    return clean[index + len(prefix) :].strip()


def _token(value) -> str:
    text = str(value if value is not None else "").strip()
    return "" if text.casefold() in UNKNOWN_TOKENS else text


def _number(value, cast):
    text = str(value if value is not None else "").strip()
    if text.casefold() in UNKNOWN_TOKENS:
        return None
    try:
        return cast(float(text))
    except Exception:
        return None


def parse_size(value: str) -> int | None:
    text = str(value or "").strip().replace("~", "").strip()
    if text.casefold() in UNKNOWN_TOKENS:
        return None
    match = re.fullmatch(r"(?P<amount>[\d.]+)\s*(?P<unit>[a-zA-Z]*)", text)
    if not match:
        return None
    try:
        amount = float(match.group("amount"))
    except Exception:
        return None
    unit = (match.group("unit") or "b").casefold().replace("/s", "")
    factor = SIZE_UNITS.get(unit)
    if factor is None:
        return None
    return int(amount * factor)


def parse_speed(value: str) -> float | None:
    text = str(value or "").strip()
    if text.casefold().replace("b/s", "").strip() in UNKNOWN_TOKENS:
        return None
    match = re.fullmatch(r"(?P<amount>[\d.]+)\s*(?P<unit>[a-zA-Z]*)(?:/s)?", text)
    if not match:
        return None
    try:
        amount = float(match.group("amount"))
    except Exception:
        return None
    unit = (match.group("unit") or "b").casefold()
    factor = SIZE_UNITS.get(unit)
    if factor is None:
        return None
    return float(amount * factor)


def parse_clock(value: str) -> float | None:
    text = str(value or "").strip()
    if text.casefold() in UNKNOWN_TOKENS:
        return None
    parts = text.split(":")
    if not all(part.isdigit() for part in parts if part != ""):
        return None
    try:
        values = [int(part) for part in parts]
    except Exception:
        return None
    seconds = 0.0
    for part in values:
        seconds = seconds * 60 + part
    return seconds


def parse_structured_progress(payload: str) -> dict | None:
    text = str(payload or "").strip()
    if not text:
        return None
    if text.startswith("{"):
        try:
            value = json.loads(text)
        except Exception:
            return None
        if not isinstance(value, dict):
            return None
        return _normalize_structured(value)
    parts = text.split("|", len(PROGRESS_FIELDS) - 1)
    if len(parts) < len(PROGRESS_FIELDS):
        parts.extend([""] * (len(PROGRESS_FIELDS) - len(parts)))
    value = dict(zip(PROGRESS_FIELDS, parts))
    return _normalize_structured(value)


def _normalize_structured(value: dict) -> dict | None:
    status = str(value.get("status") or "").strip().casefold()
    if status in UNKNOWN_TOKENS and not any(str(value.get(name) or "").strip().casefold() not in UNKNOWN_TOKENS for name in ("downloaded_bytes", "total_bytes", "speed")):
        return None
    downloaded = _number(value.get("downloaded_bytes"), int)
    total = _number(value.get("total_bytes"), int)
    if total is None:
        total = _number(value.get("total_bytes_estimate"), int)
        estimated = total is not None
    else:
        estimated = False
    filename = str(value.get("tmpfilename") or value.get("filename") or "").strip()
    if filename.casefold() in UNKNOWN_TOKENS:
        filename = ""
    return {
        "source": "structured",
        "status": status or "downloading",
        "format_id": _token(value.get("format_id")),
        "vcodec": _token(value.get("vcodec")),
        "acodec": _token(value.get("acodec")),
        "downloaded_bytes": downloaded,
        "total_bytes": total,
        "total_is_estimate": estimated,
        "speed_bps": _number(value.get("speed"), float),
        "eta_seconds": _number(value.get("eta"), float),
        "elapsed_seconds": _number(value.get("elapsed"), float),
        "fragment_index": _number(value.get("fragment_index"), int),
        "fragment_count": _number(value.get("fragment_count"), int),
        "filename": filename,
    }


def parse_download_line(text: str) -> dict | None:
    clean = clean_text(text)
    if "[download]" not in clean:
        return None
    match = DOWNLOAD_LINE.search(clean)
    if not match:
        return None
    total = parse_size(match.group("total"))
    percent = None
    try:
        percent = float(match.group("percent"))
    except Exception:
        percent = None
    downloaded = None
    if total is not None and percent is not None:
        downloaded = int(total * max(0.0, min(percent, 100.0)) / 100.0)
    eta = parse_clock(match.group("eta")) if match.group("eta") else None
    elapsed = parse_clock(match.group("taken")) if match.group("taken") else None
    return {
        "source": "download_line",
        "status": "finished" if percent is not None and percent >= 100.0 else "downloading",
        "percent": percent,
        "downloaded_bytes": downloaded,
        "total_bytes": total,
        "total_is_estimate": bool(match.group("estimate")),
        "speed_bps": parse_speed(match.group("speed")) if match.group("speed") else None,
        "eta_seconds": eta,
        "elapsed_seconds": elapsed,
        "fragment_index": _number(match.group("frag"), int),
        "fragment_count": _number(match.group("fragtotal"), int),
        "filename": "",
    }


def component_stage(sample: dict | None) -> str:
    if not sample:
        return ""
    vcodec = str(sample.get("vcodec") or "").strip().casefold()
    acodec = str(sample.get("acodec") or "").strip().casefold()
    if not vcodec and not acodec:
        return ""
    video = vcodec not in {"", "none"}
    audio = acodec not in {"", "none"}
    if audio and not video:
        return "downloading_audio"
    if video and not audio:
        return "downloading_video"
    return ""


def parse_destination(text: str) -> str:
    match = DESTINATION_LINE.search(clean_text(text))
    return match.group("path").strip() if match else ""


def parse_merge_target(text: str) -> str:
    clean = clean_text(text)
    match = MERGE_TARGET_LINE.search(clean)
    if match:
        return match.group("path").strip()
    match = ALREADY_DOWNLOADED_LINE.search(clean)
    return match.group("path").strip() if match else ""


def parse_resume_offset(text: str) -> int | None:
    match = RESUME_LINE.search(clean_text(text))
    return int(match.group("offset")) if match else None


class ProgressTracker:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.started_at = clock()
        self.last_update_at: float | None = None
        self.last_emitted: dict | None = None
        self.downloaded_bytes = 0
        self.total_bytes: int | None = None
        self.total_is_estimate = False
        self.speed_bps: float | None = None
        self.eta_seconds: float | None = None
        self.elapsed_seconds = 0.0
        self.filename = ""
        self.resume_offset = 0
        self.sources: set[str] = set()
        self.updates = 0

    def note_resume_offset(self, offset: int) -> None:
        self.resume_offset = max(0, int(offset or 0))

    def accept(self, sample: dict | None) -> dict | None:
        if not sample:
            return None
        now = self.clock()
        downloaded = sample.get("downloaded_bytes")
        total = sample.get("total_bytes")
        if downloaded is not None and int(downloaded) >= 0:
            value = int(downloaded)
            self.downloaded_bytes = max(self.downloaded_bytes, value) if sample.get("source") == "disk" else value
        if total is not None and int(total) > 0:
            self.total_bytes = int(total)
            self.total_is_estimate = bool(sample.get("total_is_estimate"))
        if sample.get("speed_bps") is not None:
            self.speed_bps = float(sample["speed_bps"])
        if sample.get("eta_seconds") is not None:
            self.eta_seconds = float(sample["eta_seconds"])
        if sample.get("elapsed_seconds") is not None:
            self.elapsed_seconds = float(sample["elapsed_seconds"])
        else:
            self.elapsed_seconds = max(self.elapsed_seconds, now - self.started_at)
        if sample.get("filename"):
            self.filename = str(sample["filename"])
        source = str(sample.get("source") or "")
        if source:
            self.sources.add(source)
        self.last_update_at = now
        self.updates += 1
        self.last_emitted = {
            "downloaded_bytes": max(0, int(self.downloaded_bytes)),
            "total_bytes": self.total_bytes,
            "total_is_estimate": self.total_is_estimate,
            "speed_bps": self.speed_bps,
            "eta_seconds": self.eta_seconds,
            "elapsed_seconds": max(0.0, float(self.elapsed_seconds)),
            "filename": self.filename,
            "source": source,
            "fragment_index": sample.get("fragment_index"),
            "fragment_count": sample.get("fragment_count"),
        }
        return dict(self.last_emitted)

    def silent_for(self) -> float:
        reference = self.last_update_at if self.last_update_at is not None else self.started_at
        return max(0.0, self.clock() - reference)

    def has_telemetry(self) -> bool:
        return self.updates > 0
