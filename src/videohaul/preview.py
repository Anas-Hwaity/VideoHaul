from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .http_security import build_scoped_opener, require_public_http_url, scope_headers
from .paths import DATA_ROOT, ensure_directories

VLC_MANIFEST = DATA_ROOT / "vlc.json"
IS_WINDOWS = os.name == "nt"
RELAY_IDLE_SECONDS = 900.0
RELAY_CHUNK = 256 * 1024

WINDOWS_VLC_LOCATIONS = (
    ("PROGRAMFILES", "VideoLAN/VLC/vlc.exe"),
    ("PROGRAMFILES(X86)", "VideoLAN/VLC/vlc.exe"),
    ("LOCALAPPDATA", "Programs/VideoLAN/VLC/vlc.exe"),
)

POSIX_VLC_LOCATIONS = (
    "/usr/bin/vlc",
    "/usr/local/bin/vlc",
    "/snap/bin/vlc",
    "/var/lib/flatpak/exports/bin/org.videolan.VLC",
    "/Applications/VLC.app/Contents/MacOS/VLC",
)


class PreviewUnavailable(RuntimeError):
    pass


VLC_EXECUTABLE_NAMES = {"vlc", "vlc.exe", "VLC"}


def _validate(executable: str | Path, runner=None) -> bool:
    try:
        path = Path(executable)
    except Exception:
        return False
    try:
        if not path.is_file():
            return False
    except OSError:
        return False
    if path.name not in VLC_EXECUTABLE_NAMES and path.stem.casefold() != "vlc":
        return False
    if runner is None:
        return True
    try:
        result = runner([str(path), "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except Exception:
        return False
    return int(getattr(result, "returncode", 1)) == 0


def _read_manifest() -> dict:
    try:
        return json.loads(VLC_MANIFEST.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_manifest(executable: str | Path) -> None:
    try:
        ensure_directories()
        temporary = VLC_MANIFEST.with_suffix(".tmp")
        temporary.write_text(json.dumps({"executable": str(Path(executable))}, indent=2), encoding="utf-8")
        os.replace(temporary, VLC_MANIFEST)
    except Exception:
        return


def _windows_registry_locations() -> list[str]:
    if not IS_WINDOWS:
        return []
    values: list[str] = []
    try:
        import winreg
    except Exception:
        return values
    keys = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\VideoLAN\VLC"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\VideoLAN\VLC"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\VideoLAN\VLC"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\vlc.exe"),
    )
    for root, path in keys:
        try:
            with winreg.OpenKey(root, path) as handle:
                value, _ = winreg.QueryValueEx(handle, "")
                text = str(value or "").strip('"')
                if text and text not in values:
                    values.append(text)
        except Exception:
            continue
    resolved: list[str] = []
    for value in values:
        candidate = Path(value)
        if candidate.is_dir():
            candidate = candidate / "vlc.exe"
        resolved.append(str(candidate))
    return resolved


def candidate_locations() -> list[str]:
    values: list[str] = []
    explicit = os.environ.get("VIDEOHAUL_VLC_EXECUTABLE")
    if explicit:
        values.append(explicit)
    for name in ("vlc.exe", "vlc", "VLC"):
        found = shutil.which(name)
        if found:
            values.append(found)
    if IS_WINDOWS:
        for variable, suffix in WINDOWS_VLC_LOCATIONS:
            root = os.environ.get(variable)
            if root:
                values.append(str(Path(root) / suffix))
        values.extend(_windows_registry_locations())
    else:
        values.extend(POSIX_VLC_LOCATIONS)
    ordered: list[str] = []
    for value in values:
        if value and value not in ordered:
            ordered.append(value)
    return ordered


_DISCOVERY_LOCK = threading.RLock()
_DISCOVERED: dict = {"executable": "", "checked_at": 0.0}
DISCOVERY_CACHE_SECONDS = 300.0


def find_vlc(runner=None, use_cache: bool = True) -> str | None:
    with _DISCOVERY_LOCK:
        if use_cache:
            remembered = str(_DISCOVERED.get("executable") or "")
            fresh = (time.monotonic() - float(_DISCOVERED.get("checked_at") or 0.0)) < DISCOVERY_CACHE_SECONDS
            if remembered and (fresh or _validate(remembered, runner)):
                if _validate(remembered, runner):
                    _DISCOVERED["checked_at"] = time.monotonic()
                    return remembered
                _DISCOVERED["executable"] = ""
            cached = str(_read_manifest().get("executable") or "")
            if cached and _validate(cached, runner):
                _DISCOVERED.update({"executable": cached, "checked_at": time.monotonic()})
                return cached
        for candidate in candidate_locations():
            if _validate(candidate, runner):
                _write_manifest(candidate)
                _DISCOVERED.update({"executable": str(candidate), "checked_at": time.monotonic()})
                return str(candidate)
        _DISCOVERED.update({"executable": "", "checked_at": time.monotonic()})
        return None


def forget_vlc() -> None:
    with _DISCOVERY_LOCK:
        _DISCOVERED.update({"executable": "", "checked_at": 0.0})


def vlc_status(runner=None) -> dict:
    executable = find_vlc(runner)
    return {
        "available": bool(executable),
        "executable": executable or "",
        "installs_on_demand": False,
        "detail": "Existing VLC installation reused" if executable else "No existing VLC installation was found",
    }


class LoopbackRelay:
    def __init__(self, target_url: str, headers: dict[str, str] | None = None, host: str = "127.0.0.1", opener=None):
        self.target_url = str(target_url or "")
        self.headers = {str(key): str(value) for key, value in (headers or {}).items()}
        self.host = host
        self.opener = opener
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.token = os.urandom(16).hex()
        self.last_activity = time.time()

    @property
    def url(self) -> str:
        if self._server is None:
            raise PreviewUnavailable("Relay is not running")
        return f"http://{self.host}:{self._server.server_address[1]}/{self.token}"

    def start(self) -> "LoopbackRelay":
        relay = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return

            def _forward(self, include_body: bool) -> None:
                if self.path.lstrip("/") != relay.token:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                relay.last_activity = time.time()
                target = require_public_http_url(relay.target_url) if relay.opener is None else relay.target_url
                request = urllib.request.Request(target, method="GET")
                for key, value in scope_headers(relay.headers, target, relay.target_url).items():
                    request.add_header(key, value)
                incoming_range = self.headers.get("Range")
                if incoming_range:
                    request.add_header("Range", incoming_range)
                try:
                    open_url = relay.opener or build_scoped_opener(target, relay.target_url, True).open
                    with open_url(request, timeout=45) as response:
                        self.send_response(int(getattr(response, "status", 200) or 200))
                        for name in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                            value = response.headers.get(name)
                            if value:
                                self.send_header(name, value)
                        self.end_headers()
                        if not include_body:
                            return
                        while True:
                            chunk = response.read(RELAY_CHUNK)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            relay.last_activity = time.time()
                except urllib.error.HTTPError as exc:
                    self.send_response(int(exc.code))
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                except Exception:
                    try:
                        self.send_response(502)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                    except Exception:
                        return

            def do_GET(self):
                self._forward(True)

            def do_HEAD(self):
                self._forward(False)

        self._server = ThreadingHTTPServer((self.host, 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="videohaul-preview-relay")
        self._thread.start()
        return self

    def expired(self, idle_seconds: float = RELAY_IDLE_SECONDS) -> bool:
        return (time.time() - self.last_activity) > float(idle_seconds)

    def stop(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass


class PreviewService:
    def __init__(self, locator=find_vlc, launcher=None, relay_factory=LoopbackRelay, observer=None):
        self.locator = locator
        self.launcher = launcher or _default_launcher
        self.relay_factory = relay_factory
        self.observer = observer
        self._relays: dict[str, LoopbackRelay] = {}
        self._lock = threading.RLock()

    def _emit(self, kind: str, message: str, payload: dict | None = None, level: str = "info") -> None:
        if self.observer is None:
            return
        try:
            self.observer(kind, message, payload or {}, level)
        except Exception:
            return

    def available(self) -> bool:
        return bool(self.locator())

    def _prune(self) -> None:
        with self._lock:
            expired = [(key, relay) for key, relay in self._relays.items() if relay.expired()]
            for key, relay in expired:
                if self._relays.get(key) is relay:
                    self._relays.pop(key, None)
        for _, relay in expired:
            relay.stop()

    def preview(self, candidate, full_stream: bool = False) -> dict:
        executable = self.locator()
        if not executable:
            self._emit("preview_unavailable", "No existing VLC installation was found", {"candidate_id": getattr(candidate, "candidate_id", "")}, "warning")
            raise PreviewUnavailable("No existing VLC installation was found. VideoHaul never installs VLC for preview.")
        from .media_candidates import candidate_request_headers, redact_url

        self._prune()
        target = getattr(candidate, "final_url", "") or getattr(candidate, "discovered_url", "")
        if not target:
            raise PreviewUnavailable("This candidate has no playable URL")
        headers = candidate_request_headers(candidate)
        needs_relay = bool(getattr(candidate, "required_headers", None)) or bool(getattr(candidate, "required_cookie_scope", ""))
        play_url = target
        relay_used = False
        if needs_relay:
            key = str(getattr(candidate, "candidate_id", "") or target)
            with self._lock:
                relay = self._relays.get(key)
            if relay is None:
                created = self.relay_factory(target, headers)
                created.start()
                discard = None
                with self._lock:
                    relay = self._relays.get(key)
                    if relay is None:
                        self._relays[key] = created
                        relay = created
                    else:
                        discard = created
                if discard is not None:
                    discard.stop()
            play_url = relay.url
            relay_used = True
        arguments = [str(executable), "--no-video-title-show", play_url]
        if not full_stream:
            arguments.insert(1, "--start-time=0")
            arguments.insert(2, "--run-time=20")
            arguments.insert(3, "--play-and-exit")
        started = self.launcher(arguments)
        self._emit(
            "preview_launched" if started else "preview_launch_failed",
            "Candidate preview launched in existing VLC" if started else "VLC was found but could not be started",
            {
                "candidate_id": getattr(candidate, "candidate_id", ""),
                "url": redact_url(target),
                "relay": relay_used,
                "full_stream": bool(full_stream),
                "executable": str(executable),
            },
            "info" if started else "error",
        )
        return {
            "ok": bool(started),
            "executable": str(executable),
            "relay": relay_used,
            "full_stream": bool(full_stream),
            "detail": "Opened in the existing VLC installation" if started else "VLC could not be launched",
        }

    def shutdown(self) -> None:
        with self._lock:
            values = list(self._relays.values())
            self._relays.clear()
        for relay in values:
            relay.stop()


def _default_launcher(arguments: list[str]) -> bool:
    flags = 0x08000000 if IS_WINDOWS else 0
    try:
        subprocess.Popen(arguments, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        return True
    except Exception:
        return False


def free_loopback_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def python_executable() -> str:
    return sys.executable
