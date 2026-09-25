from __future__ import annotations

import argparse
import threading
import time
import webbrowser

from .analysis import Analyzer
from .desktop import TaskbarProgress, active_job_summary, persist_window_geometry, tray_state, window_geometry
from .jobs import JobController
from .persistence import Store
from .state import VideoHaulState
from .tray import WindowsTray, native_window_handle
from .web.server import WebServer

TASKBAR_REFRESH_SECONDS = 1.0


def build_runtime(store: Store | None = None):
    store = store or Store()
    state = VideoHaulState(store)
    analyzer = Analyzer(state)

    jobs = JobController(state)
    server = WebServer(state, analyzer, jobs)
    return state, analyzer, jobs, server


def _observe(state):
    def observer(kind: str, message: str = "", payload: dict | None = None, level: str = "info") -> None:
        state.record_diagnostic(kind, message, payload or {}, level)

    return observer


def _run_desktop_loop(state, taskbar: TaskbarProgress, tray: WindowsTray, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            taskbar.apply(state.jobs)
        except Exception:
            pass
        try:
            if state.settings.minimize_to_tray:
                tray.start()
            else:
                tray.close()
        except Exception:
            pass
        stop.wait(TASKBAR_REFRESH_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--browser", action="store_true")
    parser.add_argument("--no-webview", action="store_true")
    args, _ = parser.parse_known_args()
    state, analyzer, jobs, server = build_runtime()
    server.start()
    state.record_diagnostic("application_ready", "VideoHaul local interface is ready", {"url": server.url})
    if args.browser or args.no_webview:
        print(f"VideoHaul is running. Open this private link in your browser: {server.launch_url}", flush=True)
        webbrowser.open(server.launch_url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            server.stop()
        return
    stop = threading.Event()
    taskbar = TaskbarProgress(observer=_observe(state))
    tray = None
    window = None
    try:
        import webview

        geometry = window_geometry(state.settings)
        window = webview.create_window(
            "VideoHaul",
            server.launch_url,
            width=geometry["width"],
            height=geometry["height"],
            x=geometry.get("x"),
            y=geometry.get("y"),
            min_size=(760, 560),
        )

        def restore_window() -> None:
            try:
                window.show()
            except Exception:
                pass
            try:
                window.restore()
            except Exception:
                pass

        def exit_from_tray() -> None:
            try:
                window.destroy()
            except Exception:
                stop.set()

        tray = WindowsTray(
            on_open=restore_window,
            on_pause_all=jobs.pause_all,
            on_resume_all=jobs.resume_all,
            on_exit=exit_from_tray,
            observer=_observe(state),
        )

        def on_closing() -> bool:
            summary = active_job_summary(state.jobs)
            if summary["should_warn_on_exit"]:
                state.record_diagnostic("exit_warning_shown", summary["message"], summary, "warning")
                try:
                    confirmed = window.create_confirmation_dialog("VideoHaul", f"{summary['message']} Close anyway?")
                except Exception:
                    confirmed = True
                if not confirmed:
                    return False
            try:
                persist_window_geometry(state, window.width, window.height, getattr(window, "x", None), getattr(window, "y", None))
            except Exception:
                pass
            stop.set()
            taskbar.clear()
            if tray is not None:
                tray.close()
            return True

        try:
            window.events.closing += on_closing
        except Exception:
            state.record_diagnostic("exit_guard_unavailable", "Window close events are unavailable in this shell", {}, "warning")

        def on_loaded() -> None:
            taskbar.attach(native_window_handle(window))
            threading.Thread(target=_run_desktop_loop, args=(state, taskbar, tray, stop), daemon=True, name="videohaul-desktop-state").start()

        def on_minimized() -> None:
            value = tray_state(state.settings, state.jobs)
            if not value["should_minimize_to_tray"]:
                return
            if tray is not None and tray.start():
                try:
                    window.hide()
                    state.record_diagnostic("window_minimized_to_tray", "VideoHaul window moved to the system tray", value)
                except Exception as exc:
                    state.record_diagnostic("tray_hide_failed", str(exc), value, "warning")

        try:
            window.events.loaded += on_loaded
        except Exception:
            on_loaded()
        try:
            window.events.minimized += on_minimized
        except Exception:
            state.record_diagnostic("tray_minimize_event_unavailable", "Window minimize events are unavailable in this shell", {}, "warning")
        webview.start()
    except Exception as exc:
        state.record_diagnostic("desktop_shell_unavailable", "Desktop shell is unavailable; opening the browser dashboard instead", {"error": str(exc)}, "warning")
        print(f"VideoHaul is running. Open this private link in your browser: {server.launch_url}", flush=True)
        webbrowser.open(server.launch_url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    finally:
        stop.set()
        try:
            taskbar.clear()
        except Exception:
            pass
        if tray is not None:
            tray.close()
        server.stop()
