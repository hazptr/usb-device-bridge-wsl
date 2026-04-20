from __future__ import annotations

import atexit
import ctypes
import errno
import logging
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from usbipd_attach_manager.version_info import (
    get_app_version,
    is_dev_source_launch,
    version_is_newer,
)
from usbipd_attach_manager.windows_admin import is_windows_process_elevated

logger = logging.getLogger(__name__)

_WSAECONNREFUSED = 10061


def _is_connection_refused(exc: BaseException) -> bool:
    if isinstance(exc, ConnectionRefusedError):
        return True
    if isinstance(exc, OSError):
        if exc.errno == errno.ECONNREFUSED:
            return True
        if getattr(exc, "winerror", None) == _WSAECONNREFUSED:
            return True
    return False


# Session mutex + loopback IPC (version handshake, focus, yield-to-newer).
MUTEX_NAME = "Local\\UsbipdDeviceAttachManager_SingleInstance_v1"
FOCUS_PORT = 48291
# Pre-UAC probe: must finish in well under 1s so startup never "hangs" on localhost.
_OPTIONAL_PEER_BUDGET_S = 2.0
_OPTIONAL_CONNECT_TIMEOUT_S = 0.35
_OPTIONAL_IO_TIMEOUT_S = 0.75
# Elevated duplicate: must reach the other process; still cap total wall time.
_REQUIRED_PEER_BUDGET_S = 5.0
_REQUIRED_CONNECT_TIMEOUT_S = 1.0
_REQUIRED_IO_TIMEOUT_S = 1.25
_CONNECT_ATTEMPTS = 16
_CONNECT_DELAY_S = 0.08
_MUTEX_WAIT_AFTER_YIELD_S = 45.0
_MUTEX_POLL_S = 0.15

_kernel32 = None
if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.SetLastError.argtypes = [ctypes.c_uint32]
    _kernel32.SetLastError.restype = None
    _kernel32.CreateMutexW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_bool,
        ctypes.c_wchar_p,
    ]
    _kernel32.CreateMutexW.restype = ctypes.c_void_p
    _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    _kernel32.CloseHandle.restype = ctypes.c_bool
    _kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    _kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    _kernel32.OpenProcess.restype = ctypes.c_void_p
    _kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    _kernel32.TerminateProcess.restype = ctypes.c_bool
    _kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = ctypes.c_bool

_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_TERMINATE = 0x0001

# WaitForSingleObject return values (WinBase.h)
_WAIT_OBJECT_0 = 0x00000000
_WAIT_ABANDONED_0 = 0x00000080
_WAIT_TIMEOUT = 0x00000102

_mutex_handle: int | None = None
_server_sock: socket.socket | None = None
_focus_callback: Callable[[], None] | None = None
_yield_callback: Callable[[], None] | None = None
_focus_lock = threading.Lock()
_pending_focus = threading.Event()
_pending_yield = threading.Event()
_server_thread: threading.Thread | None = None


def _close_mutex() -> None:
    global _mutex_handle
    if sys.platform != "win32" or not _kernel32 or _mutex_handle is None:
        return
    try:
        _kernel32.CloseHandle(_mutex_handle)
    except Exception:
        pass
    _mutex_handle = None


def release_singleton_mutex_before_uac_if_needed() -> None:
    """
    The elevating launcher exits after ShellExecute; it must not hold the mutex
    across that handoff or the elevated child cannot take over the singleton.
    No-op when already elevated or when this process does not hold the mutex.
    """
    if sys.platform != "win32" or not _kernel32:
        return
    if is_windows_process_elevated():
        return
    if _mutex_handle is not None:
        _close_mutex()


def try_acquire_single_instance_mutex() -> bool:
    """
    Returns True if this process now owns the singleton mutex.
    Returns False if another instance already holds it.
    On non-Windows, always True (no mutex). On mutex API failure, logs and returns True.

    After CreateMutex, we always ``WaitForSingleObject(..., 0)``. If another process
    holds the mutex we get ``WAIT_TIMEOUT``. If the mutex is free or *abandoned*
    (previous owner terminated without releasing, e.g. force-kill during hung apt),
    we acquire it and return True so a new launch is not blocked forever.
    """
    global _mutex_handle
    if sys.platform != "win32" or not _kernel32:
        return True
    if _mutex_handle is not None:
        return True
    _kernel32.SetLastError(0)
    handle = _kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        logger.warning(
            "CreateMutexW failed (last_error=%s); continuing without single-instance guard",
            ctypes.get_last_error(),
        )
        return True

    wait = _kernel32.WaitForSingleObject(handle, 0)
    if wait in (_WAIT_OBJECT_0, _WAIT_ABANDONED_0):
        if wait == _WAIT_ABANDONED_0:
            logger.info(
                "Recovered abandoned singleton mutex (previous instance exited uncleanly)."
            )
        _mutex_handle = handle
        atexit.register(_close_mutex)
        return True
    if wait == _WAIT_TIMEOUT:
        _kernel32.CloseHandle(handle)
        return False
    logger.warning(
        "WaitForSingleObject(mutex) returned %s (last_error=%s); closing handle",
        wait,
        ctypes.get_last_error(),
    )
    _kernel32.CloseHandle(handle)
    return True


def _windows_listen_pids_for_port(port: int) -> list[int]:
    """PIDs with a TCP listener involving ``port`` (best-effort via netstat)."""
    try:
        r = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=_WIN_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    found: set[int] = set()
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        if "LISTENING" not in line.upper():
            continue
        local = parts[1]
        if ":" not in local:
            continue
        try:
            port_s = int(local.rsplit(":", 1)[-1])
        except ValueError:
            continue
        if port_s != port:
            continue
        try:
            found.add(int(parts[-1]))
        except ValueError:
            continue
    return list(found)


def _query_process_image_path_win(pid: int) -> str | None:
    if not _kernel32:
        return None
    h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(32768)
        size = ctypes.c_uint32(len(buf))
        ok = _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        if not ok:
            return None
        return buf.value
    finally:
        _kernel32.CloseHandle(h)


def _wmi_command_line_for_pid(pid: int) -> str | None:
    try:
        r = subprocess.run(
            [
                "wmic",
                "process",
                "where",
                f"ProcessId={pid}",
                "get",
                "CommandLine",
                "/format:list",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=_WIN_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for raw in (r.stdout or "").splitlines():
        line = raw.strip()
        if line.lower().startswith("commandline="):
            return line.split("=", 1)[1].strip() or None
    return None


def _pid_appears_to_be_this_app(pid: int) -> bool:
    exe = _query_process_image_path_win(pid)
    if not exe:
        return False
    try:
        if Path(exe).resolve() == Path(sys.executable).resolve():
            return True
    except OSError:
        pass
    low = exe.lower()
    if low.endswith("python.exe") or low.endswith("pythonw.exe"):
        cl = _wmi_command_line_for_pid(pid)
        if not cl:
            return False
        c = cl.lower()
        return (
            "usbipd_attach_manager" in c
            or "usbipd-attach" in c
            or "\\main.py" in cl.replace("/", "\\").lower()
        )
    return False


def _windows_terminate_process(pid: int) -> bool:
    if not _kernel32:
        return False
    h = _kernel32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not h:
        logger.warning(
            "OpenProcess(TERMINATE, pid=%s) failed (last_error=%s)",
            pid,
            ctypes.get_last_error(),
        )
        return False
    try:
        return bool(_kernel32.TerminateProcess(h, 1))
    finally:
        _kernel32.CloseHandle(h)


def _ipc_probe_hello_once(
    my_version: str, *, connect_s: float, io_s: float
) -> str:
    """
    Single HELLO/response probe. Returns a response line, '__refused__',
    '__timeout__', or '' if the peer closed without a line.
    """
    try:
        s = socket.create_connection(("127.0.0.1", FOCUS_PORT), timeout=connect_s)
    except OSError:
        return "__refused__"
    try:
        s.settimeout(io_s)
        dev = is_dev_source_launch()
        hello = f"HELLO\t{my_version}\tdev\n" if dev else f"HELLO\t{my_version}\n"
        s.sendall(hello.encode("utf-8"))
        resp = b""
        while b"\n" not in resp and len(resp) < 64:
            chunk = s.recv(64)
            if not chunk:
                break
            resp += chunk
        return resp.decode("utf-8", errors="replace").strip()
    except TimeoutError:
        return "__timeout__"
    finally:
        try:
            s.close()
        except OSError:
            pass


def _try_recover_defunct_ipc_listener(my_version: str) -> bool:
    """
    If our IPC port is held by this application but HELLO gets no valid reply,
    terminate that listener so a deadlocked instance is not left running.

    Returns True if a process was terminated (caller should retry the mutex).
    """
    if sys.platform != "win32" or not _kernel32:
        return False
    pids = _windows_listen_pids_for_port(FOCUS_PORT)
    if not pids:
        return False
    ours = [p for p in pids if _pid_appears_to_be_this_app(p)]
    for p in pids:
        if p not in ours:
            logger.warning(
                "Port %s is bound by pid=%s (not this app); leaving it alone.",
                FOCUS_PORT,
                p,
            )
    if not ours:
        return False

    reply = _ipc_probe_hello_once(my_version, connect_s=1.5, io_s=2.5)
    if reply == "__refused__":
        return False
    if reply in ("FOCUS", "YIELD"):
        return False
    if reply and reply not in ("__timeout__",):
        logger.debug(
            "IPC peer responded with %r; treating as healthy instance.", reply[:80]
        )
        return False

    for pid in ours:
        if _windows_terminate_process(pid):
            logger.warning(
                "Terminated defunct single-instance IPC (pid=%s); probe was %r.",
                pid,
                reply,
            )
            time.sleep(0.45)
            return True
    return False


def _send_line(conn: socket.socket, text: str) -> None:
    conn.sendall(f"{text}\n".encode("utf-8"))


def _schedule_focus() -> None:
    with _focus_lock:
        cb = _focus_callback
    if cb is not None:
        try:
            cb()
        except Exception:
            logger.exception("single-instance focus callback failed")
    else:
        _pending_focus.set()


def _schedule_yield() -> None:
    with _focus_lock:
        cb = _yield_callback
    if cb is not None:
        try:
            cb()
        except Exception:
            logger.exception("single-instance yield callback failed")
    else:
        _pending_yield.set()


def _handle_client_connection(conn: socket.socket) -> None:
    try:
        conn.settimeout(3.0)
        data = conn.recv(512)
        if not data:
            return
        first_line = data.split(b"\n", 1)[0].strip()
        # Legacy second instance: single 0x01 ping (no version, no response expected).
        if first_line == b"\x01":
            _send_line(conn, "FOCUS")
            _schedule_focus()
            return
        if first_line.startswith(b"HELLO\t"):
            parts = first_line.split(b"\t", 3)
            client_ver = (
                parts[1].decode("utf-8", errors="replace").strip()
                if len(parts) >= 2
                else ""
            )
            client_dev = (
                len(parts) >= 3 and parts[2].strip().lower() == b"dev"
            )
            mine = get_app_version()
            server_dev = is_dev_source_launch()
            # Dev runs from main.py / -m: always yield so the next launch replaces
            # this process without bumping the package version.
            if server_dev or client_dev or version_is_newer(client_ver, mine):
                if server_dev:
                    logger.info(
                        "Dev source instance yielding to incoming process (faster iteration)."
                    )
                elif client_dev:
                    logger.info(
                        "Yielding to incoming dev-flagged instance (handshake)."
                    )
                else:
                    logger.info(
                        "Newer instance reported (%s > %s); exiting after handshake.",
                        client_ver,
                        mine,
                    )
                _send_line(conn, "YIELD")
                conn.close()
                _schedule_yield()
                return
            _send_line(conn, "FOCUS")
            _schedule_focus()
            return
        _send_line(conn, "FOCUS")
        _schedule_focus()
    except Exception:
        logger.exception("single-instance connection handler failed")
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _server_loop() -> None:
    global _server_sock
    sock = _server_sock
    if sock is None:
        return
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            break
        try:
            t = threading.Thread(
                target=_handle_client_connection,
                args=(conn,),
                name="usbipd-attach-ipc",
                daemon=True,
            )
            t.start()
        except Exception:
            logger.exception("failed to spawn IPC handler thread")
            try:
                conn.close()
            except OSError:
                pass


def start_focus_server() -> None:
    """Listen for other instances; call after we own the singleton mutex."""
    global _server_sock, _server_thread
    if sys.platform != "win32":
        return
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", FOCUS_PORT))
        sock.listen(8)
    except OSError as e:
        logger.error("single-instance IPC server could not bind: %s", e)
        try:
            sock.close()
        except OSError:
            pass
        return
    _server_sock = sock
    _server_thread = threading.Thread(
        target=_server_loop,
        name="usbipd-attach-focus-server",
        daemon=True,
    )
    _server_thread.start()


def _wait_then_acquire_mutex() -> bool:
    """After the running instance exits, take the singleton mutex."""
    deadline = time.monotonic() + _MUTEX_WAIT_AFTER_YIELD_S
    while time.monotonic() < deadline:
        if try_acquire_single_instance_mutex():
            return True
        time.sleep(_MUTEX_POLL_S)
    return False


def _tcp_handshake(my_version: str, *, allow_missing_peer: bool) -> bool:
    """
    Talk to an already-running instance on the IPC port (if any).

    Returns True to continue startup as the eventual sole UI instance.
    Returns False to exit immediately (duplicate, or failed handshake when a peer
    was required).

    If allow_missing_peer is True (pre-UAC launcher), failing to connect means no
    app is listening yet — continue to elevation without taking the mutex here.
    """
    budget_s = _OPTIONAL_PEER_BUDGET_S if allow_missing_peer else _REQUIRED_PEER_BUDGET_S
    deadline = time.monotonic() + budget_s
    connect_timeout = (
        _OPTIONAL_CONNECT_TIMEOUT_S if allow_missing_peer else _REQUIRED_CONNECT_TIMEOUT_S
    )
    io_timeout = _OPTIONAL_IO_TIMEOUT_S if allow_missing_peer else _REQUIRED_IO_TIMEOUT_S

    def _remaining() -> float:
        return deadline - time.monotonic()

    for attempt in range(_CONNECT_ATTEMPTS):
        if _remaining() <= 0:
            break
        tcp_established = False
        try:
            # Shrink timeouts so we never burn the whole budget on one syscall.
            ct = max(0.05, min(connect_timeout, _remaining()))
            s = socket.create_connection(
                ("127.0.0.1", FOCUS_PORT),
                timeout=ct,
            )
            tcp_established = True
            try:
                it = max(0.05, min(io_timeout, _remaining()))
                s.settimeout(it)
                dev = is_dev_source_launch()
                hello = f"HELLO\t{my_version}\tdev\n" if dev else f"HELLO\t{my_version}\n"
                s.sendall(hello.encode("utf-8"))
                resp = b""
                while b"\n" not in resp and len(resp) < 64:
                    chunk = s.recv(64)
                    if not chunk:
                        break
                    resp += chunk
                line = resp.decode("utf-8", errors="replace").strip()
                if line == "YIELD":
                    logger.info(
                        "Waiting for the previous instance to exit so this version can run."
                    )
                    if _wait_then_acquire_mutex():
                        return True
                    logger.error(
                        "Timed out waiting for the previous instance to release the singleton "
                        "lock (waited %ss). If it is stuck, end the process in Task Manager.",
                        int(_MUTEX_WAIT_AFTER_YIELD_S),
                    )
                    return False
                if line == "FOCUS":
                    logger.info(
                        "Another instance is already running (same or newer); "
                        "bringing it to the foreground."
                    )
                    return False
                logger.info(
                    "Connected to a running instance; assuming foreground handoff (legacy IPC)."
                )
                return False
            finally:
                try:
                    s.close()
                except OSError:
                    pass
        except TimeoutError:
            # Only treat as cold start if we never got a TCP connection.
            if allow_missing_peer and not tcp_established:
                logger.debug(
                    "IPC probe timed out (no responsive single-instance peer); continuing startup."
                )
                return True
            if tcp_established:
                logger.warning(
                    "IPC handshake timed out after connect; treating as duplicate instance."
                )
                return False
            if attempt + 1 < _CONNECT_ATTEMPTS and _remaining() > _CONNECT_DELAY_S:
                time.sleep(min(_CONNECT_DELAY_S, _remaining()))
        except OSError as e:
            if allow_missing_peer and _is_connection_refused(e):
                return True
            if allow_missing_peer:
                # Anything else (routing/firewall oddities): do not block cold start.
                logger.debug(
                    "IPC probe could not connect (%s); continuing startup.", e
                )
                return True
            if attempt + 1 < _CONNECT_ATTEMPTS and _remaining() > _CONNECT_DELAY_S:
                time.sleep(min(_CONNECT_DELAY_S, _remaining()))
    if allow_missing_peer:
        return True
    logger.warning(
        "Could not reach the running instance on the IPC port; exiting as duplicate."
    )
    return False


def prepare_single_instance() -> bool:
    """
    Returns True to continue startup as the owning UI instance.
    Returns False to exit (duplicate / focus-only).

    Order matters on Windows:
    - Before UAC, we only use TCP (no mutex) so the elevating launcher does not
      hold the mutex across ShellExecute (avoids racing the elevated child).
    - After elevation, we take the mutex and use the same TCP protocol when it
      is already held by another elevated process.
    """
    if sys.platform != "win32" or not _kernel32:
        return True
    my_v = get_app_version()
    if not is_windows_process_elevated():
        return _tcp_handshake(my_v, allow_missing_peer=True)
    if try_acquire_single_instance_mutex():
        return True
    if _try_recover_defunct_ipc_listener(my_v):
        if try_acquire_single_instance_mutex():
            logger.info(
                "Acquired singleton mutex after clearing a defunct IPC listener."
            )
            return True
    return _tcp_handshake(my_v, allow_missing_peer=False)


def set_focus_handler(callback: Callable[[], None]) -> None:
    """Register how to bring the main window forward (e.g. page.run_task(show_from_tray))."""
    global _focus_callback
    with _focus_lock:
        _focus_callback = callback
    if _pending_focus.is_set():
        _pending_focus.clear()
        try:
            callback()
        except Exception:
            logger.exception("single-instance focus callback failed on registration")


def set_yield_handler(callback: Callable[[], None]) -> None:
    """Register how to shut down so a newer instance can replace this process."""
    global _yield_callback
    with _focus_lock:
        _yield_callback = callback
    if _pending_yield.is_set():
        _pending_yield.clear()
        try:
            callback()
        except Exception:
            logger.exception("single-instance yield callback failed on registration")
