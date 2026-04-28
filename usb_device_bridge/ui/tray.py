from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Callable

import pystray
from PIL import Image

# Windows shell: pystray maps tray activation to WM_LBUTTONUP (single click).
# We patch so only WM_LBUTTONDBLCLK runs the default menu action (Show).
_WIN32_TRAY_NOTIFY_PATCHED = False
_WM_LBUTTONDBLCLK = 0x0203


def _ensure_win32_tray_double_click_activation() -> None:
    global _WIN32_TRAY_NOTIFY_PATCHED
    if sys.platform != "win32" or _WIN32_TRAY_NOTIFY_PATCHED:
        return
    try:
        from pystray._win32 import Icon as Win32TrayIcon
        from pystray._util import win32 as win32lib
    except ImportError:
        return

    _orig = Win32TrayIcon._on_notify

    def _on_notify(self, wparam, lparam):
        if lparam == _WM_LBUTTONDBLCLK:
            self()
            return
        if lparam == win32lib.WM_LBUTTONUP:
            return
        return _orig(self, wparam, lparam)

    Win32TrayIcon._on_notify = _on_notify
    _WIN32_TRAY_NOTIFY_PATCHED = True


class TrayManager:
    """
    Notification-area (system tray) icon with Show / Exit. The icon runs on a
    background thread; callbacks are invoked from that thread — use Flet
    `page.run_task` / `page.run_thread` in those callbacks.
    """

    def __init__(self, icon_path: Path, tooltip: str) -> None:
        self._icon_path = icon_path
        self._tooltip = tooltip
        self._icon: pystray.Icon | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._on_show: Callable[[], None] | None = None
        self._on_exit: Callable[[], None] = None

    def set_handlers(
        self, *, on_show: Callable[[], None], on_exit: Callable[[], None]
    ) -> None:
        self._on_show = on_show
        self._on_exit = on_exit

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            if not self._icon_path.is_file():
                return
            if self._on_show is None or self._on_exit is None:
                return

            _ensure_win32_tray_double_click_activation()

            image = Image.open(self._icon_path)

            def _call_show(icon, item) -> None:
                if self._on_show:
                    self._on_show()

            def _call_exit(icon, item) -> None:
                if self._on_exit:
                    self._on_exit()

            menu = pystray.Menu(
                pystray.MenuItem("Show", _call_show, default=True),
                pystray.MenuItem("Exit", _call_exit),
            )
            self._icon = pystray.Icon(
                "usbipd_wsl_attach",
                image,
                self._tooltip,
                menu,
            )
            self._thread = threading.Thread(
                target=self._icon.run,
                name="pystray",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            icon = self._icon
            self._icon = None
            self._thread = None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
