from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from usbipd_attach_manager.config import save_config
from usbipd_attach_manager.firewall import (
    apply_wsl_public_profile_firewall_fix_async,
    usbipd_output_suggests_firewall_block,
)
from usbipd_attach_manager.process import run_cmd, run_cmd_async, run_executable_cancellable

_log = logging.getLogger(__name__)

ATTACH_CMD_TIMEOUT_SEC = 50.0

# Shorter than normal interactive timeouts so app exit cannot hang on stuck usbipd/WSL.
SHUTDOWN_USBIPD_CMD_TIMEOUT_SEC = 45.0
# Upper bound for one full disconnect (detach + state + unbind) during shutdown.
SHUTDOWN_PER_DEVICE_MAX_SEC = 150.0


def find_usbipd() -> str:
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pfx86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    for base in (pf, pfx86):
        candidate = Path(base) / "usbipd-win" / "usbipd.exe"
        if candidate.is_file():
            return str(candidate)
    return "usbipd"


def parse_usbipd_state(
    usbipd: str, *, timeout: float = 120.0
) -> tuple[list[dict[str, Any]] | None, str | None]:
    code, out, err = run_cmd(usbipd, ["state"], timeout=timeout)
    if code != 0:
        return None, err or out or "usbipd state failed."
    try:
        data = json.loads(out)
        devices = data.get("Devices") or []
        if not isinstance(devices, list):
            return None, "Unexpected usbipd state format."
        return devices, None
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON from usbipd: {e}"


def usbipd_cli_works(usbipd: str) -> bool:
    """Return True if ``usbipd state`` runs successfully (usbipd-win is installed)."""
    _devs, err = parse_usbipd_state(usbipd)
    return err is None


def classify(dev: dict[str, Any]) -> str:
    if dev.get("ClientIPAddress"):
        return "attached"
    if dev.get("StubInstanceId"):
        return "shared"
    if dev.get("BusId"):
        return "available"
    return "offline"


def _state_rank_attached_first(st: str) -> int:
    return {"attached": 0, "shared": 1, "available": 2, "offline": 3}.get(st, 9)


def _state_rank_connectable_first(st: str) -> int:
    return {"available": 0, "shared": 1, "attached": 2, "offline": 3}.get(st, 9)


def _bus_id_sort_tuple(bus: str | None) -> tuple:
    if not bus:
        return (999, 999)
    try:
        p = str(bus).replace("_", "-").split("-", 1)
        a = int(p[0])
        b = int(p[1]) if len(p) > 1 else 0
        return (a, b)
    except (ValueError, IndexError):
        return (998, str(bus))


def touch_device(cfg: dict[str, Any], instance_id: str) -> None:
    if not instance_id:
        return
    cfg.setdefault("device_recency", {})[instance_id] = time.time()
    save_config(cfg)


def sort_devices_list(
    devs: list[dict[str, Any]],
    sort_order: str,
    recency: dict[str, Any],
) -> list[dict[str, Any]]:
    out = list(devs)

    def inst(d: dict[str, Any]) -> str:
        return d.get("InstanceId") or ""

    def name_key(d: dict[str, Any]) -> str:
        return (d.get("Description") or "").lower()

    if sort_order == "state_attached_first":
        out.sort(key=lambda d: (_state_rank_attached_first(classify(d)), name_key(d)))
    elif sort_order == "state_connectable_first":
        out.sort(key=lambda d: (_state_rank_connectable_first(classify(d)), name_key(d)))
    elif sort_order == "recents":
        out.sort(
            key=lambda d: (
                -float(recency.get(inst(d), 0.0) or 0.0),
                name_key(d),
            )
        )
    elif sort_order == "name":
        out.sort(key=name_key)
    elif sort_order == "bus_id":
        out.sort(key=lambda d: (_bus_id_sort_tuple(d.get("BusId")), name_key(d)))
    else:
        out.sort(key=lambda d: (_state_rank_attached_first(classify(d)), name_key(d)))
    return out


def vid_pid_from_instance(instance_id: str | None) -> str | None:
    if not instance_id:
        return None
    u = instance_id.upper()
    if "VID_" not in u or "PID_" not in u:
        return None
    try:
        vid_s = u.split("VID_", 1)[1][:4]
        rest = u.split("PID_", 1)[1]
        pid_s = rest[:4]
        return f"{vid_s}:{pid_s}".lower()
    except IndexError:
        return None


async def usbipd_bind(
    usbipd: str, bus_id: str, *, cancel_event: asyncio.Event | None = None
) -> tuple[bool, str]:
    code, out, err = await run_executable_cancellable(
        usbipd, ["bind", "-b", bus_id], cancel_event=cancel_event, timeout=120.0
    )
    if code == 0:
        return True, ""
    if code == -1 and err == "Cancelled.":
        return False, "Cancelled."
    return False, err or out or "bind failed"


async def usbipd_attach_once(
    usbipd: str,
    distro: str,
    bus_id: str,
    *,
    auto: bool,
    timeout: float,
    cancel_event: asyncio.Event | None = None,
) -> tuple[bool, str]:
    args = ["attach", "--wsl", distro, "-b", bus_id]
    if auto:
        args.append("-a")
    code, out, err = await run_executable_cancellable(
        usbipd, args, cancel_event=cancel_event, timeout=timeout
    )
    combined = f"{err}\n{out}".strip()
    if code == 0:
        _log.info(
            "usbipd attach succeeded (distro=%s, bus_id=%s, auto=%s)",
            distro,
            bus_id,
            auto,
        )
        return True, ""
    if code == -1 and err == "Cancelled.":
        return False, "Cancelled."
    return False, combined or "attach failed"


async def usbipd_attach_with_firewall_recovery(
    usbipd: str,
    distro: str,
    bus_id: str,
    *,
    auto: bool,
    cancel_event: asyncio.Event | None = None,
) -> tuple[bool, str]:
    ok, msg = await usbipd_attach_once(
        usbipd,
        distro,
        bus_id,
        auto=auto,
        timeout=ATTACH_CMD_TIMEOUT_SEC,
        cancel_event=cancel_event,
    )
    if ok:
        return True, ""
    if msg == "Cancelled." or (cancel_event and cancel_event.is_set()):
        return False, "Cancelled."
    if not usbipd_output_suggests_firewall_block(msg):
        return False, msg
    if cancel_event and cancel_event.is_set():
        return False, "Cancelled."
    fix_ok, fix_err = await apply_wsl_public_profile_firewall_fix_async()
    if cancel_event and cancel_event.is_set():
        return False, "Cancelled."
    if not fix_ok:
        return (
            False,
            "usbipd attach failed, likely due to Windows Firewall / Public profile on "
            "the WSL vEthernet adapter.\n\n"
            f"Automatic fix failed: {fix_err}\n\n"
            f"usbipd output:\n{msg}",
        )
    ok2, msg2 = await usbipd_attach_once(
        usbipd,
        distro,
        bus_id,
        auto=auto,
        timeout=ATTACH_CMD_TIMEOUT_SEC,
        cancel_event=cancel_event,
    )
    if ok2:
        return True, ""
    if msg2 == "Cancelled." or (cancel_event and cancel_event.is_set()):
        return False, "Cancelled."
    return (
        False,
        "Attach still failing after adjusting the Public firewall profile for WSL "
        f"vEthernet adapters.\n\n{msg2}",
    )


async def usbipd_detach(
    usbipd: str, bus_id: str, *, timeout: float = 120.0
) -> tuple[bool, str]:
    code, out, err = await run_cmd_async(
        usbipd, ["detach", "-b", bus_id], timeout=timeout
    )
    if code == 0:
        _log.info("usbipd detach succeeded (bus_id=%s)", bus_id)
        return True, ""
    return False, err or out or "detach failed"


async def usbipd_unbind(
    usbipd: str, dev: dict[str, Any], *, timeout: float = 120.0
) -> tuple[bool, str]:
    bus_id = dev.get("BusId")
    guid = dev.get("PersistedGuid")
    if bus_id:
        code, out, err = await run_cmd_async(
            usbipd, ["unbind", "-b", bus_id], timeout=timeout
        )
    elif guid:
        code, out, err = await run_cmd_async(
            usbipd, ["unbind", "-g", guid], timeout=timeout
        )
    else:
        return False, "No BusId or PersistedGuid for unbind."
    if code == 0:
        return True, ""
    return False, err or out or "unbind failed"


async def usbipd_disconnect_fully(
    usbipd: str, dev: dict[str, Any], *, command_timeout: float = 120.0
) -> tuple[bool, str]:
    """Detach from WSL when attached, then unbind so the device is no longer shared.

    Replaces separate detach + end-share flows with a single user action.
    """
    inst = dev.get("InstanceId") or ""
    st = classify(dev)

    if st == "attached":
        bid = dev.get("BusId")
        if not bid:
            return False, "No BusId — cannot detach."
        ok, msg = await usbipd_detach(usbipd, bid, timeout=command_timeout)
        if not ok:
            return False, msg
        devs, err = await asyncio.to_thread(
            lambda: parse_usbipd_state(usbipd, timeout=command_timeout)
        )
        if err or devs is None:
            return False, err or "Could not read usbipd state after detach."
        dev = next((d for d in devs if d.get("InstanceId") == inst), dev)
        st = classify(dev)

    should_unbind = st in ("attached", "shared") or (
        st == "offline" and bool(dev.get("PersistedGuid"))
    )
    if not should_unbind:
        return True, ""

    return await usbipd_unbind(usbipd, dev, timeout=command_timeout)


async def usbipd_disconnect_all_on_exit(
    usbipd: str,
    *,
    on_progress: Callable[[int, int], Awaitable[None]] | None = None,
    command_timeout: float = SHUTDOWN_USBIPD_CMD_TIMEOUT_SEC,
    per_device_cap: float = SHUTDOWN_PER_DEVICE_MAX_SEC,
) -> None:
    """Detach and unbind every device that is attached, shared, or offline-but-persisted.

    Used when the app exits normally so USB devices are no longer shared with WSL.

    ``on_progress`` is invoked as ``await on_progress(current, total)`` with ``current``
    in ``1..total`` before each device is processed. If there is nothing to disconnect,
    it is invoked once as ``(0, 0)``.
    """
    devs, err = await asyncio.to_thread(
        lambda: parse_usbipd_state(usbipd, timeout=command_timeout)
    )
    if err or not devs:
        if err:
            _log.warning("Shutdown disconnect: could not read usbipd state: %s", err)
        if on_progress:
            await on_progress(0, 0)
        return
    pending = [
        d
        for d in devs
        if classify(d) in ("attached", "shared")
        or (classify(d) == "offline" and d.get("PersistedGuid"))
    ]
    if not pending:
        if on_progress:
            await on_progress(0, 0)
        return
    total = len(pending)
    for idx, dev in enumerate(pending):
        if on_progress:
            await on_progress(idx + 1, total)
        label = dev.get("InstanceId") or dev.get("PersistedGuid") or "?"
        try:
            ok, msg = await asyncio.wait_for(
                usbipd_disconnect_fully(
                    usbipd, dev, command_timeout=command_timeout
                ),
                timeout=per_device_cap,
            )
        except asyncio.TimeoutError:
            _log.warning(
                "Shutdown disconnect: timed out after %ss for %s",
                int(per_device_cap),
                label,
            )
            continue
        if ok:
            _log.info("Shutdown disconnect: cleared %s", label)
        else:
            _log.warning("Shutdown disconnect: %s failed: %s", label, msg)


async def connect_to_wsl(
    usbipd: str,
    distro: str,
    dev: dict[str, Any],
    *,
    auto_attach: bool,
    cancel_event: asyncio.Event | None = None,
) -> tuple[bool, str]:
    bus_id = dev.get("BusId")
    if not bus_id:
        return False, "Device is not connected (no BusId)."
    st = classify(dev)
    if st == "attached":
        return False, "Already attached to a client."
    if st == "available":
        ok, msg = await usbipd_bind(usbipd, bus_id, cancel_event=cancel_event)
        if not ok:
            return False, msg
        if cancel_event and cancel_event.is_set():
            return False, "Cancelled."
    return await usbipd_attach_with_firewall_recovery(
        usbipd, distro, bus_id, auto=auto_attach, cancel_event=cancel_event
    )
