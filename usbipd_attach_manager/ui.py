from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import flet as ft

from usbipd_attach_manager.app_logging import install_asyncio_exception_logging
from usbipd_attach_manager.auto_attach import AutoAttachManager
from usbipd_attach_manager.config import (
    load_config,
    prune_device_entry_if_unused,
    remembered_instance_ids,
    save_config,
)
from usbipd_attach_manager.firewall import apply_wsl_public_profile_firewall_fix_async
from usbipd_attach_manager.single_instance import set_focus_handler, set_yield_handler
from usbipd_attach_manager.system_package_install import (
    find_winget,
    powershell_stream_setup_dialog_test,
    winget_install_usbipd,
    wsl_install_usbip_client_packages,
)
from usbipd_attach_manager.tray_manager import TrayManager
from usbipd_attach_manager.usbipd import (
    classify,
    connect_to_wsl,
    find_usbipd,
    parse_usbipd_state,
    sort_devices_list,
    touch_device,
    usbipd_cli_works,
    usbipd_disconnect_all_on_exit,
    usbipd_disconnect_fully,
    vid_pid_from_instance,
)
from usbipd_attach_manager.version_info import get_display_version
from usbipd_attach_manager.windows_startup import (
    can_configure_run_at_logon,
    is_run_at_logon_enabled,
    set_run_at_logon,
)
from usbipd_attach_manager.wsl import parse_wsl_distros

ACCENT = "#2dd4bf"
# Footer uses icon-only buttons below this width (half-screen on many monitors).
FOOTER_COMPACT_BREAKPOINT_PX = 1150

_LOG_SETUP_INSTALL = logging.getLogger(__name__ + ".setup_install")


def _test_setup_dialog_requested() -> bool:
    if "--test-setup-dialog" in sys.argv:
        return True
    v = os.environ.get("USBIPD_ATTACH_MANAGER_TEST_SETUP_DIALOG", "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _device_list_fingerprint(
    devs: list[dict[str, Any]],
    order: str,
    cfg: dict[str, Any],
    *,
    manual_attaching: set[str],
    auto_attaching_ids: set[str],
    auto_failed_ids: set[str],
    auto_long_wait_ids: set[str],
) -> str:
    """Stable hash for whether the rendered device list would change."""
    normalized = sorted(devs, key=lambda d: d.get("InstanceId") or "")
    dev_prefs = sorted(
        (k, sorted(ent.items()))
        for k, ent in (cfg.get("devices") or {}).items()
        if isinstance(ent, dict)
    )
    return json.dumps(
        {
            "d": normalized,
            "o": order,
            "r": cfg.get("device_recency") or {},
            "dev": dev_prefs,
            "m": sorted(manual_attaching),
            "a": sorted(auto_attaching_ids),
            "f": sorted(auto_failed_ids),
            "l": sorted(auto_long_wait_ids),
        },
        sort_keys=True,
        default=str,
    )


def _assets_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "assets"
    return Path(__file__).resolve().parent.parent / "assets"


async def run_app(page: ft.Page) -> None:
    install_asyncio_exception_logging()
    _win_title = f"USB/IP → WSL — {get_display_version()}"
    page.title = _win_title
    ico = _assets_dir() / "app_icon.ico"
    if ico.is_file():
        page.window.icon = str(ico)
    page.window.width = 980
    page.window.height = 720
    page.window.min_width = 720
    page.window.min_height = 520
    page.window.title_bar_hidden = True
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 0
    page.bgcolor = "#0b1220"

    cfg = load_config()
    usbipd_exe = find_usbipd()
    wsl_distro_names: list[str] = []

    remember_startup = ft.Switch(
        label="",
        value=cfg.get("apply_on_startup", False),
        active_color=ACCENT,
    )

    auto_refresh_sw = ft.Switch(
        label="",
        value=cfg.get("auto_refresh", True),
        active_color=ACCENT,
    )

    _txt_startup = ft.Text(
        "Apply on startup",
        size=10,
        color="#cbd5e1",
        text_align=ft.TextAlign.CENTER,
    )
    _txt_autorefresh = ft.Text(
        "Auto-refresh (3s)",
        size=10,
        color="#cbd5e1",
        text_align=ft.TextAlign.CENTER,
        tooltip=(
            "Refreshes the device list on a timer. If you have remembered devices, "
            "the list still updates periodically so plug-ins are noticed; remembered "
            "attachment runs in the background either way."
        ),
    )
    col_remember_switch = ft.Column(
        [_txt_startup, remember_startup],
        spacing=3,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        tight=True,
    )
    col_auto_switch = ft.Column(
        [_txt_autorefresh, auto_refresh_sw],
        spacing=3,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        tight=True,
    )
    minimize_to_tray_sw = ft.Switch(
        label="",
        value=cfg.get("minimize_to_tray", False),
        active_color=ACCENT,
    )
    _txt_tray = ft.Text(
        "To tray",
        size=10,
        color="#cbd5e1",
        text_align=ft.TextAlign.CENTER,
        tooltip=(
            "Close or minimize sends the window to the notification area "
            "(system tray). Open the tray icon to show the window or exit."
        ),
    )
    col_tray_switch = ft.Column(
        [_txt_tray, minimize_to_tray_sw],
        spacing=3,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        tight=True,
    )
    _start_win_available = can_configure_run_at_logon()
    start_with_windows_sw = ft.Switch(
        label="",
        value=is_run_at_logon_enabled() if _start_win_available else False,
        active_color=ACCENT,
        disabled=not _start_win_available,
    )
    _txt_start_win = ft.Text(
        "Start with Windows",
        size=10,
        color="#cbd5e1",
        text_align=ft.TextAlign.CENTER,
        tooltip=(
            "Adds this app to your per-user startup list so it runs when you sign in. "
            "This is not a Windows background service. Turning it on does not require "
            "administrator approval; you may still see one UAC prompt when the app "
            "starts so usbipd can manage USB devices."
        ),
    )
    col_start_win_switch = ft.Column(
        [_txt_start_win, start_with_windows_sw],
        spacing=3,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        tight=True,
        visible=_start_win_available,
    )
    sort_dd = ft.Dropdown(
        label="Sort",
        expand=True,
        dense=True,
        border_color="#334155",
        filled=True,
        bgcolor="#1e293b",
        label_style=ft.TextStyle(color="#94a3b8", size=12),
        text_style=ft.TextStyle(color="#f1f5f9", size=13),
        options=[
            ft.DropdownOption(
                key="state_attached_first", text="State · attached first"
            ),
            ft.DropdownOption(
                key="state_connectable_first", text="State · connectable first"
            ),
            ft.DropdownOption(key="recents", text="Recents first"),
            ft.DropdownOption(key="name", text="Name A–Z"),
            ft.DropdownOption(key="bus_id", text="Bus ID"),
        ],
        value=cfg.get("sort_order", "state_attached_first"),
    )

    status_text = ft.Text("", color="#94a3b8", size=11, expand=True)
    title_heading = ft.Text(
        _win_title,
        size=13,
        weight=ft.FontWeight.W_600,
        color="#f1f5f9",
    )
    device_list = ft.Column(spacing=4, scroll=ft.ScrollMode.AUTO, expand=True)
    loading_overlay = ft.Container(
        visible=True,
        expand=True,
        bgcolor="#0b1220",
        alignment=ft.Alignment.CENTER,
        content=ft.Column(
            [
                ft.ProgressRing(width=44, height=44, stroke_width=3),
                ft.Text(
                    "Loading devices…",
                    size=13,
                    color="#94a3b8",
                ),
            ],
            spacing=14,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            tight=True,
        ),
    )
    shutdown_status_text = ft.Text(
        "Disconnecting devices…",
        size=13,
        color="#94a3b8",
    )
    shutdown_overlay = ft.Container(
        visible=False,
        expand=True,
        bgcolor="#0b1220",
        alignment=ft.Alignment.CENTER,
        content=ft.Column(
            [
                ft.ProgressRing(width=44, height=44, stroke_width=3),
                shutdown_status_text,
            ],
            spacing=14,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            tight=True,
        ),
    )
    device_list_stack = ft.Stack(
        [
            ft.Container(device_list, expand=True),
            loading_overlay,
            shutdown_overlay,
        ],
        expand=True,
    )
    initial_device_load_pending = True
    last_list_fingerprint: str | None = None
    manual_attaching: set[str] = set()
    manual_cancel_events: dict[str, asyncio.Event] = {}
    seen_auto_failures: set[str] = set()
    poll_stop = asyncio.Event()
    install_cancel_holder: list[asyncio.Event | None] = [None]
    auto_attach_manager = AutoAttachManager()
    preserve_auto_attach_on_exit = [False]

    def _auto_attach_atexit_cleanup() -> None:
        if preserve_auto_attach_on_exit[0]:
            logging.getLogger(__name__).info(
                "Skipping auto-attach cleanup during instance handoff."
            )
            return
        auto_attach_manager.terminate_all()

    atexit.register(_auto_attach_atexit_cleanup)
    _shutdown_started = [False]
    tray = TrayManager(ico, _win_title)

    async def show_from_tray() -> None:
        page.window.visible = True
        page.window.skip_task_bar = False
        await page.window.to_front()
        page.update()

    async def full_shutdown(*, yield_to_replacement: bool) -> None:
        if _shutdown_started[0]:
            return
        _shutdown_started[0] = True
        poll_stop.set()
        ev = install_cancel_holder[0]
        if ev is not None:
            ev.set()
        preserve_auto_attach_on_exit[0] = bool(yield_to_replacement)
        if not yield_to_replacement:
            auto_attach_manager.terminate_all()
        if not yield_to_replacement:
            await asyncio.sleep(0.2)
            page.window.minimized = False
            page.window.visible = True
            page.window.skip_task_bar = False
            await page.window.to_front()
            loading_overlay.visible = False
            shutdown_overlay.visible = True
            shutdown_status_text.value = "Disconnecting devices…"
            page.update()

            async def on_shutdown_progress(current: int, total: int) -> None:
                if total == 0:
                    shutdown_status_text.value = "Shutting down…"
                elif total == 1:
                    shutdown_status_text.value = "Disconnecting devices…"
                else:
                    shutdown_status_text.value = (
                        f"Disconnecting devices… ({current} of {total})"
                    )
                page.update()
                await asyncio.sleep(0)

            await usbipd_disconnect_all_on_exit(
                usbipd_exe, on_progress=on_shutdown_progress
            )
        tray.stop()
        page.window.prevent_close = False
        await page.window.destroy()

    async def exit_from_tray() -> None:
        await full_shutdown(yield_to_replacement=False)

    async def yield_to_newer_instance() -> None:
        logging.getLogger(__name__).info(
            "A newer version was started; exiting so it can take over."
        )
        await full_shutdown(yield_to_replacement=True)

    async def hide_to_tray() -> None:
        if not ico.is_file():
            show_error(
                "The tray icon asset is missing; cannot send the window to the "
                "notification area."
            )
            return
        tray.start()
        page.window.minimized = False
        page.window.visible = False
        page.update()

    tray.set_handlers(
        on_show=lambda: page.run_task(show_from_tray),
        on_exit=lambda: page.run_task(exit_from_tray),
    )
    set_focus_handler(lambda: page.run_task(show_from_tray))
    set_yield_handler(lambda: page.run_task(yield_to_newer_instance))

    def persist_cfg() -> None:
        cfg["apply_on_startup"] = remember_startup.value
        cfg["auto_refresh"] = auto_refresh_sw.value
        cfg["minimize_to_tray"] = minimize_to_tray_sw.value
        cfg["sort_order"] = sort_dd.value or "state_attached_first"
        save_config(cfg)

    def show_error(msg: str) -> None:
        page.snack_bar = ft.SnackBar(
            content=ft.Text(msg, color="#fecaca"),
            bgcolor="#450a0a",
        )
        page.snack_bar.open = True
        page.update()

    def show_ok(msg: str) -> None:
        page.snack_bar = ft.SnackBar(
            content=ft.Text(msg, color="#ecfdf5"),
            bgcolor="#064e3b",
        )
        page.snack_bar.open = True
        page.update()

    async def refresh_distros() -> None:
        names = await asyncio.to_thread(parse_wsl_distros)
        wsl_distro_names.clear()
        wsl_distro_names.extend(names)
        page.update()

    def distro_for_instance(instance_id: str) -> str | None:
        if not wsl_distro_names:
            return None
        devices = cfg.get("devices") or {}
        ent = devices.get(instance_id) if isinstance(devices, dict) else None
        if not isinstance(ent, dict):
            ent = {}
        pick = (ent.get("wsl_distro") or cfg.get("wsl_distro") or "").strip()
        if pick and pick in wsl_distro_names:
            return pick
        legacy = (cfg.get("wsl_distro") or "").strip()
        if legacy and legacy in wsl_distro_names:
            return legacy
        return wsl_distro_names[0]

    async def rebuild_devices(
        prefetched: tuple[list[dict[str, Any]] | None, str | None] | None = None,
    ) -> None:
        nonlocal last_list_fingerprint, initial_device_load_pending
        if prefetched is not None:
            devs, err = prefetched
        else:
            devs, err = await asyncio.to_thread(parse_usbipd_state, usbipd_exe)
        device_list.controls.clear()
        if err:
            device_list.controls.append(
                ft.Container(
                    content=ft.Text(err, color="#fca5a5"),
                    padding=16,
                )
            )
            status_text.value = "Could not read usbipd state."
            if initial_device_load_pending:
                initial_device_load_pending = False
            loading_overlay.visible = False
            page.update()
            return

        assert devs is not None
        await asyncio.to_thread(
            auto_attach_manager.sync,
            usbipd_exe,
            remembered_instance_ids(cfg),
            devs,
            distro_for_instance,
        )

        status_text.value = f"{len(devs)} device(s) — usbipd at {usbipd_exe}"
        remembered = remembered_instance_ids(cfg)
        auto_failed_ids = auto_attach_manager.failed_instance_ids()
        auto_long_wait_ids = auto_attach_manager.instance_ids_long_waiting(
            devs,
            remembered,
        )
        seen_auto_failures.intersection_update(auto_failed_ids)
        new_auto_failures = auto_failed_ids - seen_auto_failures
        if new_auto_failures:
            failed_name = "remembered device"
            for dev in devs:
                inst = dev.get("InstanceId") or ""
                if inst in new_auto_failures:
                    failed_name = dev.get("Description") or failed_name
                    break
            count = len(new_auto_failures)
            show_error(
                (
                    f"Auto-attach failed for {count} remembered device(s), including "
                    f"{failed_name}."
                )
                if count > 1
                else f"Auto-attach failed for remembered device: {failed_name}."
            )
            seen_auto_failures.update(new_auto_failures)

        async def cancel_attach_for_instance(instance_id: str) -> None:
            if not instance_id:
                return
            ev = manual_cancel_events.get(instance_id)
            if ev is not None:
                ev.set()
                await rebuild_devices()
                return
            auto_attach_manager.cancel_background_attach(instance_id)
            await rebuild_devices()

        async def retry_auto_attach_for_instance(instance_id: str) -> None:
            if not instance_id:
                return
            auto_attach_manager.retry_background_attach(instance_id)
            await rebuild_devices()

        async def toggle_remember(inst: str, checked: bool) -> None:
            if not inst:
                return
            ent = cfg.setdefault("devices", {}).setdefault(inst, {})
            if checked:
                ent["remembered"] = True
            else:
                ent.pop("remembered", None)
                prune_device_entry_if_unused(cfg, inst)
            touch_device(cfg, inst)
            await rebuild_devices()

        async def do_connect(dev: dict[str, Any]) -> None:
            inst = dev.get("InstanceId") or ""
            cancel_ev = asyncio.Event()
            if inst:
                manual_attaching.add(inst)
                manual_cancel_events[inst] = cancel_ev
                await rebuild_devices()
            try:
                d = distro_for_instance(inst) if inst else None
                if not d:
                    show_error(
                        "No WSL distribution available for this device. Install a "
                        "distro or check that WSL is listed."
                    )
                    return
                persist_cfg()
                ok, msg = await connect_to_wsl(
                    usbipd_exe,
                    d,
                    dev,
                    auto_attach=False,
                    cancel_event=cancel_ev,
                )
                if ok:
                    touch_device(cfg, dev.get("InstanceId") or "")
                    show_ok("Attached to WSL.")
                else:
                    if msg == "Cancelled.":
                        show_ok("Attach cancelled.")
                    else:
                        show_error(msg)
            finally:
                if inst:
                    manual_attaching.discard(inst)
                    manual_cancel_events.pop(inst, None)
                await rebuild_devices()

        async def do_disconnect(dev: dict[str, Any]) -> None:
            inst = dev.get("InstanceId") or ""
            ok, msg = await usbipd_disconnect_fully(usbipd_exe, dev)
            if ok:
                touch_device(cfg, inst)
                show_ok("Disconnected.")
            else:
                show_error(msg)
            await rebuild_devices()

        order = cfg.get("sort_order") or sort_dd.value or "state_attached_first"
        auto_busy_ids = auto_attach_manager.instance_ids_attaching(devs, remembered)
        last_list_fingerprint = _device_list_fingerprint(
            devs,
            order,
            cfg,
            manual_attaching=manual_attaching,
            auto_attaching_ids=auto_busy_ids,
            auto_failed_ids=auto_failed_ids,
            auto_long_wait_ids=auto_long_wait_ids,
        )
        for dev in sort_devices_list(devs, order, cfg.get("device_recency") or {}):
            desc = dev.get("Description") or "(no description)"
            bus = dev.get("BusId") or "—"
            inst = dev.get("InstanceId") or ""
            st = classify(dev)
            if st == "attached":
                st_label = "Attached"
                st_color = "#34d399"
            elif st == "shared":
                st_label = "Shared"
                st_color = "#fbbf24"
            elif st == "available":
                st_label = "Available"
                st_color = "#94a3b8"
            else:
                st_label = "Offline / persisted"
                st_color = "#a78bfa"

            vp = vid_pid_from_instance(inst) or "—"
            is_rem = inst in remembered
            fail_msg = auto_attach_manager.failure_for_instance(inst) if inst else None
            is_failed = bool(fail_msg)
            is_auto_attaching = bool(inst) and inst in auto_busy_ids
            is_long_wait = bool(inst) and inst in auto_long_wait_ids
            is_attaching = bool(inst) and (
                inst in manual_attaching or inst in auto_busy_ids
            )

            pill_bg = {
                "attached": "#14532d",
                "shared": "#713f12",
                "available": "#0f172a",
                "offline": "#312e81",
            }.get(st, "#1e293b")

            dd_val = distro_for_instance(inst) if inst else None
            if (
                dd_val is not None
                and wsl_distro_names
                and dd_val not in wsl_distro_names
            ):
                dd_val = wsl_distro_names[0]
            dev_distro_dd = ft.Dropdown(
                label="WSL",
                width=148,
                dense=True,
                border_color="#334155",
                filled=True,
                bgcolor="#1e293b",
                label_style=ft.TextStyle(color="#94a3b8", size=11),
                text_style=ft.TextStyle(color="#f1f5f9", size=12),
                options=[ft.dropdown.Option(n) for n in wsl_distro_names],
                value=dd_val,
                disabled=is_attaching or (not inst) or (not wsl_distro_names),
                hint_text="No distros" if not wsl_distro_names else None,
                tooltip="WSL distro for this device",
            )

            def _dev_distro_change(e: ft.ControlEvent, ii: str = inst) -> None:
                if not ii:
                    return
                sel = (getattr(e.control, "value", None) or "") or ""
                cfg.setdefault("devices", {}).setdefault(ii, {})["wsl_distro"] = sel
                save_config(cfg)
                auto_attach_manager.cancel_background_attach(ii)
                page.run_task(rebuild_devices)

            dev_distro_dd.on_change = _dev_distro_change

            text_block = ft.Column(
                [
                    ft.Text(
                        desc,
                        size=13,
                        weight=ft.FontWeight.W_600,
                        color="#f8fafc",
                        max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    ft.Text(
                        f"Bus {bus}  ·  {vp}",
                        size=11,
                        color="#64748b",
                    ),
                ],
                spacing=2,
                tight=True,
            )

            center_row = ft.Row(
                [
                    text_block,
                    *(
                        [
                            ft.Container(
                                expand=True,
                                padding=ft.padding.only(left=8),
                                content=ft.Row(
                                    [
                                        ft.ProgressBar(
                                            expand=True,
                                            color=ACCENT,
                                            bgcolor="#1f2937",
                                            bar_height=3,
                                            border_radius=2,
                                        ),
                                        ft.Text(
                                            (
                                                "This device is taking a long time to attach. Retry?"
                                                if is_auto_attaching and is_long_wait
                                                else "Attaching…"
                                            ),
                                            size=10,
                                            color="#fbbf24",
                                            weight=ft.FontWeight.W_500,
                                            no_wrap=True,
                                        ),
                                        ft.TextButton(
                                            "Retry",
                                            visible=is_auto_attaching and is_long_wait,
                                            style=ft.ButtonStyle(
                                                color="#fbbf24",
                                                padding=ft.padding.symmetric(
                                                    horizontal=8,
                                                    vertical=2,
                                                ),
                                            ),
                                            tooltip="Restart auto-attach for this device",
                                            on_click=lambda e, i=inst: page.run_task(
                                                retry_auto_attach_for_instance, i
                                            ),
                                        ),
                                        ft.IconButton(
                                            icon=ft.Icons.STOP_CIRCLE_OUTLINED,
                                            tooltip="Cancel attach",
                                            icon_size=22,
                                            icon_color="#fb7185",
                                            style=ft.ButtonStyle(padding=4),
                                            on_click=lambda e, i=inst: page.run_task(
                                                cancel_attach_for_instance, i
                                            ),
                                        ),
                                    ],
                                    spacing=4,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                ),
                            ),
                        ]
                        if is_attaching
                        else [
                            ft.Container(
                                expand=True,
                                padding=ft.padding.only(left=8),
                                content=ft.Row(
                                    [
                                        ft.Icon(
                                            ft.Icons.ERROR_OUTLINE,
                                            size=16,
                                            color="#fca5a5",
                                        ),
                                        ft.Text(
                                            "Auto-attach failed",
                                            size=10,
                                            color="#fca5a5",
                                            weight=ft.FontWeight.W_500,
                                            no_wrap=True,
                                            tooltip=fail_msg,
                                        ),
                                    ],
                                    spacing=5,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                ),
                            ),
                        ]
                        if is_failed
                        else []
                    ),
                ],
                spacing=0,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                expand=True,
            )

            _remembered_manual_tooltip = (
                "Remove Remember to use this button. While Remember is on, this app "
                "attaches automatically and does not offer manual connect/disconnect."
            )

            row_actions = ft.Row(
                spacing=4,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    dev_distro_dd,
                    ft.FilledIconButton(
                        icon=ft.Icons.USB,
                        tooltip=(
                            _remembered_manual_tooltip
                            if is_rem
                            else "Connect to WSL"
                        ),
                        visible=st in ("available", "shared"),
                        disabled=(
                            st in ("attached", "offline")
                            or is_attaching
                            or is_rem
                        ),
                        icon_size=18,
                        style=ft.ButtonStyle(
                            bgcolor=ACCENT,
                            color="#0f172a",
                            padding=6,
                        ),
                        on_click=lambda e, d=dev: page.run_task(do_connect, d),
                    ),
                    ft.OutlinedIconButton(
                        icon=ft.Icons.LINK_OFF,
                        tooltip=(
                            _remembered_manual_tooltip
                            if is_rem
                            else "Disconnect from WSL and stop sharing (detach + unbind)"
                        ),
                        visible=(
                            st in ("attached", "shared")
                            or (st == "offline" and bool(dev.get("PersistedGuid")))
                        ),
                        disabled=is_attaching or is_rem,
                        icon_size=18,
                        style=ft.ButtonStyle(padding=6),
                        on_click=lambda e, d=dev: page.run_task(
                            do_disconnect, d
                        ),
                    ),
                    ft.IconButton(
                        icon=(
                            ft.Icons.BOOKMARK if is_rem else ft.Icons.BOOKMARK_BORDER
                        ),
                        tooltip="Remember — keep attached to WSL while this app is open",
                        icon_size=20,
                        icon_color=ACCENT if is_rem else "#64748b",
                        on_click=lambda e, i=inst, r=is_rem: page.run_task(
                            toggle_remember, i, not r
                        ),
                        disabled=not inst,
                    ),
                ],
            )

            card = ft.Container(
                content=ft.Row(
                    [
                        ft.Container(
                            width=78,
                            alignment=ft.Alignment.CENTER,
                            padding=ft.padding.symmetric(vertical=4, horizontal=4),
                            content=ft.Text(
                                st_label,
                                size=10,
                                weight=ft.FontWeight.W_600,
                                color=st_color,
                                text_align=ft.TextAlign.CENTER,
                                max_lines=2,
                            ),
                            bgcolor=pill_bg,
                            border_radius=6,
                        ),
                        center_row,
                        row_actions,
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=ft.padding.symmetric(horizontal=10, vertical=6),
                bgcolor="#111827",
                border=ft.border.all(1, "#1f2937"),
                border_radius=8,
            )
            device_list.controls.append(card)

        if initial_device_load_pending:
            initial_device_load_pending = False
        loading_overlay.visible = False
        page.update()

    async def present_usbip_setup_dialog(
        *,
        after_install_refresh: bool = True,
        need_usbipd: bool | None = None,
    ) -> None:
        nonlocal usbipd_exe, initial_device_load_pending
        completed: asyncio.Future[bool] = asyncio.Future()
        setup_success_pending_review = [False]

        if need_usbipd is None:
            need_usbipd = not (
                await asyncio.to_thread(usbipd_cli_works, usbipd_exe)
            )
        forced = _test_setup_dialog_requested()

        if need_usbipd:
            intro_parts: list[ft.Control] = [
                ft.Text(
                    "Installs usbipd-win on Windows with WinGet when needed, then installs "
                    "USB packages in your selected WSL distro via apt (usbutils, "
                    "linux-tools-generic, hwdata).",
                    color="#e2e8f0",
                ),
            ]
        else:
            intro_parts = [
                ft.Text(
                    "Installs USB client packages in your selected WSL distro via apt "
                    "(usbutils, linux-tools-generic, hwdata). usbipd-win is already "
                    "installed on this PC.",
                    color="#e2e8f0",
                ),
            ]
        if forced and not need_usbipd:
            intro_parts.insert(
                0,
                ft.Text(
                    "Test mode: this dialog was forced; the WinGet install step is skipped "
                    "because usbipd already works. A short streamed PowerShell run (including "
                    "winget --version when available) exercises the same log path as WinGet.",
                    color="#fbbf24",
                ),
            )
        if not wsl_distro_names:
            intro_parts.append(
                ft.Text(
                    "No WSL distributions were found. Install a distro (wsl --install) "
                    "and restart this app, or continue to install only usbipd-win.",
                    color="#fb923c",
                ),
            )
        intro_col = ft.Column(intro_parts, spacing=8, tight=True)

        status = ft.Text("", color="#94a3b8", selectable=True)

        log_heading = ft.Text(
            "WSL / apt output (live)",
            color="#64748b",
        )
        stick_install_log = [True]
        _log_append_depth = [0]

        install_log_text = ft.Text(
            "",
            selectable=True,
            font_family="monospace",
            color="#e2e8f0",
            size=12,
            expand=True,
        )

        def on_install_log_scroll(e: ft.OnScrollEvent) -> None:
            if _log_append_depth[0]:
                return
            try:
                m = float(e.max_scroll_extent or 0)
                p = float(e.pixels or 0)
            except (TypeError, ValueError):
                stick_install_log[0] = True
                return
            if m <= 1:
                stick_install_log[0] = True
            else:
                stick_install_log[0] = p >= m - 36

        log_scroll = ft.Column(
            [install_log_text],
            scroll=ft.ScrollMode.AUTO,
            height=120,
            tight=True,
            spacing=0,
            auto_scroll=False,
            on_scroll=on_install_log_scroll,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )

        log_view = ft.Container(
            content=log_scroll,
            bgcolor="#0f172a",
            border=ft.border.all(1, "#334155"),
            border_radius=6,
            padding=ft.padding.all(10),
            expand=True,
        )

        async def _scroll_install_log_to_end() -> None:
            try:
                await log_scroll.scroll_to(offset=-1, duration=0)
            except (OSError, RuntimeError, AttributeError):
                pass

        default_dist = (cfg.get("wsl_distro") or "").strip()
        if default_dist not in wsl_distro_names:
            default_dist = wsl_distro_names[0] if wsl_distro_names else None

        distro_dd = ft.Dropdown(
            label="WSL distro",
            dense=True,
            expand=True,
            border_color="#334155",
            filled=True,
            bgcolor="#1e293b",
            label_style=ft.TextStyle(color="#94a3b8"),
            text_style=ft.TextStyle(color="#f1f5f9"),
            options=[ft.dropdown.Option(n) for n in wsl_distro_names],
            value=default_dist,
            hint_text="No distributions" if not wsl_distro_names else None,
            disabled=not wsl_distro_names,
        )

        skip_btn = ft.TextButton("Skip")

        dlg: ft.AlertDialog | None = None

        async def _apply_setup_success_chrome() -> None:
            assert dlg is not None
            dlg.shape = ft.RoundedRectangleBorder(
                radius=12,
                side=ft.BorderSide(3, "#22c55e"),
            )
            dlg.actions = [install_btn]
            install_btn.content = "Continue"
            install_btn.icon = ft.Icons.CHECK_CIRCLE
            install_btn.disabled = False
            install_btn.style = ft.ButtonStyle(
                bgcolor=ACCENT,
                color="#0f172a",
                icon_color="#0f172a",
                animation_duration=200,
            )
            page.update()
            await asyncio.sleep(0.03)
            install_btn.style = ft.ButtonStyle(
                bgcolor={
                    ft.ControlState.DEFAULT: "#16a34a",
                    ft.ControlState.HOVERED: "#15803d",
                },
                color="#ffffff",
                icon_color="#ffffff",
                overlay_color="#14532d",
                animation_duration=700,
            )
            page.update()

        async def complete_setup_after_install() -> None:
            if not completed.done():
                completed.set_result(True)
            page.pop_dialog()
            page.update()
            if not after_install_refresh:
                return
            loading_overlay.visible = True
            page.update()
            try:
                await rebuild_devices()
            finally:
                loading_overlay.visible = False
                page.update()

        async def do_primary(_: ft.ControlEvent) -> None:
            if setup_success_pending_review[0]:
                await complete_setup_after_install()
                return
            nonlocal usbipd_exe
            cancel_ev = asyncio.Event()
            install_cancel_holder[0] = cancel_ev
            try:
                setup_success_pending_review[0] = False
                install_btn.disabled = True
                status.value = ""
                status.color = "#94a3b8"
                stick_install_log[0] = True
                install_log_text.value = ""
                page.update()
                install_log: list[str] = [""]
                last_log_ui = [0.0]

                def _flush_install_log(*, force: bool = False) -> None:
                    now = time.monotonic()
                    if not force and now - last_log_ui[0] < 0.12:
                        return
                    last_log_ui[0] = now
                    page.update()
                    if stick_install_log[0]:
                        page.run_task(_scroll_install_log_to_end)

                def _append_install_log(delta: str) -> None:
                    _log_append_depth[0] += 1
                    try:
                        if delta:
                            _LOG_SETUP_INSTALL.info("%s", delta)
                        install_log[0] += delta
                        if len(install_log[0]) > 120000:
                            install_log[0] = install_log[0][-100000:]
                        install_log_text.value = install_log[0]
                        _flush_install_log()
                    finally:
                        _log_append_depth[0] -= 1

                install_log_text.value = ""
                install_log[0] = ""
                _LOG_SETUP_INSTALL.info("--- setup install output (stream follows) ---")

                if need_usbipd:
                    if not find_winget():
                        status.value = (
                            "WinGet was not found. Install App Installer from the "
                            "Microsoft Store, then retry."
                        )
                        install_btn.disabled = False
                        skip_btn.disabled = False
                        page.update()
                        return
                    status.value = "Installing usbipd-win via WinGet (see log below)…"
                    _append_install_log("— WinGet (usbipd-win) —\n")
                    page.update()
                    ok, msg = await winget_install_usbipd(
                        on_output_text=_append_install_log,
                        cancel_event=cancel_ev,
                    )
                    _flush_install_log(force=True)
                    if not ok:
                        status.value = (
                            "Installation cancelled."
                            if "[Cancelled.]" in msg
                            else msg
                        )
                        install_btn.disabled = False
                        skip_btn.disabled = False
                        page.update()
                        return
                    usbipd_exe = find_usbipd()
                    if not await asyncio.to_thread(usbipd_cli_works, usbipd_exe):
                        status.value = (
                            "usbipd still does not respond after install. Try restarting "
                            "this app so PATH picks up the new install."
                        )
                        install_btn.disabled = False
                        skip_btn.disabled = False
                        page.update()
                        return

                if forced and not need_usbipd:
                    status.value = (
                        "Test mode: streaming PowerShell (incl. winget --version if available)…"
                    )
                    _append_install_log(
                        "\n— Test: PowerShell stream (WinGet-style code path) —\n"
                    )
                    page.update()
                    ok_ps, msg_ps = await powershell_stream_setup_dialog_test(
                        on_output_text=_append_install_log,
                        cancel_event=cancel_ev,
                    )
                    _flush_install_log(force=True)
                    if not ok_ps:
                        status.value = (
                            "Installation cancelled."
                            if "[Cancelled.]" in msg_ps
                            else msg_ps
                        )
                        install_btn.disabled = False
                        skip_btn.disabled = False
                        page.update()
                        return

                d = (distro_dd.value or "").strip()
                if not d:
                    status.value = "Choose a WSL distro (or install WSL first)."
                    install_btn.disabled = False
                    skip_btn.disabled = False
                    page.update()
                    return

                cfg["wsl_distro"] = d
                save_config(cfg)

                status.value = f"Installing packages in “{d}” (see log below)…"
                _append_install_log("\n— WSL (apt) —\n")
                page.update()

                ok2, msg2 = await wsl_install_usbip_client_packages(
                    d,
                    on_output_text=_append_install_log,
                    cancel_event=cancel_ev,
                )
                _flush_install_log(force=True)
                if not ok2:
                    if "[Cancelled.]" in msg2:
                        status.value = "Installation cancelled."
                    else:
                        status.value = (
                            "Installation failed — see output above. "
                            f"{msg2[:400]}{'…' if len(msg2) > 400 else ''}"
                        )
                    install_btn.disabled = False
                    skip_btn.disabled = False
                    page.update()
                    return

                setup_success_pending_review[0] = True
                status.value = (
                    "Setup completed successfully. Review the log above, then tap Continue."
                )
                status.color = "#86efac"
                await _apply_setup_success_chrome()
            except Exception as ex:  # noqa: BLE001
                status.value = str(ex)
                install_btn.disabled = False
                skip_btn.disabled = False
                page.update()
            finally:
                install_cancel_holder[0] = None

        async def do_skip(_: ft.ControlEvent) -> None:
            ev = install_cancel_holder[0]
            if ev is not None:
                ev.set()
                return
            if setup_success_pending_review[0]:
                await complete_setup_after_install()
                return
            if not completed.done():
                completed.set_result(False)
            page.pop_dialog()
            page.update()

        install_btn = ft.FilledButton(
            content="Install",
            icon=ft.Icons.DOWNLOAD,
            on_click=lambda e: page.run_task(do_primary, e),
            disabled=(not wsl_distro_names) and (not need_usbipd),
        )
        skip_btn.on_click = lambda e: page.run_task(do_skip, e)

        def _on_dismiss(_: ft.ControlEvent) -> None:
            if not completed.done():
                completed.set_result(
                    True if setup_success_pending_review[0] else False
                )

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Set up USB/IP for WSL"),
            content=ft.Container(
                width=520,
                padding=ft.padding.only(bottom=4),
                content=ft.Column(
                    [
                        intro_col,
                        distro_dd,
                        status,
                        log_heading,
                        log_view,
                    ],
                    spacing=10,
                    tight=True,
                    scroll=ft.ScrollMode.HIDDEN,
                    horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                ),
            ),
            actions=[skip_btn, install_btn],
            actions_alignment=ft.MainAxisAlignment.END,
            on_dismiss=_on_dismiss,
        )

        loading_overlay.visible = False
        if initial_device_load_pending:
            initial_device_load_pending = False
        page.show_dialog(dlg)
        page.update()
        await asyncio.sleep(0)
        await completed

    async def apply_remembered(*, quiet: bool = False) -> None:
        if not wsl_distro_names:
            if not quiet:
                show_error("No WSL distributions found. Install WSL or check wsl.exe -l -v.")
            return
        if not remembered_instance_ids(cfg):
            if not quiet:
                show_ok("No remembered devices.")
            return
        await rebuild_devices()
        rc = auto_attach_manager.running_count()
        if not quiet:
            if rc:
                show_ok(
                    f"Background attach active for {rc} remembered device(s)."
                )
            else:
                show_ok(
                    "Remembered devices will attach when present — usually within a few "
                    "seconds of plug-in or state change."
                )

    async def poll_loop() -> None:
        while not poll_stop.is_set():
            await asyncio.sleep(3)
            if poll_stop.is_set():
                break
            devs, err = await asyncio.to_thread(parse_usbipd_state, usbipd_exe)
            if poll_stop.is_set():
                break
            remember_ids = remembered_instance_ids(cfg)
            if not err and devs is not None:
                await asyncio.to_thread(
                    auto_attach_manager.sync,
                    usbipd_exe,
                    remember_ids,
                    devs,
                    distro_for_instance,
                )

            # List UI: refresh on a timer when auto-refresh is on, or whenever there
            # are remembered devices (AGENTS.md — discovery without manual refresh).
            refresh_list_ui = auto_refresh_sw.value or bool(remember_ids)
            if not refresh_list_ui:
                if not err and devs is not None:
                    status_text.value = (
                        f"{len(devs)} device(s) — usbipd at {usbipd_exe}"
                    )
                page.update()
                continue

            if err or devs is None:
                if poll_stop.is_set():
                    break
                await rebuild_devices((devs, err))
                continue

            order = cfg.get("sort_order") or sort_dd.value or "state_attached_first"
            auto_busy = auto_attach_manager.instance_ids_attaching(
                devs, remember_ids
            )
            auto_failed = auto_attach_manager.failed_instance_ids()
            auto_long_wait = auto_attach_manager.instance_ids_long_waiting(
                devs,
                remember_ids,
            )
            new_fp = _device_list_fingerprint(
                devs,
                order,
                cfg,
                manual_attaching=manual_attaching,
                auto_attaching_ids=auto_busy,
                auto_failed_ids=auto_failed,
                auto_long_wait_ids=auto_long_wait,
            )
            if (
                last_list_fingerprint is not None
                and new_fp == last_list_fingerprint
            ):
                status_text.value = f"{len(devs)} device(s) — usbipd at {usbipd_exe}"
                page.update()
            else:
                if poll_stop.is_set():
                    break
                await rebuild_devices((devs, err))

    def on_sort_change(e: ft.ControlEvent) -> None:
        new_order = (
            getattr(e.control, "value", None)
            or (e.data if isinstance(getattr(e, "data", None), str) else None)
            or sort_dd.value
            or "state_attached_first"
        )
        cfg["sort_order"] = new_order
        save_config(cfg)
        page.run_task(rebuild_devices)

    sort_dd.on_change = on_sort_change
    remember_startup.on_change = lambda _: (persist_cfg())
    auto_refresh_sw.on_change = lambda _: (persist_cfg())

    def on_minimize_tray_change(_: ft.ControlEvent) -> None:
        persist_cfg()
        page.window.prevent_close = True
        if not minimize_to_tray_sw.value:
            tray.stop()
            page.window.visible = True
            page.window.skip_task_bar = False
        page.update()

    minimize_to_tray_sw.on_change = on_minimize_tray_change

    _start_win_suppress = [False]

    def on_start_with_windows_change(_: ft.ControlEvent) -> None:
        if _start_win_suppress[0]:
            return
        want = start_with_windows_sw.value
        ok, err = set_run_at_logon(want)
        if not ok:
            _start_win_suppress[0] = True
            start_with_windows_sw.value = not want
            _start_win_suppress[0] = False
            show_error(err or "Could not update Windows startup setting.")
        page.update()

    start_with_windows_sw.on_change = on_start_with_windows_change

    _caption_style = ft.ButtonStyle(
        color="#cbd5e1",
        bgcolor=ft.Colors.TRANSPARENT,
        overlay_color="#334155",
        padding=ft.padding.symmetric(horizontal=10, vertical=4),
    )
    _caption_close_style = ft.ButtonStyle(
        color="#cbd5e1",
        bgcolor=ft.Colors.TRANSPARENT,
        overlay_color="#7f1d1d",
        padding=ft.padding.symmetric(horizontal=10, vertical=4),
    )

    maximize_btn = ft.IconButton(
        icon=ft.Icons.OPEN_IN_FULL,
        icon_size=18,
        tooltip="Maximize",
        style=_caption_style,
    )

    def sync_caption_icons() -> None:
        try:
            mx = bool(page.window.maximized)
        except (TypeError, ValueError):
            mx = False
        maximize_btn.icon = ft.Icons.FULLSCREEN_EXIT if mx else ft.Icons.OPEN_IN_FULL
        maximize_btn.tooltip = "Restore" if mx else "Maximize"

    def on_caption_minimize(_: ft.ControlEvent) -> None:
        page.window.minimized = True
        page.update()

    def toggle_window_maximized() -> None:
        try:
            page.window.maximized = not bool(page.window.maximized)
        except (TypeError, ValueError):
            return
        sync_caption_icons()
        page.update()

    def on_caption_maximize(_: ft.ControlEvent) -> None:
        toggle_window_maximized()

    async def on_caption_close() -> None:
        if minimize_to_tray_sw.value:
            await hide_to_tray()
        else:
            await exit_from_tray()

    minimize_btn = ft.IconButton(
        icon=ft.Icons.MINIMIZE,
        icon_size=18,
        tooltip="Minimize",
        style=_caption_style,
        on_click=on_caption_minimize,
    )
    maximize_btn.on_click = on_caption_maximize
    close_btn = ft.IconButton(
        icon=ft.Icons.CLOSE,
        icon_size=18,
        tooltip="Close",
        style=_caption_close_style,
        on_click=lambda _: page.run_task(on_caption_close),
    )

    title_drag = ft.WindowDragArea(
        expand=True,
        maximizable=False,
        content=ft.GestureDetector(
            expand=True,
            mouse_cursor=ft.MouseCursor.BASIC,
            on_double_tap=lambda _: toggle_window_maximized(),
            content=ft.Container(
                padding=ft.padding.only(left=12, right=8, top=6, bottom=6),
                expand=True,
                content=ft.Row(
                    [
                        title_heading,
                        status_text,
                    ],
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    expand=True,
                ),
            ),
        ),
    )
    caption_controls = ft.Row(
        [minimize_btn, maximize_btn, close_btn],
        spacing=0,
        tight=True,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )
    top_bar = ft.Container(
        content=ft.Row(
            [title_drag, caption_controls],
            spacing=0,
            expand=True,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        bgcolor="#0f172a",
        border=ft.border.only(bottom=ft.BorderSide(1, "#1e293b")),
        padding=ft.padding.only(right=4),
    )

    list_padded = ft.Container(
        content=device_list_stack,
        padding=ft.padding.symmetric(horizontal=8),
        expand=True,
    )
    body = ft.Container(
        content=ft.Column(
            [
                top_bar,
                list_padded,
            ],
            expand=True,
            spacing=4,
        ),
        padding=ft.padding.only(bottom=8),
        expand=True,
    )

    _refresh_style = ft.ButtonStyle(bgcolor="#334155", color="#f1f5f9")
    _apply_style = ft.ButtonStyle(bgcolor="#0f766e", color="#ecfdf5")

    async def open_usbip_setup_from_footer() -> None:
        ok_host = await asyncio.to_thread(usbipd_cli_works, usbipd_exe)
        await present_usbip_setup_dialog(
            after_install_refresh=True,
            need_usbipd=not ok_host,
        )

    btn_install = ft.FilledButton(
        content="Install",
        icon=ft.Icons.DOWNLOAD,
        tooltip="Install usbipd-win and/or WSL USB client packages (apt)",
        style=_refresh_style,
        on_click=lambda _: page.run_task(open_usbip_setup_from_footer),
    )
    btn_install_icon = ft.FilledIconButton(
        icon=ft.Icons.DOWNLOAD,
        tooltip="Install usbipd-win and/or WSL USB client packages (apt)",
        style=_refresh_style,
        icon_size=20,
        on_click=lambda _: page.run_task(open_usbip_setup_from_footer),
        visible=False,
    )
    btn_refresh = ft.FilledButton(
        content="Refresh",
        icon=ft.Icons.REFRESH,
        tooltip="Refresh device list",
        style=_refresh_style,
        on_click=lambda _: page.run_task(rebuild_devices),
    )
    btn_refresh_icon = ft.FilledIconButton(
        icon=ft.Icons.REFRESH,
        tooltip="Refresh device list",
        style=_refresh_style,
        icon_size=20,
        on_click=lambda _: page.run_task(rebuild_devices),
        visible=False,
    )
    btn_apply = ft.FilledButton(
        content="Apply remembered",
        icon=ft.Icons.BOOKMARK_ADD,
        tooltip="Connect all remembered devices",
        style=_apply_style,
        on_click=lambda _: page.run_task(apply_remembered),
    )
    btn_apply_icon = ft.FilledIconButton(
        icon=ft.Icons.BOOKMARK_ADD,
        tooltip="Connect all remembered devices",
        style=_apply_style,
        icon_size=20,
        on_click=lambda _: page.run_task(apply_remembered),
        visible=False,
    )
    def sync_footer_layout() -> None:
        try:
            w = float(page.window.width)
        except (TypeError, ValueError):
            w = 2000.0
        compact = w < FOOTER_COMPACT_BREAKPOINT_PX
        btn_install.visible = not compact
        btn_install_icon.visible = compact
        btn_refresh.visible = not compact
        btn_refresh_icon.visible = compact
        btn_apply.visible = not compact
        btn_apply_icon.visible = compact

    def on_window_event(e: ft.WindowEvent) -> None:
        t = e.type
        if t == ft.WindowEventType.CLOSE or t == "close":
            if minimize_to_tray_sw.value:
                page.run_task(hide_to_tray)
                return
            page.run_task(exit_from_tray)
            return
        if t == ft.WindowEventType.MINIMIZE or t == "minimize":
            if minimize_to_tray_sw.value:
                page.run_task(hide_to_tray)
            return
        if t == ft.WindowEventType.RESIZED or t == "resized":
            sync_footer_layout()
            sync_caption_icons()
            page.update()
            return
        if t in (
            ft.WindowEventType.MAXIMIZE,
            "maximize",
            ft.WindowEventType.UNMAXIMIZE,
            "unmaximize",
        ):
            sync_caption_icons()
            page.update()
            return

    footer_actions = ft.Row(
        [
            btn_install,
            btn_install_icon,
            btn_refresh,
            btn_refresh_icon,
            btn_apply,
            btn_apply_icon,
            col_remember_switch,
            col_auto_switch,
            col_tray_switch,
            col_start_win_switch,
        ],
        spacing=8,
        tight=True,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )

    footer_row = ft.Row(
        [
            sort_dd,
            footer_actions,
        ],
        spacing=8,
        wrap=False,
        tight=False,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
        expand=True,
    )

    footer = ft.Container(
        content=footer_row,
        padding=ft.padding.symmetric(horizontal=16, vertical=10),
        bgcolor="#0f172a",
        border=ft.border.only(top=ft.BorderSide(1, "#1e293b")),
    )

    page.add(
        ft.Column(
            [
                body,
                footer,
            ],
            expand=True,
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
    )

    page.window.prevent_close = True
    page.window.on_event = on_window_event
    sync_footer_layout()
    sync_caption_icons()

    await refresh_distros()
    test_forced = _test_setup_dialog_requested()
    usbip_ok = await asyncio.to_thread(usbipd_cli_works, usbipd_exe)
    _no_wsl = not wsl_distro_names
    btn_install.disabled = _no_wsl and usbip_ok
    btn_install_icon.disabled = btn_install.disabled
    page.update()
    if (not usbip_ok) or test_forced:
        await present_usbip_setup_dialog(
            after_install_refresh=False,
            need_usbipd=not usbip_ok,
        )
    loading_overlay.visible = True
    page.update()
    fw_ok = True
    fw_err = ""
    try:
        fw_ok, fw_err = await apply_wsl_public_profile_firewall_fix_async()
        await rebuild_devices()
    finally:
        loading_overlay.visible = False
        page.update()
    if not fw_ok:
        show_error(
            "Could not adjust the Public firewall profile for WSL vEthernet adapters. "
            "usbipd attach may hang or warn about port 3240 until this is fixed.\n\n"
            f"Detail: {fw_err}"
        )

    if (
        cfg.get("apply_on_startup")
        and remembered_instance_ids(cfg)
        and wsl_distro_names
    ):
        await apply_remembered(quiet=True)

    page.run_task(poll_loop)

    async def on_disconnect(_: ft.ControlEvent) -> None:
        if _shutdown_started[0]:
            return
        await full_shutdown(yield_to_replacement=False)

    page.on_disconnect = on_disconnect
