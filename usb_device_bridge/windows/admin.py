from __future__ import annotations

import ctypes
import subprocess
import sys
from pathlib import Path


def _windows_is_elevated() -> bool:
    if sys.platform != "win32":
        return True
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def is_windows_process_elevated() -> bool:
    """True if this process is running elevated (Administrator). Always True on non-Windows."""
    return _windows_is_elevated()


def _gui_python_executable() -> str:
    """
    Prefer pythonw.exe next to python.exe so the elevated GUI does not allocate a
    visible console (python.exe is a console subsystem binary).
    """
    exe = Path(sys.executable)
    low = exe.name.lower()
    if low == "python.exe":
        w = exe.with_name("pythonw.exe")
        if w.is_file():
            return str(w)
    if low == "python3.exe":
        w = exe.with_name("pythonw3.exe")
        if w.is_file():
            return str(w)
    return str(exe)


def _elevate_shell_execute_file() -> str:
    """
    Binary passed to ShellExecute 'runas' (this is what UAC labels).

    When packaged (e.g. PyInstaller), sys.executable is our app .exe with a
    Windows version resource. When running from source, it remains pythonw/python.
    """
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve())
    return _gui_python_executable()


def _elevated_launch_params() -> str:
    """Build the argument string for the Python/app binary when re-launching with UAC."""
    entry = Path(sys.argv[0]).resolve()
    if entry.is_file() and entry.name == "main.py":
        return subprocess.list2cmdline([str(entry), *sys.argv[1:]])
    if entry.is_file() and entry.name == "__main__.py":
        return subprocess.list2cmdline(
            ["-m", "usbipd_attach_manager", *sys.argv[1:]]
        )
    fallback = Path(__file__).resolve().parent.parent.parent / "main.py"
    if fallback.is_file():
        return subprocess.list2cmdline([str(fallback), *sys.argv[1:]])
    return subprocess.list2cmdline([str(entry), *sys.argv[1:]])


def ensure_administrator_windows() -> None:
    """
    On Windows, usbipd bind/attach needs elevation. If we are not running as
    administrator, re-launch with the 'runas' verb (UAC) and exit.
    """
    if sys.platform != "win32":
        return
    if _windows_is_elevated():
        return
    params = _elevated_launch_params()
    entry = Path(sys.argv[0]).resolve()
    if entry.is_file() and entry.name == "__main__.py":
        cwd = str(Path(__file__).resolve().parent.parent.parent)
    else:
        cwd = str(entry.parent)
    ret = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        _elevate_shell_execute_file(),
        params,
        cwd,
        1,
    )
    if int(ret) <= 32:
        msg = (
            "Administrator approval is required to manage USB devices with usbipd.\n"
        )
        err = sys.stderr
        if err is not None:
            err.write(msg)
        sys.exit(1)
    sys.exit(0)
