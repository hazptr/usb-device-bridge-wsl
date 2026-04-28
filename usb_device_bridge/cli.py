from __future__ import annotations

import sys
from pathlib import Path

import flet as ft

from usb_device_bridge.app_logging import setup_logging
from usb_device_bridge.single_instance import (
    prepare_single_instance,
    release_singleton_mutex_before_uac_if_needed,
    start_focus_server,
)
from usb_device_bridge.ui import run_app
from usb_device_bridge.windows.admin import ensure_administrator_windows


def _repo_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def main() -> None:
    setup_logging()
    if not prepare_single_instance():
        sys.exit(0)
    release_singleton_mutex_before_uac_if_needed()
    ensure_administrator_windows()
    start_focus_server()
    assets = _repo_root() / "assets"
    ft.run(run_app, assets_dir=str(assets))
