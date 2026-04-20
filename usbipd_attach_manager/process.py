from __future__ import annotations

import asyncio
import codecs
import subprocess
import sys
import threading
from collections.abc import Callable

_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def kill_pid_tree(pid: int | None) -> None:
    """Force-kill a process tree (Windows: ``taskkill /T``; else SIGTERM on the pid)."""
    if pid is None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                creationflags=_WIN_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        import os
        import signal

        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass


def _run_cmd_with_thread_cancel(
    exe: str,
    args: list[str],
    *,
    cancel_ev: threading.Event,
    timeout: float,
) -> tuple[int, str, str]:
    """Same pipe handling as ``run_cmd`` (``communicate``), with cancel via ``kill_pid_tree``."""
    proc = subprocess.Popen(
        [exe, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_WIN_NO_WINDOW if sys.platform == "win32" else 0,
    )
    stop_killer = threading.Event()

    def killer_loop() -> None:
        while True:
            if cancel_ev.is_set():
                kill_pid_tree(proc.pid)
                return
            if stop_killer.wait(timeout=0.25):
                break
        if cancel_ev.is_set():
            kill_pid_tree(proc.pid)

    killer = threading.Thread(target=killer_loop, daemon=True)
    killer.start()
    try:
        try:
            out_b, err_b = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_pid_tree(proc.pid)
            try:
                proc.wait(timeout=20)
            except (OSError, subprocess.TimeoutExpired):
                pass
            return -1, "", "Command timed out."
    finally:
        stop_killer.set()
        killer.join(timeout=3.0)

    if cancel_ev.is_set():
        return -1, "", "Cancelled."

    out = (out_b or b"").decode("utf-8", errors="replace").strip()
    err = (err_b or b"").decode("utf-8", errors="replace").strip()
    code = proc.returncode if proc.returncode is not None else -1
    return code, out, err


async def run_executable_cancellable(
    exe: str,
    args: list[str],
    *,
    cancel_event: asyncio.Event | None,
    timeout: float,
) -> tuple[int, str, str]:
    """Run ``exe`` with ``args``; cancel via ``cancel_event`` or kill the whole tree.

    Uses a worker thread + ``subprocess.Popen.communicate`` (same as ``run_cmd``) so
    Windows usbipd/wsl child processes behave like the non-cancellable path. Asyncio's
    subprocess pipe handling was unreliable here.
    """
    if cancel_event is None:
        return await asyncio.to_thread(run_cmd, exe, args, timeout=timeout)

    cancel_thread_ev = threading.Event()

    async def _forward_cancel() -> None:
        await cancel_event.wait()
        cancel_thread_ev.set()

    fwd = asyncio.create_task(_forward_cancel())
    try:
        return await asyncio.to_thread(
            _run_cmd_with_thread_cancel,
            exe,
            args,
            cancel_ev=cancel_thread_ev,
            timeout=timeout,
        )
    finally:
        fwd.cancel()
        try:
            await fwd
        except asyncio.CancelledError:
            pass


def run_cmd(
    exe: str, args: list[str], *, timeout: float = 120
) -> tuple[int, str, str]:
    try:
        r = subprocess.run(
            [exe, *args],
            capture_output=True,
            timeout=timeout,
            creationflags=_WIN_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except OSError as e:
        return 1, "", str(e)
    except subprocess.TimeoutExpired:
        return 1, "", "Command timed out."
    out = (r.stdout or b"").decode("utf-8", errors="replace").strip()
    err = (r.stderr or b"").decode("utf-8", errors="replace").strip()
    return r.returncode, out, err


async def run_cmd_async(
    exe: str, args: list[str], *, timeout: float = 120
) -> tuple[int, str, str]:
    return await asyncio.to_thread(run_cmd, exe, args, timeout=timeout)


async def run_cmd_stream_merged_async(
    exe: str,
    args: list[str],
    *,
    on_text: Callable[[str], None],
    timeout: float = 900.0,
    cancel_event: asyncio.Event | None = None,
) -> tuple[int, str]:
    """
    Run ``exe`` with ``args``; merge **stderr into stdout** (``subprocess.STDOUT``)
    so WinGet/apt messages on stderr appear in the same stream as the UI log.

    UTF-8 text is sent to ``on_text`` in chunks (piped children may still buffer).

    When ``cancel_event`` is set, the process tree is killed and the function returns
    ``(-2, log + "\\n[Cancelled.]")``.

    Returns ``(returncode, full_log)``.
    """
    kwargs: dict[str, object] = {
        "stdin": asyncio.subprocess.DEVNULL,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.STDOUT,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = _WIN_NO_WINDOW

    proc = await asyncio.create_subprocess_exec(exe, *args, **kwargs)
    log_parts: list[str] = []
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    cancelled_flag = False

    async def _pump() -> None:
        nonlocal cancelled_flag
        assert proc.stdout is not None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                cancelled_flag = True
                kill_pid_tree(proc.pid)
                break

            if cancel_event is not None:
                read_task = asyncio.create_task(proc.stdout.read(8192))
                cancel_task = asyncio.create_task(cancel_event.wait())
                done, pending = await asyncio.wait(
                    {read_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for p in pending:
                    p.cancel()
                if cancel_task in done:
                    cancelled_flag = True
                    if not read_task.done():
                        read_task.cancel()
                        try:
                            await read_task
                        except asyncio.CancelledError:
                            pass
                    kill_pid_tree(proc.pid)
                    break
                try:
                    await cancel_task
                except asyncio.CancelledError:
                    pass
                raw = read_task.result()
            else:
                raw = await proc.stdout.read(8192)

            if not raw:
                break
            chunk = decoder.decode(raw)
            if chunk:
                log_parts.append(chunk)
                on_text(chunk)
        if not cancelled_flag:
            tail = decoder.decode(b"", final=True)
            if tail:
                log_parts.append(tail)
                on_text(tail)

    try:
        await asyncio.wait_for(_pump(), timeout=timeout)
    except asyncio.TimeoutError:
        kill_pid_tree(proc.pid)
        try:
            await asyncio.wait_for(proc.wait(), timeout=30.0)
        except (asyncio.TimeoutError, OSError):
            pass
        return -1, "".join(log_parts) + "\n[Timed out.]"

    if cancelled_flag:
        tail = decoder.decode(b"", final=True)
        if tail:
            log_parts.append(tail)
            on_text(tail)
        suffix = "\n[Cancelled.]"
        log_parts.append(suffix)
        on_text(suffix)
        try:
            await asyncio.wait_for(proc.wait(), timeout=30.0)
        except (asyncio.TimeoutError, OSError):
            pass
        return -2, "".join(log_parts)

    code = await proc.wait()
    rc = code if code is not None else -1
    return rc, "".join(log_parts)
