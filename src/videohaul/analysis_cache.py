from __future__ import annotations

from copy import deepcopy
import threading
import time

from .models import MediaAnalysis


class AnalysisCache:
    def __init__(self, max_age_seconds: float = 600.0, clock=time.time):
        self.max_age_seconds = max(0.0, float(max_age_seconds))
        self.clock = clock
        self._values: dict[tuple[str, str], MediaAnalysis] = {}
        self._lock = threading.RLock()

    def key(self, url: str, browser_cookies: str) -> tuple[str, str]:
        return str(url).strip(), str(browser_cookies or "none").strip().lower()

    def is_stale(self, analysis: MediaAnalysis, now: float | None = None) -> bool:
        current = self.clock() if now is None else float(now)
        if analysis.expires_at is not None and analysis.expires_at <= current + 5:
            return True
        if not analysis.analyzed_at:
            return True
        return current - float(analysis.analyzed_at) > self.max_age_seconds

    def get(self, url: str, browser_cookies: str = "none") -> MediaAnalysis | None:
        key = self.key(url, browser_cookies)
        with self._lock:
            value = self._values.get(key)
            if value is None:
                return None
            if self.is_stale(value):
                self._values.pop(key, None)
                return None
            return deepcopy(value)

    def put(self, analysis: MediaAnalysis, browser_cookies: str = "none") -> MediaAnalysis:
        key = self.key(analysis.source_url, browser_cookies)
        with self._lock:
            self._values[key] = deepcopy(analysis)
        return analysis

    def invalidate(self, url: str | None = None) -> None:
        with self._lock:
            if url is None:
                self._values.clear()
                return
            normalized = str(url).strip()
            for key in [key for key in self._values if key[0] == normalized]:
                self._values.pop(key, None)
