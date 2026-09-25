from __future__ import annotations

import os
import threading
import uuid


IS_WINDOWS = os.name == "nt"


class WindowsTray:
    """Small native Windows notification-area host with no extra dependency."""

    def __init__(self, on_open=None, on_pause_all=None, on_resume_all=None, on_exit=None, observer=None):
        self.on_open = on_open
        self.on_pause_all = on_pause_all
        self.on_resume_all = on_resume_all
        self.on_exit = on_exit
        self.observer = observer
        self.available = False
        self.detail = "System tray integration is only available on Windows"
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._hwnd = None
        self._lock = threading.RLock()

    def start(self) -> bool:
        if not IS_WINDOWS:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.available
            self._ready.clear()
            self._thread = threading.Thread(target=self._run, daemon=True, name="videohaul-tray")
            self._thread.start()
        self._ready.wait(2.0)
        return self.available

    def close(self) -> None:
        with self._lock:
            hwnd = self._hwnd
            thread = self._thread
        if hwnd:
            try:
                import ctypes

                ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
            except Exception:
                pass
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        with self._lock:
            self._thread = None
            self._hwnd = None
            self.available = False

    def _emit(self, kind: str, message: str, level: str = "info") -> None:
        if self.observer is None:
            return
        try:
            self.observer(kind, message, {"available": self.available}, level)
        except Exception:
            return

    def _dispatch(self, callback) -> None:
        if callback is None:
            return
        threading.Thread(target=self._safe_call, args=(callback,), daemon=True, name="videohaul-tray-action").start()

    @staticmethod
    def _safe_call(callback) -> None:
        try:
            callback()
        except Exception:
            return

    def _run(self) -> None:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        shell32 = ctypes.windll.shell32
        kernel32 = ctypes.windll.kernel32
        callback_message = 0x8000 + 73
        open_id, pause_id, resume_id, exit_id = 1001, 1002, 1003, 1004
        class_name = f"VideoHaulTray_{uuid.uuid4().hex}"

        wndproc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", wndproc_type),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        class NOTIFYICONDATAW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uTimeoutOrVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16),
                ("hBalloonIcon", wintypes.HICON),
            ]

        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.CreatePopupMenu.restype = wintypes.HMENU
        user32.LoadIconW.restype = wintypes.HICON
        shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
        shell32.Shell_NotifyIconW.restype = wintypes.BOOL

        notify = NOTIFYICONDATAW()

        def show_menu(hwnd) -> None:
            menu = user32.CreatePopupMenu()
            try:
                user32.AppendMenuW(menu, 0, open_id, "Open VideoHaul")
                user32.AppendMenuW(menu, 0x800, 0, None)
                user32.AppendMenuW(menu, 0, pause_id, "Pause all")
                user32.AppendMenuW(menu, 0, resume_id, "Resume all")
                user32.AppendMenuW(menu, 0x800, 0, None)
                user32.AppendMenuW(menu, 0, exit_id, "Exit VideoHaul")
                point = wintypes.POINT()
                user32.GetCursorPos(ctypes.byref(point))
                user32.SetForegroundWindow(hwnd)
                choice = user32.TrackPopupMenu(menu, 0x0100 | 0x0002, point.x, point.y, 0, hwnd, None)
                if choice == open_id:
                    self._dispatch(self.on_open)
                elif choice == pause_id:
                    self._dispatch(self.on_pause_all)
                elif choice == resume_id:
                    self._dispatch(self.on_resume_all)
                elif choice == exit_id:
                    self._dispatch(self.on_exit)
            finally:
                user32.DestroyMenu(menu)

        @wndproc_type
        def wndproc(hwnd, message, wparam, lparam):
            if message == callback_message:
                mouse_message = int(lparam) & 0xFFFF
                if mouse_message in {0x0202, 0x0203}:
                    self._dispatch(self.on_open)
                elif mouse_message == 0x0205:
                    show_menu(hwnd)
                return 0
            if message == 0x0010:
                user32.DestroyWindow(hwnd)
                return 0
            if message == 0x0002:
                shell32.Shell_NotifyIconW(2, ctypes.byref(notify))
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        instance = kernel32.GetModuleHandleW(None)
        window_class = WNDCLASSW(0, wndproc, 0, 0, instance, None, None, None, None, class_name)
        atom = user32.RegisterClassW(ctypes.byref(window_class))
        if not atom:
            self.detail = "Windows could not register the tray host"
            self._ready.set()
            self._emit("tray_unavailable", self.detail, "warning")
            return
        hwnd = user32.CreateWindowExW(0, class_name, "VideoHaul tray", 0, 0, 0, 0, 0, wintypes.HWND(-3), None, instance, None)
        if not hwnd:
            user32.UnregisterClassW(class_name, instance)
            self.detail = "Windows could not create the tray host"
            self._ready.set()
            self._emit("tray_unavailable", self.detail, "warning")
            return
        notify.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        notify.hWnd = hwnd
        notify.uID = 1
        notify.uFlags = 0x1 | 0x2 | 0x4
        notify.uCallbackMessage = callback_message
        notify.hIcon = user32.LoadIconW(None, 32512)
        notify.szTip = "VideoHaul"
        added = bool(shell32.Shell_NotifyIconW(0, ctypes.byref(notify)))
        with self._lock:
            self._hwnd = hwnd
            self.available = added
            self.detail = "VideoHaul tray icon is active" if added else "Windows rejected the tray icon"
        self._ready.set()
        self._emit("tray_attached" if added else "tray_unavailable", self.detail, "info" if added else "warning")
        if added:
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        else:
            user32.DestroyWindow(hwnd)
        user32.UnregisterClassW(class_name, instance)
        with self._lock:
            self._hwnd = None
            self.available = False


def native_window_handle(window) -> int | None:
    """Resolve a pywebview native handle across supported Windows renderers."""

    candidates = [window]
    for name in ("native", "gui", "form", "NativeWindow"):
        value = getattr(window, name, None)
        if value is not None:
            candidates.append(value)
    for candidate in candidates:
        for name in ("native_handle", "handle", "Handle", "hwnd", "HWND"):
            value = getattr(candidate, name, None)
            try:
                if value is not None and int(value):
                    return int(value)
            except Exception:
                continue
    if IS_WINDOWS:
        try:
            import ctypes
            from ctypes import wintypes

            find_window = ctypes.windll.user32.FindWindowW
            find_window.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
            find_window.restype = wintypes.HWND
            value = find_window(None, "VideoHaul")
            return int(value) if value else None
        except Exception:
            return None
    return None
