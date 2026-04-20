from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from usbipd_attach_manager.process import run_cmd_async, run_cmd_stream_merged_async

# Official WinGet identifier (see https://github.com/dorssel/usbipd-win)
USBIPD_WINGET_ID = "dorssel.usbipd-win"


def find_winget() -> str | None:
    w = shutil.which("winget")
    if w:
        return w
    local = (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "Microsoft"
        / "WindowsApps"
        / "winget.exe"
    )
    if local.is_file():
        return str(local)
    return None


async def winget_install_usbipd(
    *,
    timeout: float = 600.0,
    on_output_text: Callable[[str], None] | None = None,
    cancel_event: asyncio.Event | None = None,
) -> tuple[bool, str]:
    """
    Install usbipd-win via WinGet. Requires a working `winget` on PATH or under
    LocalAppData WindowsApps (Store-delivered WinGet).

    When ``on_output_text`` is set, stdout and stderr are merged and streamed in
    chunks (same mechanism as WSL install).
    """
    winget = find_winget()
    if not winget:
        return False, "WinGet was not found. Install App Installer from the Microsoft Store."
    args = [
        "install",
        "-e",
        "--id",
        USBIPD_WINGET_ID,
        "--accept-package-agreements",
        "--accept-source-agreements",
        "--disable-interactivity",
    ]
    if on_output_text is None:
        code, out, err = await run_cmd_async(winget, args, timeout=timeout)
        combined = "\n".join(x for x in (out, err) if x).strip()
        if code == 0:
            return True, combined or "usbipd-win installed via WinGet."
        detail = combined or f"WinGet exited with code {code}."
        return False, detail

    code, combined = await run_cmd_stream_merged_async(
        winget,
        args,
        on_text=on_output_text,
        timeout=timeout,
        cancel_event=cancel_event,
    )
    text = combined.strip()
    if code == 0:
        return True, text or "usbipd-win installed via WinGet."
    detail = text or f"WinGet exited with code {code}."
    return False, detail


# ``--test-setup-dialog`` when usbipd already works: same streaming pipeline as WinGet
# (merged stdout/stderr via ``run_cmd_stream_merged_async``), without installing packages.
_POWERSHELL_SETUP_STREAM_TEST = (
    "& { $ErrorActionPreference='Continue'; "
    "Write-Output '==> Test: PowerShell streaming (same code path as WinGet install)'; "
    "Write-Output ('PSVersion: ' + $PSVersionTable.PSVersion.ToString()); "
    "if (Get-Command winget -ErrorAction SilentlyContinue) { "
    "Write-Output '==> winget --version'; winget --version "
    "} else { "
    "Write-Output 'Tip: winget.exe not on PATH; install App Installer to see output here.' "
    "} }"
)


async def powershell_stream_setup_dialog_test(
    *,
    timeout: float = 120.0,
    on_output_text: Callable[[str], None] | None = None,
    cancel_event: asyncio.Event | None = None,
) -> tuple[bool, str]:
    """
    For forced setup-dialog test mode when the real WinGet step is skipped: stream
    PowerShell output (and ``winget --version`` when available) so the live log
    exercises the WinGet streaming code path.
    """
    ps_args = [
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        _POWERSHELL_SETUP_STREAM_TEST,
    ]
    if on_output_text is None:
        code, out, err = await run_cmd_async("powershell.exe", ps_args, timeout=timeout)
        combined = "\n".join(x for x in (out, err) if x).strip()
        if code == 0:
            return True, combined or "Stream test completed."
        return False, combined or f"PowerShell exited with code {code}."

    code, combined = await run_cmd_stream_merged_async(
        "powershell.exe",
        ps_args,
        on_text=on_output_text,
        timeout=timeout,
        cancel_event=cancel_event,
    )
    text = combined.strip()
    if code == 0:
        return True, text or "Stream test completed."
    return False, text or f"PowerShell exited with code {code}."


# Matches common WSL Ubuntu/Debian guidance (usbutils / linux-tools for usbip client).
# run_cmd_stream_merged_async merges stderr into stdout (same pipe as subprocess.STDOUT).
# Invoked as ``wsl.exe -d <distro> -u root`` so apt runs without interactive sudo (piped
# sessions cannot enter a password). Phase echoes + optional ``script`` PTY; lock wait.
_WSL_APT_SETUP_SCRIPT = r"""set -e
export DEBIAN_FRONTEND=noninteractive
echo "==> WSL: starting apt setup as root (stdout+stderr shown together)"
if ! command -v apt-get >/dev/null 2>&1; then
  echo "Automatic install only supports apt-based distros. Install usbutils (and USB/IP client tools if needed) with your package manager." >&2
  exit 4
fi
if command -v fuser >/dev/null 2>&1; then
  n=0
  while (( n < 45 )); do
    if fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1 \
       || fuser /var/lib/dpkg/lock >/dev/null 2>&1; then
      echo "==> Waiting for dpkg/apt lock (another apt/dpkg may be running)..."
      sleep 2
      n=$((n + 1))
    else
      break
    fi
  done
fi
run_apt() {
  echo "==> apt-get update (-q: log-friendly, no progress-bar spam)"
  apt-get -q update
  echo "==> apt-get install (usbutils, linux-tools-generic, hwdata)"
  apt-get -q -y install usbutils linux-tools-generic hwdata
}
if command -v script >/dev/null 2>&1; then
  script -q -e -c "set -e; export DEBIAN_FRONTEND=noninteractive; echo '==> apt-get update'; apt-get -q update; echo '==> apt-get install'; apt-get -q -y install usbutils linux-tools-generic hwdata" /dev/null
else
  run_apt
fi
"""


async def wsl_install_usbip_client_packages(
    distro: str,
    *,
    timeout: float = 900.0,
    on_output_text: Callable[[str], None] | None = None,
    cancel_event: asyncio.Event | None = None,
) -> tuple[bool, str]:
    """
    Install USB client packages inside a WSL distro (Debian/Ubuntu via apt).

    Runs ``wsl.exe -d <distro> -u root`` so ``apt-get`` does not require interactive
    ``sudo`` (no TTY for a password in our piped session).
    When ``on_output_text`` is set, merged stdout/stderr is streamed in UTF-8 chunks.
    """
    d = distro.strip()
    if not d:
        return False, "No WSL distribution selected."
    args = ["-d", d, "-u", "root", "--", "bash", "-lc", _WSL_APT_SETUP_SCRIPT]
    if on_output_text is None:
        code, out, err = await run_cmd_async("wsl.exe", args, timeout=timeout)
        combined = "\n".join(x for x in (out, err) if x).strip()
        if code == 0:
            return True, combined or "WSL packages installed."
        detail = combined or f"WSL setup exited with code {code}."
        return False, detail

    code, combined = await run_cmd_stream_merged_async(
        "wsl.exe",
        args,
        on_text=on_output_text,
        timeout=timeout,
        cancel_event=cancel_event,
    )
    text = combined.strip()
    if code == 0:
        return True, text or "WSL packages installed."
    detail = text or f"WSL setup exited with code {code}."
    return False, detail
