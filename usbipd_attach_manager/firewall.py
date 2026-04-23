from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

from usbipd_attach_manager.process import run_cmd

_log = logging.getLogger(__name__)


def _clip(text: str, *, limit: int = 600) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."

# usbipd attach can hang when the WSL vEthernet uses the Public firewall profile and
# policy blocks the usbipd rule — exclude those NICs from the Public profile (same idea
# as: Set-NetFirewallProfile -Profile Public -DisabledInterfaceAliases "vEthernet (WSL …)").
_WSL_PUBLIC_PROFILE_FIX_PS1 = (
    "$ErrorActionPreference='Stop';"
    "$names=@(Get-NetAdapter -ErrorAction SilentlyContinue|"
    "?{$_.Name -like '*vEthernet*WSL*'}|"
    "%{$_.Name}|Sort-Object -Unique);"
    "if($names.Count -eq 0){exit 0};"
    "$prof=Get-NetFirewallProfile -Name Public;"
    "$cur=@($prof.DisabledInterfaceAliases);"
    "foreach($n in $names){if($cur -notcontains $n){$cur+=$n}};"
    "Set-NetFirewallProfile -Profile Public -DisabledInterfaceAliases $cur"
)


def _powershell_exe() -> str:
    return str(
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )


def apply_wsl_public_profile_firewall_fix() -> tuple[bool, str]:
    """
    Merge WSL Hyper-V vEthernet adapter names into the Public profile's
    DisabledInterfaceAliases so traffic is not blocked by Public-profile / GPO rules
    that affect usbipd (TCP 3240). Requires Administrator (the app already elevates).

    """
    if sys.platform != "win32":
        _log.debug("Skipping firewall fix: non-Windows platform (%s)", sys.platform)
        return True, ""
    ps = _powershell_exe()
    if not Path(ps).is_file():
        _log.error("Firewall fix cannot run: PowerShell executable not found (%s)", ps)
        return False, "PowerShell not found."
    _log.info(
        "Running WSL Public-profile firewall fix via PowerShell (exe=%s)",
        ps,
    )
    code, out, err = run_cmd(
        ps,
        ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", _WSL_PUBLIC_PROFILE_FIX_PS1],
        timeout=90,
    )
    if code == 0:
        if out.strip():
            _log.info("Firewall fix succeeded with output: %s", _clip(out.strip()))
        else:
            _log.info("Firewall fix succeeded.")
        return True, ""
    detail = (err or out or "Set-NetFirewallProfile failed").strip()
    _log.warning(
        "Firewall fix failed (exit=%s): %s",
        code,
        _clip(detail),
    )
    return False, detail


async def apply_wsl_public_profile_firewall_fix_async() -> tuple[bool, str]:
    return await asyncio.to_thread(apply_wsl_public_profile_firewall_fix)


def usbipd_output_suggests_firewall_block(text: str) -> bool:
    t = text.lower()
    if "timed out" in t:
        _log.warning(
            "usbipd output suggests firewall/network block (matched='timed out'): %s",
            _clip(text.strip()),
        )
        return True
    markers = [
        "firewall",
        "3240",
        "group policy",
        "public network profile",
        "blocking the connection",
    ]
    hits = [k for k in markers if k in t]
    if hits:
        _log.warning(
            "usbipd output suggests firewall/network block (matched=%s): %s",
            ", ".join(hits),
            _clip(text.strip()),
        )
        return True
    return False
