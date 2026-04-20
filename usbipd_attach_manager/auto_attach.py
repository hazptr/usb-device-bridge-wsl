"""Background attachment workers for remembered devices.

Product intent: see ``AGENTS.md`` (section “Remembered devices”).

Remembered devices use **usbipd’s built-in auto-attach** (``usbipd attach … -a``), one
long-running subprocess per device while attach is still in progress. ``sync`` is
called on the UI poll timer to start/stop those helpers as plug state and BusId
change, and to **stop** the helper when the device is already attached (so we do
not respawn in a tight loop). Exits are logged; respawns use a short backoff.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

from usbipd_attach_manager.process import run_cmd
from usbipd_attach_manager.usbipd import classify

_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

_log = logging.getLogger(__name__)

# Minimum seconds between spawning a new ``attach -a`` for the same instance after
# the previous process exited (avoids log/UI thrash if usbipd exits quickly on error).
_MIN_RESPAWN_INTERVAL_SEC = 2.0


def _terminate_process(proc: subprocess.Popen[Any], detail: str) -> None:
    """Stop a background ``usbipd attach`` process and any child processes.

    On Windows, ``usbipd`` may spawn ``wsl.exe`` / conhost under the tool process.
    ``Popen.terminate()`` only ends the top-level PID, which can leave those
    children behind; ``taskkill /T`` tears down the whole tree.
    """
    if proc.poll() is not None:
        _log.info(
            "Auto-attach subtask already exited (pid was %s): %s",
            proc.pid,
            detail,
        )
        return
    pid = proc.pid
    _log.info(
        "Auto-attach subtask stopping process tree (pid=%s): %s",
        pid,
        detail,
    )
    if sys.platform == "win32" and pid is not None:
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
        try:
            proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
        _log.info("Auto-attach subtask cleanup finished (pid=%s)", pid)
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except OSError:
            pass
    _log.info("Auto-attach subtask cleanup finished (pid=%s)", pid)


def _spawn_auto_attach(usbipd: str, distro: str, bus_id: str) -> subprocess.Popen[Any]:
    """Start usbipd **auto-attach** mode (``-a``) for a remembered device row."""
    args = [usbipd, "attach", "--wsl", distro, "-b", bus_id, "-a"]
    proc = subprocess.Popen(
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_WIN_NO_WINDOW if sys.platform == "win32" else 0,
    )
    _log.info(
        "usbipd attach -a started (pid=%s, distro=%s, bus_id=%s)",
        proc.pid,
        distro,
        bus_id,
    )
    return proc


class AutoAttachManager:
    """Runs ``usbipd attach -a`` helpers per remembered device (with bind when needed)."""

    def __init__(self) -> None:
        self._procs: dict[str, tuple[subprocess.Popen[Any], str]] = {}
        self._last_spawn_mono: dict[str, float] = {}

    def running_count(self) -> int:
        return sum(
            1
            for p, _ in self._procs.values()
            if p.poll() is None
        )

    def terminate_all(self) -> None:
        keys = list(self._procs.keys())
        if keys:
            _log.info(
                "Auto-attach cleanup: stopping %d background subtask(s)",
                len(keys),
            )
        for inst in keys:
            self._terminate_one(inst, "terminate_all")

    def _terminate_one(self, instance_id: str, reason: str) -> None:
        self._last_spawn_mono.pop(instance_id, None)
        t = self._procs.pop(instance_id, None)
        if t:
            proc, bus_id = t
            _log.info(
                "Auto-attach subtask cleanup: instance_id=%s bus_id=%s reason=%s",
                instance_id,
                bus_id,
                reason,
            )
            _terminate_process(
                proc,
                f"instance_id={instance_id}, bus_id={bus_id}, reason={reason}",
            )

    def cancel_background_attach(self, instance_id: str) -> None:
        """Stop the background ``usbipd attach -a`` subtask for one device."""
        self._terminate_one(instance_id, "user_cancel")

    def sync(
        self,
        usbipd: str,
        remember_ids: set[str],
        devs: list[dict[str, Any]],
        distro_for_instance: Callable[[str], str | None],
    ) -> None:
        if not remember_ids:
            self.terminate_all()
            return

        by_inst = {
            d["InstanceId"]: d
            for d in devs
            if d.get("InstanceId")
        }

        for inst in list(self._procs.keys()):
            if inst not in remember_ids:
                self._terminate_one(inst, "no longer remembered")

        for inst in remember_ids:
            dev = by_inst.get(inst)
            if not dev:
                self._terminate_one(inst, "missing from usbipd state")
                continue

            distro = distro_for_instance(inst)
            if not distro:
                self._terminate_one(inst, "no distro for device")
                continue

            bid = dev.get("BusId")
            st = classify(dev)
            if not bid:
                self._terminate_one(inst, "no BusId")
                continue

            if st == "offline":
                self._terminate_one(inst, "device offline")
                continue

            if st == "attached":
                # Connected — stop the ``-a`` helper; do not spawn another.
                self._terminate_one(inst, "already attached to client")
                continue

            if st in ("available", "shared"):
                self._ensure_running(
                    usbipd,
                    distro,
                    inst,
                    bid,
                    need_bind=(st == "available"),
                )
                continue

            self._terminate_one(inst, f"unexpected state: {st}")

    def _ensure_running(
        self,
        usbipd: str,
        distro: str,
        instance_id: str,
        bus_id: str,
        *,
        need_bind: bool,
    ) -> None:
        if instance_id in self._procs:
            proc, old_bid = self._procs[instance_id]
            if old_bid != bus_id:
                self._terminate_one(instance_id, f"BusId changed ({old_bid!r} -> {bus_id!r})")
            elif proc.poll() is None:
                return
            else:
                exit_code = proc.poll()
                _log.info(
                    "usbipd attach -a process ended (exit=%s, instance_id=%s bus_id=%s)",
                    exit_code,
                    instance_id,
                    bus_id,
                )
                del self._procs[instance_id]

        now = time.monotonic()
        last = self._last_spawn_mono.get(instance_id, 0.0)
        if now - last < _MIN_RESPAWN_INTERVAL_SEC:
            _log.debug(
                "auto-attach respawn skipped (min_interval=%.1fs) instance_id=%s",
                _MIN_RESPAWN_INTERVAL_SEC,
                instance_id,
            )
            return

        if need_bind:
            bcode, _bout, berr = run_cmd(usbipd, ["bind", "-b", bus_id], timeout=120)
            if bcode != 0:
                _log.warning(
                    "Auto-attach bind failed (instance_id=%s bus_id=%s): %s",
                    instance_id,
                    bus_id,
                    berr or _bout or "bind failed",
                )
                return

        proc = _spawn_auto_attach(usbipd, distro, bus_id)
        self._procs[instance_id] = (proc, bus_id)
        self._last_spawn_mono[instance_id] = time.monotonic()

    def instance_ids_attaching(
        self,
        devs: list[dict[str, Any]],
        remember_ids: set[str],
    ) -> set[str]:
        """Devices with a live ``usbipd attach -a`` subprocess that is not yet attached."""
        out: set[str] = set()
        by_inst = {
            d["InstanceId"]: d for d in devs if d.get("InstanceId")
        }
        for inst in remember_ids:
            pair = self._procs.get(inst)
            if not pair:
                continue
            proc, _bus = pair
            if proc.poll() is not None:
                continue
            dev = by_inst.get(inst)
            if dev is None:
                continue
            if classify(dev) != "attached":
                out.add(inst)
        return out
