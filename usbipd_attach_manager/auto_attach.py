"""Background attachment workers for remembered devices.

Product intent: see ``AGENTS.md`` (section “Remembered devices”).

Remembered devices use **usbipd’s built-in auto-attach** (``usbipd attach … -a``), one
long-running subprocess per device while attach is still in progress. ``usbipd`` keeps
that listener alive across unplug/replug at the same BusId; ``sync`` therefore **does
not** stop a live listener just because the device row disappears or is transiently
offline. It stops the helper when the device is already attached, when the BusId for
that instance changes (new listener needed), on user cancel / shutdown, or when a
listener exceeds a wall-clock safety timeout. Dead listeners are reaped so a new one
can be spawned when the device is connectable again. Respawns use a short backoff.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

from usbipd_attach_manager.process import run_cmd, run_cmd_stream_merged_async
from usbipd_attach_manager.firewall import (
    apply_wsl_public_profile_firewall_fix,
    usbipd_output_suggests_firewall_block,
)
from usbipd_attach_manager.usbipd import classify

_WIN_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

_log = logging.getLogger(__name__)

# Minimum seconds between spawning a new ``attach -a`` for the same instance after
# the previous process exited (avoids log/UI thrash if usbipd exits quickly on error).
_MIN_RESPAWN_INTERVAL_SEC = 2.0

# Adaptive timeout/backoff when a listener stays alive but the device is still not
# attached: 10s, 20s, 40s, 80s, 160s, then cap at 300s.
_INITIAL_LISTENER_TIMEOUT_SEC = 10.0
_MAX_LISTENER_TIMEOUT_SEC = 300.0
_MAX_ATTACH_ATTEMPTS = 6
_LONG_WAIT_UI_THRESHOLD_SEC = 30.0
_ADOPT_SCAN_INTERVAL_SEC = 15.0
_DIRECT_ATTACH_TIMEOUT_SEC = 15.0
_FIREWALL_PROMPT_REQUEUE_COOLDOWN_SEC = 20.0


def _listener_timeout_for_attempt(attempt_no: int) -> float:
    clamped = max(1, min(attempt_no, _MAX_ATTACH_ATTEMPTS))
    return min(
        _INITIAL_LISTENER_TIMEOUT_SEC * (2 ** (clamped - 1)),
        _MAX_LISTENER_TIMEOUT_SEC,
    )


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=10,
                creationflags=_WIN_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        out = (r.stdout or "").strip()
        if not out:
            return False
        if "No tasks are running" in out:
            return False
        return f'"{pid}"' in out or (str(pid) in out and "usbipd" in out.lower())
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _terminate_pid_tree(pid: int, detail: str) -> None:
    _log.info("Auto-attach external listener stopping (pid=%s): %s", pid, detail)
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
        os.kill(pid, 15)
    except OSError:
        pass


def _parse_auto_attach_cmdline(cmdline: str) -> tuple[str | None, str] | None:
    if not cmdline:
        return None
    if not re.search(r"(?:^|\s)attach(?:\s|$)", cmdline, re.IGNORECASE):
        return None
    if not re.search(r"(?:^|\s)-a(?:\s|$)", cmdline, re.IGNORECASE):
        return None
    m_bus = re.search(
        r"(?:^|\s)(?:-b|--busid)(?:\s+|=)(?:\"([^\"]+)\"|([^\s]+))",
        cmdline,
        re.IGNORECASE,
    )
    if not m_bus:
        return None
    bus_id = (m_bus.group(1) or m_bus.group(2) or "").strip()
    if not bus_id:
        return None
    m_distro = re.search(
        r"(?:^|\s)--wsl(?:\s+|=)(?:\"([^\"]+)\"|([^\s]+))",
        cmdline,
        re.IGNORECASE,
    )
    distro = None
    if m_distro:
        distro = (m_distro.group(1) or m_distro.group(2) or "").strip() or None
    return distro, bus_id


def _list_existing_auto_attach_processes() -> list[tuple[int, str, str | None]]:
    if sys.platform != "win32":
        return []
    ps = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "Get-CimInstance Win32_Process -Filter \"Name='usbipd.exe'\" | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
            creationflags=_WIN_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    raw = (r.stdout or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    rows = parsed if isinstance(parsed, list) else [parsed]
    out: list[tuple[int, str, str | None]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            pid = int(row.get("ProcessId") or 0)
        except (TypeError, ValueError):
            continue
        cmdline = str(row.get("CommandLine") or "")
        parsed_cmd = _parse_auto_attach_cmdline(cmdline)
        if not parsed_cmd:
            continue
        distro, bus_id = parsed_cmd
        out.append((pid, bus_id, distro))
    return out


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

    def __init__(
        self,
        *,
        firewall_fix_policy_provider: Callable[[], str] | None = None,
    ) -> None:
        # ``start_mono`` is ``time.monotonic()`` when this listener was spawned.
        self._procs: dict[str, tuple[subprocess.Popen[Any], str, float]] = {}
        self._external_procs: dict[str, tuple[int, str, float]] = {}
        self._last_spawn_mono: dict[str, float] = {}
        self._last_adopt_scan_mono = 0.0
        self._attempt_no: dict[str, int] = {}
        self._failed: dict[str, str] = {}
        self._firewall_fix_attempted: set[str] = set()
        self._firewall_prompt_needed: dict[str, str] = {}
        self._firewall_prompt_last_mono: dict[str, float] = {}
        self._firewall_fix_policy_provider = firewall_fix_policy_provider

    def _reset_retry_state(self, instance_id: str) -> None:
        self._attempt_no.pop(instance_id, None)
        self._failed.pop(instance_id, None)
        self._firewall_fix_attempted.discard(instance_id)
        self._firewall_prompt_needed.pop(instance_id, None)
        self._firewall_prompt_last_mono.pop(instance_id, None)

    def consume_firewall_prompt_requests(self) -> list[tuple[str, str]]:
        """Return and clear pending firewall-consent prompts from auto-attach failures."""
        items = sorted(self._firewall_prompt_needed.items())
        self._firewall_prompt_needed.clear()
        return items

    def failed_instance_ids(self) -> set[str]:
        return set(self._failed.keys())

    def failure_for_instance(self, instance_id: str) -> str | None:
        return self._failed.get(instance_id)

    def running_count(self) -> int:
        own = sum(1 for p, *_rest in self._procs.values() if p.poll() is None)
        ext = sum(1 for pid, *_rest in self._external_procs.values() if _pid_is_running(pid))
        return own + ext

    def terminate_all(self) -> None:
        keys = sorted(set(self._procs.keys()) | set(self._external_procs.keys()))
        if keys:
            _log.info(
                "Auto-attach cleanup: stopping %d background subtask(s)",
                len(keys),
            )
        for inst in keys:
            self._terminate_one(inst, "terminate_all")

    def _terminate_one(
        self,
        instance_id: str,
        reason: str,
        *,
        clear_retry_state: bool = True,
    ) -> None:
        self._last_spawn_mono.pop(instance_id, None)
        if clear_retry_state:
            self._reset_retry_state(instance_id)
        t = self._procs.pop(instance_id, None)
        if t:
            proc, bus_id, _started = t
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
        ext = self._external_procs.pop(instance_id, None)
        if ext:
            pid, bus_id, _started = ext
            _terminate_pid_tree(
                pid,
                f"instance_id={instance_id}, bus_id={bus_id}, reason={reason}",
            )

    def cancel_background_attach(self, instance_id: str) -> None:
        """Stop the background ``usbipd attach -a`` subtask for one device."""
        self._terminate_one(instance_id, "user_cancel")

    def retry_background_attach(self, instance_id: str) -> None:
        """Force-restart background auto-attach and clear failure/retry state."""
        self._reset_retry_state(instance_id)
        t = self._procs.get(instance_id)
        ext = self._external_procs.get(instance_id)
        if t or ext:
            self._terminate_one(
                instance_id,
                "user_retry",
                clear_retry_state=False,
            )
        else:
            self._last_spawn_mono.pop(instance_id, None)

    def _reap_if_dead(self, instance_id: str, reason: str) -> None:
        """Remove a finished listener from bookkeeping (process already exited)."""
        t = self._procs.get(instance_id)
        if t:
            proc, bus_id, _started = t
            if proc.poll() is None:
                return
            _log.info(
                "Auto-attach listener exited (pid was %s), reaping: instance_id=%s "
                "bus_id=%s reason=%s",
                proc.pid,
                instance_id,
                bus_id,
                reason,
            )
            self._procs.pop(instance_id, None)
            return
        ext = self._external_procs.get(instance_id)
        if not ext:
            return
        pid, bus_id, _started = ext
        if _pid_is_running(pid):
            return
        _log.info(
            "Adopted auto-attach listener exited (pid was %s), reaping: "
            "instance_id=%s bus_id=%s reason=%s",
            pid,
            instance_id,
            bus_id,
            reason,
        )
        self._external_procs.pop(instance_id, None)

    def _adopt_existing_listeners(
        self,
        remember_ids: set[str],
        by_inst: dict[str, dict[str, Any]],
        distro_for_instance: Callable[[str], str | None],
    ) -> None:
        inst_for_bus: dict[str, str] = {}
        for inst in remember_ids:
            dev = by_inst.get(inst)
            if not dev:
                continue
            bus_id = (dev.get("BusId") or "").strip()
            if bus_id:
                inst_for_bus[bus_id] = inst
        if not inst_for_bus:
            return
        for pid, bus_id, distro in _list_existing_auto_attach_processes():
            inst = inst_for_bus.get(bus_id)
            if not inst:
                continue
            if inst in self._procs or inst in self._external_procs:
                continue
            expected_distro = distro_for_instance(inst)
            if expected_distro and distro and expected_distro.lower() != distro.lower():
                _log.info(
                    "Skipping adopt for instance_id=%s bus_id=%s due to distro mismatch "
                    "(running=%r expected=%r)",
                    inst,
                    bus_id,
                    distro,
                    expected_distro,
                )
                continue
            self._external_procs[inst] = (pid, bus_id, time.monotonic())
            self._attempt_no.setdefault(inst, 1)
            _log.info(
                "Adopted existing usbipd attach -a listener "
                "(pid=%s, instance_id=%s, bus_id=%s)",
                pid,
                inst,
                bus_id,
            )

    def _try_direct_attach_once(
        self,
        usbipd: str,
        distro: str,
        instance_id: str,
        bus_id: str,
    ) -> bool:
        def _run_attach() -> tuple[int, str, str]:
            # Stream merged stdout/stderr so we can react as soon as usbipd emits
            # firewall-signature text, instead of waiting until full timeout.
            cancel_event = asyncio.Event()

            def _chunk_looks_like_firewall_block(chunk: str) -> bool:
                t = chunk.lower()
                markers = (
                    "timed out",
                    "firewall",
                    "3240",
                    "group policy",
                    "public network profile",
                    "blocking the connection",
                )
                return any(m in t for m in markers)

            def _on_text(chunk: str) -> None:
                if cancel_event.is_set():
                    return
                if _chunk_looks_like_firewall_block(chunk):
                    cancel_event.set()

            try:
                code, merged = asyncio.run(
                    run_cmd_stream_merged_async(
                        usbipd,
                        ["attach", "--wsl", distro, "-b", bus_id],
                        on_text=_on_text,
                        timeout=_DIRECT_ATTACH_TIMEOUT_SEC,
                        cancel_event=cancel_event,
                    )
                )
            except Exception:
                return run_cmd(
                    usbipd,
                    ["attach", "--wsl", distro, "-b", bus_id],
                    timeout=_DIRECT_ATTACH_TIMEOUT_SEC,
                )

            text = (merged or "").strip()
            if code == 0:
                return 0, text, ""
            if code == -2 and cancel_event.is_set():
                # Early-cancelled because we already saw a firewall signature.
                return 1, "", text or "Command timed out."
            if code == -1:
                if text:
                    return 1, "", f"Command timed out.\n{text}"
                return 1, "", "Command timed out."
            return code, text, ""

        code, out, err = _run_attach()
        msg = err or out or "attach failed"
        if code == 0:
            _log.info(
                "Direct attach fallback succeeded (instance_id=%s bus_id=%s)",
                instance_id,
                bus_id,
            )
            return True
        firewall_like = usbipd_output_suggests_firewall_block(msg)
        if firewall_like:
            policy = "ask"
            if self._firewall_fix_policy_provider is not None:
                try:
                    policy = self._firewall_fix_policy_provider()
                except Exception:  # pragma: no cover - defensive policy callback
                    policy = "ask"
            if policy == "never":
                self._firewall_fix_attempted.add(instance_id)
                _log.info(
                    "Direct attach fallback appears blocked by firewall policy but saved "
                    "setting forbids automatic changes (instance_id=%s bus_id=%s)",
                    instance_id,
                    bus_id,
                )
                return False
            if policy != "always":
                self._firewall_fix_attempted.add(instance_id)
                now = time.monotonic()
                last = self._firewall_prompt_last_mono.get(instance_id, 0.0)
                if (
                    instance_id not in self._firewall_prompt_needed
                    and now - last >= _FIREWALL_PROMPT_REQUEUE_COOLDOWN_SEC
                ):
                    self._firewall_prompt_needed[instance_id] = msg
                    self._firewall_prompt_last_mono[instance_id] = now
                    _log.warning(
                        "Direct attach fallback appears blocked by firewall policy; "
                        "queued user-consent prompt (instance_id=%s bus_id=%s policy=%s)",
                        instance_id,
                        bus_id,
                        policy,
                    )
                return False
            if instance_id in self._firewall_fix_attempted:
                _log.warning(
                    "Direct attach fallback still shows firewall signature after prior "
                    "auto-fix attempt (instance_id=%s bus_id=%s)",
                    instance_id,
                    bus_id,
                )
                return False
            self._firewall_fix_attempted.add(instance_id)
            _log.warning(
                "Direct attach fallback looks blocked by firewall policy; "
                "attempting automatic firewall fix (instance_id=%s bus_id=%s): %s",
                instance_id,
                bus_id,
                (msg[:597] + "...") if len(msg) > 600 else msg,
            )
            fix_ok, fix_err = apply_wsl_public_profile_firewall_fix()
            if fix_ok:
                code2, out2, err2 = _run_attach()
                if code2 == 0:
                    _log.info(
                        "Direct attach fallback succeeded after firewall fix "
                        "(instance_id=%s bus_id=%s)",
                        instance_id,
                        bus_id,
                    )
                    return True
                msg = err2 or out2 or "attach failed"
                _log.warning(
                    "Direct attach still failing after automatic firewall fix "
                    "(instance_id=%s bus_id=%s): %s",
                    instance_id,
                    bus_id,
                    msg,
                )
            else:
                _log.warning(
                    "Automatic firewall fix failed during direct attach fallback "
                    "(instance_id=%s bus_id=%s): %s",
                    instance_id,
                    bus_id,
                    fix_err or "Set-NetFirewallProfile failed",
                )
        _log.warning(
            "Direct attach fallback failed (instance_id=%s bus_id=%s): %s",
            instance_id,
            bus_id,
            msg,
        )
        return False

    def sync(
        self,
        usbipd: str,
        remember_ids: set[str],
        devs: list[dict[str, Any]],
        distro_for_instance: Callable[[str], str | None],
    ) -> None:
        if not remember_ids:
            self._last_adopt_scan_mono = 0.0
            self.terminate_all()
            return

        by_inst = {
            d["InstanceId"]: d
            for d in devs
            if d.get("InstanceId")
        }

        now = time.monotonic()
        if now - self._last_adopt_scan_mono >= _ADOPT_SCAN_INTERVAL_SEC:
            self._last_adopt_scan_mono = now
            self._adopt_existing_listeners(remember_ids, by_inst, distro_for_instance)

        for inst in list(set(self._procs.keys()) | set(self._external_procs.keys())):
            if inst not in remember_ids:
                self._terminate_one(inst, "no longer remembered")
        for inst in list(self._failed.keys()):
            if inst not in remember_ids:
                self._reset_retry_state(inst)

        for inst in remember_ids:
            distro = distro_for_instance(inst)
            if not distro:
                self._terminate_one(inst, "no distro for device")
                continue

            dev = by_inst.get(inst)
            if not dev:
                self._reap_if_dead(inst, "device missing from usbipd state")
                continue

            bid = dev.get("BusId")
            st = classify(dev)

            if st == "attached":
                # Connected — stop the ``-a`` helper; do not spawn another.
                self._terminate_one(inst, "already attached to client")
                continue

            if st in ("available", "shared") and bid:
                self._ensure_running(
                    usbipd,
                    distro,
                    inst,
                    bid,
                    need_bind=(st == "available"),
                )
                continue

            # Unplug / transient: keep a live ``-a`` listener; only reap if it exited.
            self._reap_if_dead(
                inst,
                f"waiting (state={st!r} bus_id={bid!r})",
            )

    def _ensure_running(
        self,
        usbipd: str,
        distro: str,
        instance_id: str,
        bus_id: str,
        *,
        need_bind: bool,
    ) -> None:
        if instance_id in self._failed:
            return

        if instance_id in self._external_procs:
            pid, old_bid, old_start = self._external_procs[instance_id]
            if old_bid != bus_id:
                self._terminate_one(
                    instance_id,
                    f"BusId changed ({old_bid!r} -> {bus_id!r})",
                )
            elif _pid_is_running(pid):
                now = time.monotonic()
                attempt_no = self._attempt_no.get(instance_id, 1)
                timeout_sec = _listener_timeout_for_attempt(attempt_no)
                if instance_id not in self._firewall_fix_attempted:
                    timeout_sec = min(timeout_sec, _INITIAL_LISTENER_TIMEOUT_SEC)
                elapsed_sec = now - old_start
                if elapsed_sec <= timeout_sec:
                    return

                if attempt_no >= _MAX_ATTACH_ATTEMPTS:
                    self._terminate_one(
                        instance_id,
                        (
                            "auto-attach failed after "
                            f"{_MAX_ATTACH_ATTEMPTS} attempts (timeout {timeout_sec:.0f}s)"
                        ),
                        clear_retry_state=False,
                    )
                    msg = (
                        "Auto-attach failed after multiple attempts. "
                        "Try reconnecting the device or toggle Remember off/on."
                    )
                    self._failed[instance_id] = msg
                    _log.error(
                        "Auto-attach giving up on adopted listener: "
                        "instance_id=%s bus_id=%s attempts=%s",
                        instance_id,
                        bus_id,
                        attempt_no,
                    )
                    return

                next_attempt = attempt_no + 1
                self._try_direct_attach_once(usbipd, distro, instance_id, bus_id)
                _log.warning(
                    "Auto-attach timeout on adopted listener: restarting "
                    "instance_id=%s bus_id=%s attempt=%s timeout=%.0fs next_attempt=%s",
                    instance_id,
                    bus_id,
                    attempt_no,
                    timeout_sec,
                    next_attempt,
                )
                self._terminate_one(
                    instance_id,
                    f"adopted attach -a timed out after {elapsed_sec:.1f}s",
                    clear_retry_state=False,
                )
                self._attempt_no[instance_id] = next_attempt
            else:
                _log.info(
                    "Adopted attach -a process ended (instance_id=%s bus_id=%s pid=%s)",
                    instance_id,
                    bus_id,
                    pid,
                )
                del self._external_procs[instance_id]

        if instance_id in self._procs:
            proc, old_bid, old_start = self._procs[instance_id]
            if old_bid != bus_id:
                self._terminate_one(
                    instance_id,
                    f"BusId changed ({old_bid!r} -> {bus_id!r})",
                )
            elif proc.poll() is None:
                now = time.monotonic()
                attempt_no = self._attempt_no.get(instance_id, 1)
                timeout_sec = _listener_timeout_for_attempt(attempt_no)
                if instance_id not in self._firewall_fix_attempted:
                    timeout_sec = min(timeout_sec, _INITIAL_LISTENER_TIMEOUT_SEC)
                elapsed_sec = now - old_start
                if elapsed_sec <= timeout_sec:
                    return

                if attempt_no >= _MAX_ATTACH_ATTEMPTS:
                    self._terminate_one(
                        instance_id,
                        (
                            "auto-attach failed after "
                            f"{_MAX_ATTACH_ATTEMPTS} attempts (timeout {timeout_sec:.0f}s)"
                        ),
                        clear_retry_state=False,
                    )
                    msg = (
                        "Auto-attach failed after multiple attempts. "
                        "Try reconnecting the device or toggle Remember off/on."
                    )
                    self._failed[instance_id] = msg
                    _log.error(
                        "Auto-attach giving up: instance_id=%s bus_id=%s attempts=%s",
                        instance_id,
                        bus_id,
                        attempt_no,
                    )
                    return

                next_attempt = attempt_no + 1
                self._try_direct_attach_once(usbipd, distro, instance_id, bus_id)
                _log.warning(
                    "Auto-attach timeout: restarting listener instance_id=%s bus_id=%s "
                    "attempt=%s timeout=%.0fs next_attempt=%s",
                    instance_id,
                    bus_id,
                    attempt_no,
                    timeout_sec,
                    next_attempt,
                )
                self._terminate_one(
                    instance_id,
                    f"attach -a timed out after {elapsed_sec:.1f}s",
                    clear_retry_state=False,
                )
                self._attempt_no[instance_id] = next_attempt
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

        self._attempt_no.setdefault(instance_id, 1)
        proc = _spawn_auto_attach(usbipd, distro, bus_id)
        now = time.monotonic()
        self._procs[instance_id] = (proc, bus_id, now)
        self._last_spawn_mono[instance_id] = now

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
            live = False
            pair = self._procs.get(inst)
            if pair:
                proc, _bus, _started = pair
                live = proc.poll() is None
            else:
                ext = self._external_procs.get(inst)
                if ext:
                    pid, _bus, _started = ext
                    live = _pid_is_running(pid)
            if not live:
                continue
            dev = by_inst.get(inst)
            if dev is None or classify(dev) != "attached":
                out.add(inst)
        return out

    def instance_ids_long_waiting(
        self,
        devs: list[dict[str, Any]],
        remember_ids: set[str],
        *,
        threshold_sec: float = _LONG_WAIT_UI_THRESHOLD_SEC,
    ) -> set[str]:
        """Remembered devices still attaching after a long wait threshold."""
        out: set[str] = set()
        by_inst = {
            d["InstanceId"]: d for d in devs if d.get("InstanceId")
        }
        now = time.monotonic()
        for inst in remember_ids:
            started = 0.0
            live = False
            pair = self._procs.get(inst)
            if pair:
                proc, _bus, started = pair
                live = proc.poll() is None
            else:
                ext = self._external_procs.get(inst)
                if ext:
                    pid, _bus, started = ext
                    live = _pid_is_running(pid)
            if not live:
                continue
            dev = by_inst.get(inst)
            if dev is not None and classify(dev) == "attached":
                continue
            if (now - started) >= threshold_sec:
                out.add(inst)
        return out
