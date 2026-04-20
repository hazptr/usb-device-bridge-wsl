from __future__ import annotations

import sys
from pathlib import Path

import flet as ft

from usbipd_attach_manager.app_logging import setup_logging
from usbipd_attach_manager.single_instance import (
    prepare_single_instance,
    release_singleton_mutex_before_uac_if_needed,
    start_focus_server,
)
from usbipd_attach_manager.ui import run_app
from usbipd_attach_manager.windows_admin import ensure_administrator_windows


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
