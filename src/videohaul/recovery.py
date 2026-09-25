from __future__ import annotations

import threading
import time


class RecoveryMonitor:
    def __init__(self, jobs, interval_seconds: float = 5.0, wake_gap_seconds: float = 15.0, clock=None):
        self.jobs = jobs
        self.interval_seconds = max(0.1, float(interval_seconds))
        self.wake_gap_seconds = max(self.interval_seconds, float(wake_gap_seconds))
        self.clock = clock or time.time
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_seen = float(self.clock())
        self._last_online = bool(self.jobs._online())
        self._transitions = 0

    def _connectivity_evidence(self) -> dict:
        checker = getattr(self.jobs, "network_checker", None)
        evidence = getattr(checker, "evidence", None)
        if evidence is None:
            return {"evidence": "the connectivity checker does not report evidence"}
        try:
            return {"evidence": evidence()}
        except Exception:
            return {"evidence": "connectivity evidence was unavailable"}

    def check(self, now: float | None = None) -> bool:
        current = float(self.clock() if now is None else now)
        gap = max(0.0, current - self._last_seen)
        self._last_seen = current
        online = bool(self.jobs._online())
        connectivity_changed = online != self._last_online
        if connectivity_changed:
            kind = "connectivity_restored" if online else "connectivity_lost"
            message = "Connectivity restored" if online else "Connectivity lost"
            payload = {"online": online, "transitions": self._transitions + 1}
            payload.update(self._connectivity_evidence())
            self.jobs.state.record_diagnostic(kind, message, payload, "info" if online else "warning")
            self._last_online = online
            self._transitions += 1
        woke = gap >= self.wake_gap_seconds
        if woke:
            self.jobs.state.record_diagnostic("wake_detected", "System wake or long execution gap detected", {"gap_seconds": gap})
        if woke or connectivity_changed:
            self.jobs.revalidate_environment()
        return woke or connectivity_changed

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._last_seen = float(self.clock())
        self._last_online = bool(self.jobs._online())
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds * 2))
        self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.check()
            except Exception as exc:
                self.jobs.state.record_diagnostic("recovery_monitor_failed", "Recovery monitor check failed", {"error": str(exc)}, "error")
