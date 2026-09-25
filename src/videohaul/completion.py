from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from .files import open_directory
from .models import DownloadJob

IS_WINDOWS = os.name == "nt"
AGGREGATION_WINDOW_SECONDS = 4.0
AGGREGATION_THRESHOLD = 3

TERMINAL_ACTIONS = {"sleep", "shutdown"}
WINDOWS_TOAST_APP_ID = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe"
WINDOWS_TOAST_SCRIPT = (
    "[Windows.UI.Notifications.ToastNotification,Windows.UI.Notifications,ContentType=WindowsRuntime]|Out-Null;"
    "$template=[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
    "$texts=$template.GetElementsByTagName('text');"
    "$texts.Item(0).AppendChild($template.CreateTextNode($env:VIDEOHAUL_TOAST_TITLE))|Out-Null;"
    "$texts.Item(1).AppendChild($template.CreateTextNode($env:VIDEOHAUL_TOAST_MESSAGE))|Out-Null;"
    "$toast=[Windows.UI.Notifications.ToastNotification]::new($template);"
    f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{WINDOWS_TOAST_APP_ID}').Show($toast)"
)
SUPPORTED_ACTIONS = {"none", "open_file", "open_folder", "play_file", "move_file", "custom_command", "sleep", "shutdown"}


def platform_notifier(payload: dict) -> bool:
    title = str(payload.get("title") or "VideoHaul")
    message = str(payload.get("message") or "Download finished")
    flags = 0x08000000 if IS_WINDOWS else 0
    try:
        if IS_WINDOWS:
            environment = os.environ.copy()
            environment["VIDEOHAUL_TOAST_TITLE"] = title
            environment["VIDEOHAUL_TOAST_MESSAGE"] = message
            subprocess.Popen(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", WINDOWS_TOAST_SCRIPT],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                env=environment,
            )
            return True
        if sys.platform == "darwin":
            safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
            safe_message = message.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.Popen(
                ["osascript", "-e", f'display notification "{safe_message}" with title "{safe_title}"'],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        executable = shutil.which("notify-send")
        if executable:
            subprocess.Popen(
                [executable, title, message],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
    except Exception:
        return False
    return False


def platform_completion_sound() -> bool:
    try:
        if IS_WINDOWS:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONASTERISK)
            return True
        if sys.platform == "darwin":
            sound = Path("/System/Library/Sounds/Glass.aiff")
            executable = shutil.which("afplay")
            if executable and sound.is_file():
                subprocess.Popen([executable, str(sound)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
        for executable_name, sound_path in (
            ("canberra-gtk-play", "/usr/share/sounds/freedesktop/stereo/complete.oga"),
            ("paplay", "/usr/share/sounds/freedesktop/stereo/complete.oga"),
        ):
            executable = shutil.which(executable_name)
            if executable and Path(sound_path).is_file():
                arguments = [executable, "-f", sound_path] if executable_name == "canberra-gtk-play" else [executable, sound_path]
                subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
    except Exception:
        return False
    return False


class CompletionResult:
    def __init__(self, action: str, state: str, detail: str = ""):
        self.action = str(action)
        self.state = str(state)
        self.detail = str(detail or "")

    def to_dict(self) -> dict:
        return {"action": self.action, "state": self.state, "detail": self.detail}

    def __repr__(self) -> str:
        return f"CompletionResult({self.action!r}, {self.state!r}, {self.detail!r})"


class NotificationAggregator:
    def __init__(self, notifier=None, window_seconds: float = AGGREGATION_WINDOW_SECONDS, threshold: int = AGGREGATION_THRESHOLD, clock=time.monotonic):
        self.notifier = notifier
        self.window_seconds = float(window_seconds)
        self.threshold = max(1, int(threshold))
        self.clock = clock
        self._lock = threading.RLock()
        self._pending: list[str] = []
        self._window_started: float | None = None
        self._timer: threading.Timer | None = None

    def add(self, title: str) -> dict | None:
        with self._lock:
            now = self.clock()
            if self._window_started is None or (now - self._window_started) > self.window_seconds:
                self._flush_locked()
                self._window_started = now
            self._pending.append(str(title or "Download"))
            if len(self._pending) >= self.threshold:
                return self._flush_locked()
            if self._timer is None:
                self._timer = threading.Timer(self.window_seconds, self.flush)
                self._timer.daemon = True
                self._timer.start()
            return None

    def flush(self) -> dict | None:
        with self._lock:
            return self._flush_locked()

    def _flush_locked(self) -> dict | None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if not self._pending:
            self._window_started = None
            return None
        count = len(self._pending)
        titles = list(self._pending)
        self._pending = []
        self._window_started = None
        if count == 1:
            payload = {"count": 1, "title": titles[0], "message": f"{titles[0]} finished downloading"}
        else:
            payload = {"count": count, "title": f"{count} downloads finished", "message": f"{count} downloads finished, including {titles[0]}"}
        return self._emit(payload)

    def _emit(self, payload: dict) -> dict:
        if self.notifier is not None:
            try:
                self.notifier(payload)
            except Exception:
                return payload
        return payload

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)


class CompletionService:
    def __init__(self, state, notifier=None, launcher=None, power=None, runner=subprocess.run, aggregator=None, sound_player=None):
        self.state = state
        self.launcher = launcher
        self.power = power
        self.runner = runner
        self.sound_player = sound_player or platform_completion_sound
        self.aggregator = aggregator or NotificationAggregator(notifier or platform_notifier)

    def _diag(self, job_id: str, kind: str, message: str, payload: dict | None = None, level: str = "info") -> None:
        data = {"job_id": job_id}
        data.update(payload or {})
        try:
            self.state.record_diagnostic(kind, message, data, level)
        except Exception:
            return

    def notify(self, job: DownloadJob) -> dict | None:
        if not job.settings.notify_complete:
            return None
        title = ""
        if job.analysis and job.analysis.title:
            title = str(job.analysis.title)
        title = title or str(job.source_url or "Download")
        payload = self.aggregator.add(title)
        if payload is not None:
            self._diag(job.job_id, "completion_notification", payload["message"], {"count": payload["count"]})
        return payload

    def sound(self, job: DownloadJob) -> bool:
        if not job.settings.sound_complete:
            return False
        played = bool(self.sound_player())
        self._diag(
            job.job_id,
            "completion_sound_played" if played else "completion_sound_unavailable",
            "Completion sound played" if played else "No supported completion sound backend is available",
            {"played": played},
            "info" if played else "warning",
        )
        return played

    def run(self, job: DownloadJob) -> CompletionResult:
        action = str(job.settings.completion_action or "none")
        if action not in SUPPORTED_ACTIONS:
            return self._record(job, CompletionResult(action, "unsupported", f"{action} is not a supported completion action"))
        if action == "none":
            return self._record(job, CompletionResult(action, "skipped", "No completion action is configured"))
        output = Path(str(job.output_path or ""))
        if action in {"open_file", "play_file"}:
            if not output.is_file():
                return self._record(job, CompletionResult(action, "unavailable", "Completed output is no longer available"))
            return self._record(job, self._open(action, output))
        if action == "open_folder":
            folder = output.parent if job.output_path else Path(str(job.settings.destination or ""))
            if not folder.is_dir():
                return self._record(job, CompletionResult(action, "unavailable", f"Destination is unavailable: {folder}"))
            self._launch([str(folder)], folder, directory=True)
            return self._record(job, CompletionResult(action, "done", f"Opened {folder}"))
        if action == "move_file":
            return self._record(job, self._move(job, output))
        if action == "custom_command":
            return self._record(job, self._custom(job, output))
        return self._record(job, self._power(action))

    def _record(self, job: DownloadJob, result: CompletionResult) -> CompletionResult:
        level = "info" if result.state in {"done", "skipped"} else "warning"
        self._diag(job.job_id, "completion_action_result", result.detail or result.state, result.to_dict(), level)
        return result

    def _open(self, action: str, output: Path) -> CompletionResult:
        self._launch([str(output)], output, directory=False)
        return CompletionResult(action, "done", f"Opened {output.name}")

    def _launch(self, arguments: list[str], target: Path, directory: bool) -> None:
        if self.launcher is not None:
            self.launcher(arguments)
            return
        if directory:
            open_directory(target)
            return
        if IS_WINDOWS:
            os.startfile(str(target))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])

    def _move(self, job: DownloadJob, output: Path) -> CompletionResult:
        target = str(getattr(job.settings, "completion_move_destination", "") or "").strip()
        if not target:
            return CompletionResult("move_file", "unavailable", "No move destination is configured for this job")
        if not output.is_file():
            return CompletionResult("move_file", "unavailable", "Completed output is no longer available")
        destination = Path(target)
        sources = [output]
        for raw in list(getattr(job, "companion_paths", None) or []):
            candidate = Path(str(raw or ""))
            try:
                if candidate.is_file() and candidate.parent.resolve() == output.parent.resolve():
                    sources.append(candidate)
            except Exception:
                continue
        moved: list[tuple[Path, Path]] = []
        try:
            destination.mkdir(parents=True, exist_ok=True)
            final = destination / output.name
            index = 1
            while final.exists():
                final = destination / f"{output.stem} ({index}){output.suffix}"
                index += 1
            plans = [(output, final)]
            for source in sources[1:]:
                suffix = source.name[len(output.stem):] if source.name.startswith(output.stem) else source.suffix
                candidate = destination / f"{final.stem}{suffix}"
                companion_index = 2
                while candidate.exists() or any(existing == candidate for _, existing in plans):
                    candidate = destination / f"{final.stem} ({companion_index}){suffix}"
                    companion_index += 1
                plans.append((source, candidate))
            for source, target_path in plans:
                shutil.move(str(source), str(target_path))
                moved.append((source, target_path))
        except Exception as exc:
            for source, target_path in reversed(moved):
                try:
                    if target_path.exists() and not source.exists():
                        shutil.move(str(target_path), str(source))
                except Exception:
                    pass
            return CompletionResult("move_file", "failed", str(exc))
        companions = [str(target_path) for _, target_path in moved[1:]]
        try:
            self.state.update_job(job.job_id, output_path=str(final), companion_paths=companions)
        except Exception:
            pass
        return CompletionResult("move_file", "done", f"Moved to {final}")

    def _custom(self, job: DownloadJob, output: Path) -> CompletionResult:
        template = str(getattr(job.settings, "completion_command", "") or "").strip()
        if not template:
            return CompletionResult("custom_command", "unavailable", "No custom command is configured for this job")
        try:
            parts = shlex.split(template, posix=not IS_WINDOWS)
        except ValueError as exc:
            return CompletionResult("custom_command", "failed", f"Custom command could not be parsed: {exc}")
        if not parts:
            return CompletionResult("custom_command", "unavailable", "No custom command is configured for this job")
        arguments = [str(output) if part == "{output}" else part.replace("{output}", str(output)) for part in parts]
        try:
            flags = 0x08000000 if IS_WINDOWS else 0
            result = self.runner(arguments, capture_output=True, timeout=120, creationflags=flags)
        except Exception as exc:
            return CompletionResult("custom_command", "failed", str(exc))
        code = int(getattr(result, "returncode", 0) or 0)
        if code != 0:
            return CompletionResult("custom_command", "failed", f"Custom command exited with code {code}")
        return CompletionResult("custom_command", "done", "Custom command completed")

    def _power(self, action: str) -> CompletionResult:
        if self.power is not None:
            try:
                self.power(action)
            except Exception as exc:
                return CompletionResult(action, "failed", str(exc))
            return CompletionResult(action, "done", f"Requested system {action}")
        command = _power_command(action)
        if not command:
            return CompletionResult(action, "unsupported", f"System {action} is not supported on this platform")
        try:
            flags = 0x08000000 if IS_WINDOWS else 0
            result = self.runner(command, capture_output=True, timeout=30, creationflags=flags)
        except Exception as exc:
            return CompletionResult(action, "failed", str(exc))
        code = int(getattr(result, "returncode", 0) or 0)
        if code != 0:
            return CompletionResult(action, "failed", f"System {action} command exited with code {code}")
        return CompletionResult(action, "done", f"Requested system {action}")


def _power_command(action: str) -> list[str]:
    if IS_WINDOWS:
        if action == "shutdown":
            return ["shutdown", "/s", "/t", "30"]
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", "Add-Type -AssemblyName System.Windows.Forms;[System.Windows.Forms.Application]::SetSuspendState([System.Windows.Forms.PowerState]::Suspend,$false,$false)|Out-Null"]
    if sys.platform == "darwin":
        return ["osascript", "-e", 'tell application "System Events" to sleep'] if action == "sleep" else ["osascript", "-e", 'tell application "System Events" to shut down']
    return ["systemctl", "suspend"] if action == "sleep" else ["systemctl", "poweroff"]
