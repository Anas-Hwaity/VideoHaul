from __future__ import annotations

from pathlib import Path
import secrets
import socket
import threading
import time

import uvicorn

from ..media_candidates import verify_candidate
from ..media_detection import MediaDetectorService
from ..preview import PreviewService
from ..recovery import RecoveryMonitor
from .api import create_app
from .events import EventBroker


class WebServer:
    def __init__(self, state, analyzer, jobs, host: str = "127.0.0.1", port: int = 0):
        self.state = state
        self.analyzer = analyzer
        self.jobs = jobs
        self.host = host
        self.port = port or self._free_port()
        self.static_dir = Path(__file__).resolve().parent / "static"
        self.access_token = secrets.token_urlsafe(32)
        self.allowed_hosts = {f"{self.host}:{self.port}", f"localhost:{self.port}"}
        self.broker = EventBroker(state)
        self.detectors = MediaDetectorService(observer=self._observe, probe=verify_candidate)
        self.previews = PreviewService(observer=self._observe)
        self.app = create_app(state, analyzer, jobs, self.static_dir, None, self.broker, self.detectors, self.previews, access_token=self.access_token, allowed_hosts=self.allowed_hosts)
        self._server = None
        self._thread = None
        self.recovery = RecoveryMonitor(jobs)

    def _observe(self, kind: str, message: str = "", payload: dict | None = None, level: str = "info") -> None:
        self.state.record_diagnostic(kind, message, payload or {}, level)

    def _free_port(self) -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def launch_url(self) -> str:
        return f"{self.url}/?token={self.access_token}"

    def start(self) -> None:
        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if bool(getattr(self._server, "started", False)):
                self.recovery.start()
                return
            if self._thread is not None and not self._thread.is_alive():
                break
            time.sleep(0.02)
        self.stop()
        raise RuntimeError("VideoHaul local server did not start")

    def stop(self) -> None:
        self.recovery.stop()
        try:
            self.jobs.shutdown()
        except Exception as exc:
            self.state.record_diagnostic("job_shutdown_failed", "Download workers could not be stopped cleanly", {"error": str(exc)}, "error")
        try:
            self.analyzer.shutdown()
        except Exception as exc:
            self.state.record_diagnostic("analysis_shutdown_failed", "Analysis workers could not be stopped cleanly", {"error": str(exc)}, "error")
        try:
            self.detectors.shutdown()
        except Exception:
            pass
        try:
            self.previews.shutdown()
        except Exception:
            pass
        try:
            self.broker.shutdown()
        except Exception:
            pass
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
