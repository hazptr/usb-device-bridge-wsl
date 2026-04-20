"""
USB/IP (usbipd-win) → WSL attachment manager — desktop UI built with Flet.

Requires: Windows, usbipd-win, WSL2, Python 3.10+.

  py -m pip install -r requirements.txt
  py main.py
  py -m usbipd_attach_manager

Settings are stored under %LOCALAPPDATA%\\usbipd-device-attach-manager\\config.json
Logs (including uncaught exceptions) go to app.log in that folder; fatal Python fault
handler output is appended to fault.txt.

On Windows, if not already elevated, the app triggers UAC and re-launches itself
as administrator (required for usbipd bind/attach).

If attach hangs or warns about firewall / TCP 3240, the app tries to add WSL
vEthernet adapters to the Public profile's DisabledInterfaceAliases (same effect
as the Set-NetFirewallProfile one-liner many users run after reboot).

Remembered devices: while this app is running and a WSL distro is selected, the
app is meant to keep them attached over time (see AGENTS.md). There is no
separate toggle for that behavior.

First-run setup: if usbipd-win is missing, a setup dialog runs at startup. To
open that dialog for testing even when usbipd is installed, use
``--test-setup-dialog`` or set environment variable
``USBIPD_ATTACH_MANAGER_TEST_SETUP_DIALOG=1``. In that mode, when the real WinGet
install is skipped, a short streamed PowerShell snippet (including ``winget --version``
when available) runs first so the WinGet-style log path can be checked without
installing packages.
"""

from usbipd_attach_manager.cli import main

if __name__ == "__main__":
    main()
