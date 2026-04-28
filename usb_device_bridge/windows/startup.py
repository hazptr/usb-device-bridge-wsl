from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "usbipd-device-attach-manager"


def can_configure_run_at_logon() -> bool:
    """True for packaged Windows builds only (not dev / source runs)."""
    return sys.platform == "win32" and getattr(sys, "frozen", False)


def _process_image_path() -> Path:
    if sys.platform != "win32":
        return Path(sys.executable).resolve()
    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetModuleFileNameW(None, buf, len(buf))
    if n == 0:
        return Path(sys.executable).resolve()
    return Path(buf.value).resolve()


def _expected_run_value() -> str:
    """REG_SZ value for HKCU Run: quoted path to the running executable."""
    return subprocess.list2cmdline([str(_process_image_path())])


def is_run_at_logon_enabled() -> bool:
    if not can_configure_run_at_logon():
        return False
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ
        ) as k:
            val, _ = winreg.QueryValueEx(k, _VALUE_NAME)
    except FileNotFoundError:
        return False
    except OSError:
        return False
    return (val or "").strip() == _expected_run_value().strip()


def set_run_at_logon(enabled: bool) -> tuple[bool, str | None]:
    """
    Register or remove this app under HKCU ... Run (per-user sign-in startup).

    Does not require administrator rights. Returns (success, error message).
    """
    if not can_configure_run_at_logon():
        return False, "This option is only available for the installed Windows app."
    import winreg

    try:
        if enabled:
            cmd = _expected_run_value()
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                _RUN_KEY,
                0,
                winreg.KEY_SET_VALUE,
            ) as k:
                winreg.SetValueEx(k, _VALUE_NAME, 0, winreg.REG_SZ, cmd)
        else:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                _RUN_KEY,
                0,
                winreg.KEY_SET_VALUE,
            ) as k:
                try:
                    winreg.DeleteValue(k, _VALUE_NAME)
                except FileNotFoundError:
                    pass
        return True, None
    except OSError as e:
        return False, str(e)
