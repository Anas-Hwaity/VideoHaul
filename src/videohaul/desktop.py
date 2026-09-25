from __future__ import annotations

import os
import threading

from .models import JobStatus
from .progress import aggregate_progress

IS_WINDOWS = os.name == "nt"

TASKBAR_STATE_NO_PROGRESS = 0
TASKBAR_STATE_INDETERMINATE = 1
TASKBAR_STATE_NORMAL = 2
TASKBAR_STATE_ERROR = 4
TASKBAR_STATE_PAUSED = 8

ACTIVE_STATUSES = {JobStatus.ANALYZING, JobStatus.DOWNLOADING, JobStatus.FINALIZING}


def taskbar_state(jobs) -> dict:
    values = list(jobs or [])
    active = [job for job in values if job.status in ACTIVE_STATUSES]
    failed = [job for job in values if job.status in {JobStatus.FAILED, JobStatus.UNAVAILABLE}]
    paused = [job for job in values if job.status == JobStatus.PAUSED]
    if not active:
        if failed:
            return {"state": TASKBAR_STATE_ERROR, "fraction": None, "reason": "failed jobs"}
        if paused:
            return {"state": TASKBAR_STATE_PAUSED, "fraction": None, "reason": "paused jobs"}
        return {"state": TASKBAR_STATE_NO_PROGRESS, "fraction": None, "reason": "idle"}
    aggregate = aggregate_progress(values)
    if aggregate["determinate"] and aggregate["fraction"] is not None:
        return {"state": TASKBAR_STATE_NORMAL, "fraction": float(aggregate["fraction"]), "reason": "aggregate byte progress"}
    return {"state": TASKBAR_STATE_INDETERMINATE, "fraction": None, "reason": "unknown total bytes"}


class TaskbarProgress:
    def __init__(self, observer=None):
        self.observer = observer
        self.available = False
        self.detail = "Taskbar progress is only available on Windows"
        self._taskbar = None
        self._handle = None
        self._lock = threading.RLock()
        self._last: tuple[int, float | None] | None = None

    def attach(self, window_handle=None) -> bool:
        if not IS_WINDOWS:
            return False
        if window_handle in (None, 0):
            self.available = False
            self.detail = "Taskbar progress needs a valid window handle"
            self._emit("taskbar_progress_unavailable", self.detail, "warning")
            return False
        with self._lock:
            try:
                import comtypes
                import comtypes.client
            except Exception as exc:
                self.available = False
                self.detail = f"Taskbar progress needs the comtypes package: {exc}"
                self._emit("taskbar_progress_unavailable", self.detail, "warning")
                return False
            try:
                interface = _taskbar_interface(comtypes)
                self._taskbar = comtypes.client.CreateObject(
                    "{56FDF344-FD6D-11d0-958A-006097C9A090}",
                    interface=interface,
                )
                self._taskbar.HrInit()
                self._handle = window_handle
                self.available = True
                self.detail = "Taskbar progress attached"
                self._emit("taskbar_progress_attached", self.detail)
                return True
            except Exception as exc:
                self._taskbar = None
                self.available = False
                self.detail = f"Taskbar progress is unavailable: {exc}"
                self._emit("taskbar_progress_unavailable", self.detail, "warning")
                return False

    def apply(self, jobs) -> dict:
        value = taskbar_state(jobs)
        with self._lock:
            signature = (int(value["state"]), value["fraction"])
            if signature == self._last:
                return value
            self._last = signature
            if not self.available or self._taskbar is None or self._handle is None:
                return value
            try:
                self._taskbar.SetProgressState(self._handle, int(value["state"]))
                if value["fraction"] is not None:
                    self._taskbar.SetProgressValue(self._handle, int(round(float(value["fraction"]) * 1000)), 1000)
            except Exception as exc:
                self.available = False
                self.detail = f"Taskbar progress stopped: {exc}"
                self._emit("taskbar_progress_failed", self.detail, "warning")
        return value

    def clear(self) -> None:
        with self._lock:
            self._last = None
            if self.available and self._taskbar is not None and self._handle is not None:
                try:
                    self._taskbar.SetProgressState(self._handle, TASKBAR_STATE_NO_PROGRESS)
                except Exception:
                    return

    def _emit(self, kind: str, message: str, level: str = "info") -> None:
        if self.observer is None:
            return
        try:
            self.observer(kind, message, {"available": self.available}, level)
        except Exception:
            return


def _taskbar_interface(comtypes):
    from ctypes import HRESULT, c_int, c_ulonglong
    from ctypes.wintypes import HWND
    from comtypes import COMMETHOD, GUID, IUnknown

    class ITaskbarList3(IUnknown):
        _iid_ = GUID("{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}")
        _methods_ = [
            COMMETHOD([], HRESULT, "HrInit"),
            COMMETHOD([], HRESULT, "AddTab", (["in"], HWND, "hwnd")),
            COMMETHOD([], HRESULT, "DeleteTab", (["in"], HWND, "hwnd")),
            COMMETHOD([], HRESULT, "ActivateTab", (["in"], HWND, "hwnd")),
            COMMETHOD([], HRESULT, "SetActiveAlt", (["in"], HWND, "hwnd")),
            COMMETHOD([], HRESULT, "MarkFullscreenWindow", (["in"], HWND, "hwnd"), (["in"], c_int, "fullscreen")),
            COMMETHOD([], HRESULT, "SetProgressValue", (["in"], HWND, "hwnd"), (["in"], c_ulonglong, "completed"), (["in"], c_ulonglong, "total")),
            COMMETHOD([], HRESULT, "SetProgressState", (["in"], HWND, "hwnd"), (["in"], c_int, "state")),
        ]

    return ITaskbarList3


def window_geometry(settings) -> dict:
    width = max(760, int(getattr(settings, "window_width", 1180) or 1180))
    height = max(560, int(getattr(settings, "window_height", 820) or 820))
    x = getattr(settings, "window_x", None)
    y = getattr(settings, "window_y", None)
    geometry = {"width": width, "height": height}
    if x is not None and y is not None:
        geometry["x"] = int(x)
        geometry["y"] = int(y)
    return geometry


def persist_window_geometry(state, width, height, x=None, y=None) -> dict:
    values = {}
    try:
        values["window_width"] = max(760, int(width))
        values["window_height"] = max(560, int(height))
    except Exception:
        return {}
    if x is not None and y is not None:
        try:
            values["window_x"] = int(x)
            values["window_y"] = int(y)
        except Exception:
            values.pop("window_x", None)
            values.pop("window_y", None)
    try:
        state.update_settings(values)
    except Exception:
        return {}
    return values


def active_job_summary(jobs) -> dict:
    values = list(jobs or [])
    active = [job for job in values if job.status in ACTIVE_STATUSES]
    queued = [job for job in values if job.status == JobStatus.QUEUED]
    return {
        "active": len(active),
        "queued": len(queued),
        "should_warn_on_exit": bool(active or queued),
        "message": _exit_message(len(active), len(queued)),
    }


def _exit_message(active: int, queued: int) -> str:
    if not active and not queued:
        return ""
    parts = []
    if active:
        parts.append(f"{active} active download{'s' if active != 1 else ''}")
    if queued:
        parts.append(f"{queued} queued download{'s' if queued != 1 else ''}")
    return f"VideoHaul still has {' and '.join(parts)}. Closing now will stop them."


def tray_state(settings, jobs) -> dict:
    enabled = bool(getattr(settings, "minimize_to_tray", False))
    summary = active_job_summary(jobs)
    return {
        "enabled": enabled,
        "should_minimize_to_tray": bool(enabled and summary["active"]),
        "active": summary["active"],
        "detail": "" if enabled else "Minimize to tray is disabled in Settings",
    }
