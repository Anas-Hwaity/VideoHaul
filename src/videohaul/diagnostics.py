from __future__ import annotations

import re
import time
from typing import Any


SECRET_KEYS = {"authorization", "cookie", "cookies", "password", "token", "secret"}
URL_QUERY = re.compile(r"(https?://[^\s?'\"<>]+)\?[^\s'\"<>]+", re.IGNORECASE)
URL_USERINFO = re.compile(r"(https?://)[^/\s@]+@", re.IGNORECASE)
BEARER = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+\-/=]+")


def sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            lowered = str(key).casefold()
            if any(secret in lowered for secret in SECRET_KEYS):
                result[str(key)] = "[redacted]"
            else:
                result[str(key)] = sanitize_payload(item)
        return result
    if isinstance(value, (list, tuple, set)):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, str):
        redacted = URL_USERINFO.sub(r"\1[redacted]@", value)
        return BEARER.sub(r"\1[redacted]", URL_QUERY.sub(r"\1?[redacted]", redacted))
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def diagnostic_entry(kind: str, message: str = "", payload: dict | None = None, level: str = "info", at: float | None = None) -> dict:
    normalized_level = str(level or "info").casefold()
    if normalized_level not in {"debug", "info", "warning", "error"}:
        normalized_level = "info"
    return {
        "at": float(at if at is not None else time.time()),
        "level": normalized_level,
        "kind": str(kind or "event"),
        "message": sanitize_payload(str(message or "")),
        "payload": sanitize_payload(dict(payload or {})),
    }


def redact_command(args: list[str]) -> list[str]:
    sensitive = {"--cookies", "--cookies-from-browser", "--referer", "--user-agent", "--username", "--password", "--video-password", "--add-header"}
    result = []
    hide_next = False
    for index, raw in enumerate(args):
        value = str(raw)
        if hide_next:
            result.append("[redacted]")
            hide_next = False
            continue
        result.append(value)
        if value in sensitive:
            hide_next = True
        elif index == len(args) - 1 and value.startswith(("http://", "https://")):
            result[-1] = value.split("?", 1)[0] + ("?[redacted]" if "?" in value else "")
    return result
