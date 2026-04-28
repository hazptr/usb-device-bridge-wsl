from __future__ import annotations

import asyncio
import atexit
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal

import flet as ft

from usb_device_bridge.app_logging import install_asyncio_exception_logging
from usb_device_bridge.auto_attach import AutoAttachManager
from usb_device_bridge.config import (
    FIREWALL_FIX_POLICY_ALWAYS,
    FIREWALL_FIX_POLICY_ASK,
    FIREWALL_FIX_POLICY_NEVER,
    app_data_dir,
    default_config,
    load_config,
    prune_device_entry_if_unused,
    remembered_instance_ids,
    save_config,
)
from usb_device_bridge.firewall import apply_wsl_public_profile_firewall_fix_async
from usb_device_bridge.single_instance import (
    release_singleton_mutex_for_handoff,
    set_focus_handler,
    set_yield_handler,
)
from usb_device_bridge.ui.helpers import (
    assets_dir,
    device_list_fingerprint,
    test_first_time_setup_requested,
)
from usb_device_bridge.ui.startup.shell import (
    SetupShell,
    SetupStepRegistration,
)
from usb_device_bridge.ui.startup.theme_prompt import (
    build_theme_step_content,
    calculate_theme_step_preferred_size,
)
from usb_device_bridge.ui.startup.usb_prompt import (
    build_usb_step_content,
    calculate_usb_step_preferred_size,
    on_usb_step_leave,
)
from usb_device_bridge.ui.startup.preferences_prompt import (
    build_preferences_step_content,
    calculate_preferences_step_preferred_size,
)
from usb_device_bridge.ui.theme_picker import ThemeDropdownSelector
from usb_device_bridge.ui.settings_panel import create_settings_panel
from usb_device_bridge.ui.theme import AppTheme, ThemeManager
from usb_device_bridge.ui.tray import TrayManager
from usb_device_bridge.updater import (
    check_for_available_update,
    download_update_installer,
)
from usb_device_bridge.usbipd import (
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
from usb_device_bridge.version_info import (
    get_app_version,
    get_display_version,
    is_dev_source_launch,
)
from usb_device_bridge.windows.startup import (
    can_configure_run_at_logon,
    is_run_at_logon_enabled,
    set_run_at_logon,
)
from usb_device_bridge.wsl import parse_wsl_distros

_LOG_SETUP_INSTALL = logging.getLogger(__name__ + ".setup_install")


async def run_app(page: ft.Page) -> None:
    install_asyncio_exception_logging()
    _display_version = get_display_version()
    _is_dev_or_dirty_build = is_dev_source_launch() or (
        "-dirty" in _display_version.lower()
    )
    _win_title = f"USB Device Bridge for WSL — {_display_version}"
    page.title = _win_title
    ico = assets_dir() / "app_icon.ico"
    if ico.is_file():
        page.window.icon = str(ico)
    page.window.width = 980
    page.window.height = 720
    page.window.min_width = 720
    page.window.min_height = 520
    page.window.title_bar_hidden = True
    page.padding = 0

    # Load config and initialize theme system
    cfg = load_config()
    test_first_time_setup = test_first_time_setup_requested()
    _saved_theme = cfg.get("theme", "dark")
    theme_manager = ThemeManager(page, initial_theme=_saved_theme)
    theme = theme_manager.current_theme

    # Apply theme to page
    page.theme_mode = (
        ft.ThemeMode.DARK if theme.name == "dark" else ft.ThemeMode.LIGHT
    )
    page.bgcolor = theme.page_bg

    ACCENT = theme.accent

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
    auto_update_sw = ft.Switch(
        label="",
        value=False if _is_dev_or_dirty_build else cfg.get("auto_update", True),
        active_color=ACCENT,
        disabled=_is_dev_or_dirty_build,
    )

    minimize_to_tray_sw = ft.Switch(
        label="",
        value=cfg.get("minimize_to_tray", False),
        active_color=ACCENT,
    )

    _start_win_available = can_configure_run_at_logon()
    start_with_windows_sw = ft.Switch(
        label="",
        value=is_run_at_logon_enabled() if _start_win_available else False,
        active_color=ACCENT,
        disabled=not _start_win_available,
    )

    sort_dd = ft.Dropdown(
        label="Sort",
        width=230,
        dense=True,
        border_color=theme.input_border,
        filled=True,
        bgcolor=theme.input_bg,
        label_style=ft.TextStyle(color=theme.input_label, size=12),
        text_style=ft.TextStyle(color=theme.text_primary, size=13),
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

    status_text = ft.Text("", color=theme.text_muted, size=11, expand=True)
    title_heading = ft.Text(
        _win_title,
        size=13,
        weight=ft.FontWeight.W_600,
        color=theme.text_primary,
    )
    device_list = ft.Column(spacing=4, scroll=ft.ScrollMode.AUTO, expand=True)
    loading_overlay = ft.Container(
        visible=True,
        expand=True,
        bgcolor=theme.page_bg,
        alignment=ft.Alignment.CENTER,
        content=ft.Column(
            [
                ft.ProgressRing(width=44, height=44, stroke_width=3),
                ft.Text(
                    "Loading devices…",
                    size=13,
                    color=theme.text_muted,
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
        color=theme.text_muted,
    )
    shutdown_overlay = ft.Container(
        visible=False,
        expand=True,
        bgcolor=theme.page_bg,
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
    auto_update_inflight = [False]
    update_prompted_versions: set[str] = set()
    auto_attach_manager = AutoAttachManager(
        firewall_fix_policy_provider=lambda: (
            cfg.get("firewall_fix_policy") or FIREWALL_FIX_POLICY_ASK
        )
    )
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
        release_singleton_mutex_for_handoff()
        await full_shutdown(yield_to_replacement=True)

    async def prompt_update_ready(version: str, installer_path: Path) -> None:
        if not version or version in update_prompted_versions:
            return
        update_prompted_versions.add(version)

        completed: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        def _finish(install_now: bool) -> None:
            if completed.done():
                return
            completed.set_result(install_now)
            dlg.open = False
            page.update()

        def _on_dismiss(_: ft.ControlEvent) -> None:
            _finish(False)

        try:
            page.window.minimized = False
            page.window.visible = True
            page.window.skip_task_bar = False
            await page.window.to_front()
        except Exception:  # pragma: no cover - UI capability varies by host
            pass

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Update Ready", color=theme.text_primary),
            content=ft.Column(
                [
                    ft.Text(
                        f"Version {version} has finished downloading.",
                        color=theme.text_secondary,
                    ),
                    ft.Text(
                        "Install now? The app will close while the installer runs.",
                        color=theme.text_muted,
                        size=12,
                    ),
                    ft.Text(
                        str(installer_path),
                        color=theme.text_muted,
                        size=11,
                        selectable=True,
                    ),
                ],
                spacing=6,
                tight=True,
            ),
            actions=[
                ft.TextButton("Later", on_click=lambda _: _finish(False)),
                ft.FilledButton("Install update", on_click=lambda _: _finish(True)),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            on_dismiss=_on_dismiss,
        )
        page.show_dialog(dlg)
        page.update()

        install_now = await completed
        if not install_now or _shutdown_started[0]:
            return
        try:
            os.startfile(str(installer_path))
        except OSError as ex:
            show_error(f"Could not launch installer: {ex}")
            return
        await full_shutdown(yield_to_replacement=False)

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
        cfg["auto_update"] = auto_update_sw.value
        cfg["auto_refresh"] = auto_refresh_sw.value
        cfg["minimize_to_tray"] = minimize_to_tray_sw.value
        cfg["sort_order"] = sort_dd.value or "state_attached_first"
        save_config(cfg)

    def show_error(msg: str) -> None:
        page.snack_bar = ft.SnackBar(
            content=ft.Text(msg, color=theme.error),
            bgcolor=theme.error_bg,
        )
        page.snack_bar.open = True
        page.update()

    def show_ok(msg: str) -> None:
        page.snack_bar = ft.SnackBar(
            content=ft.Text(msg, color=theme.success),
            bgcolor=theme.success_bg,
        )
        page.snack_bar.open = True
        page.update()

    _firewall_prompt_lock = asyncio.Lock()

    def _firewall_fix_policy() -> str:
        raw = (cfg.get("firewall_fix_policy") or FIREWALL_FIX_POLICY_ASK).strip().lower()
        if raw in (
            FIREWALL_FIX_POLICY_ASK,
            FIREWALL_FIX_POLICY_ALWAYS,
            FIREWALL_FIX_POLICY_NEVER,
        ):
            return raw
        return FIREWALL_FIX_POLICY_ASK

    def _set_firewall_fix_policy(policy: str) -> None:
        cfg["firewall_fix_policy"] = policy
        save_config(cfg)

    async def ask_firewall_fix_consent(
        *,
        reason_text: str,
        title: str,
        detail: str,
    ) -> tuple[bool, bool]:
        async with _firewall_prompt_lock:
            policy = _firewall_fix_policy()
            if policy == FIREWALL_FIX_POLICY_ALWAYS:
                return True, False
            if policy == FIREWALL_FIX_POLICY_NEVER:
                return False, False

            completed: asyncio.Future[tuple[bool, bool]] = asyncio.get_running_loop().create_future()
            remember_cb = ft.Checkbox(
                label="Remember my decision",
                value=False,
                active_color=ACCENT,
            )
            clipped_reason = reason_text[:1200] + ("..." if len(reason_text) > 1200 else "")

            def _finish(allow: bool) -> None:
                if completed.done():
                    return
                completed.set_result((allow, bool(remember_cb.value)))
                dlg.open = False
                page.update()

            def _on_dismiss(_: ft.ControlEvent) -> None:
                _finish(False)

            # Auto-attach prompts can fire while the window is hidden to tray.
            # Bring the app to foreground so the consent dialog is actually visible.
            try:
                page.window.minimized = False
                page.window.visible = True
                page.window.skip_task_bar = False
                await page.window.to_front()
            except Exception:  # pragma: no cover - UI capability varies by host
                pass

            dlg = ft.AlertDialog(
                modal=True,
                title=ft.Text(title, color=theme.text_primary),
                content=ft.Column(
                    [
                        ft.Text(
                            detail,
                            color=theme.text_secondary,
                            size=12,
                        ),
                        ft.Container(height=4),
                        ft.Text(
                            "usbipd output:",
                            color=theme.text_muted,
                            size=11,
                        ),
                        ft.Text(
                            clipped_reason or "(no details)",
                            color=theme.text_secondary,
                            size=11,
                            selectable=True,
                        ),
                        ft.Container(height=8),
                        remember_cb,
                    ],
                    spacing=6,
                    tight=True,
                ),
                actions=[
                    ft.TextButton("Not now", on_click=lambda _: _finish(False)),
                    ft.FilledButton("Allow", on_click=lambda _: _finish(True)),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
                on_dismiss=_on_dismiss,
            )
            page.show_dialog(dlg)
            page.update()
            return await completed

    async def handle_auto_attach_firewall_prompts(devs: list[dict[str, Any]]) -> None:
        pending = auto_attach_manager.consume_firewall_prompt_requests()
        if not pending:
            return
        logging.getLogger(__name__).warning(
            "Auto-attach firewall consent prompts pending: count=%s instance_ids=%s",
            len(pending),
            [inst for inst, _msg in pending],
        )
        by_inst = {
            d.get("InstanceId") or "": d
            for d in devs
            if d.get("InstanceId")
        }
        for inst, reason in pending:
            dev = by_inst.get(inst) or {}
            desc = dev.get("Description") or "remembered device"
            bus_id = dev.get("BusId") or "unknown"
            logging.getLogger(__name__).warning(
                "Showing firewall consent prompt for auto-attach "
                "(instance_id=%s bus_id=%s desc=%s)",
                inst,
                bus_id,
                desc,
            )
            allow, remember = await ask_firewall_fix_consent(
                reason_text=reason,
                title="Firewall Is Blocking USB Attach",
                detail=(
                    f"{desc} (Bus {bus_id}) could not attach to WSL because Windows "
                    "firewall settings appear to block usbipd communication.\n\n"
                    "Allow this app to adjust the Public firewall profile for WSL "
                    "vEthernet interfaces now?"
                ),
            )
            if remember:
                _set_firewall_fix_policy(
                    FIREWALL_FIX_POLICY_ALWAYS if allow else FIREWALL_FIX_POLICY_NEVER
                )
            logging.getLogger(__name__).warning(
                "Firewall consent prompt decision for auto-attach "
                "(instance_id=%s allow=%s remember=%s)",
                inst,
                allow,
                remember,
            )
            if not allow:
                continue
            fix_ok, fix_err = await apply_wsl_public_profile_firewall_fix_async()
            if not fix_ok:
                show_error(
                    "Could not apply the TCP 3240 firewall rule for WSL vEthernet.\n\n"
                    "Please configure Windows Firewall manually, then try again.\n\n"
                    f"Detail: {fix_err}"
                )
                continue
            auto_attach_manager.retry_background_attach(inst)
            show_ok("Firewall updated. Retrying remembered-device attach.")

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

    _auto_fail_prompt_lock = asyncio.Lock()

    async def _prompt_auto_attach_failure(
        instance_id: str,
        description: str,
        bus_id: str,
        detail: str,
    ) -> None:
        """Show a dialog when auto-attach gives up, offering to un-remember."""
        async with _auto_fail_prompt_lock:
            completed: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

            def _finish(remove: bool) -> None:
                if completed.done():
                    return
                completed.set_result(remove)
                dlg.open = False
                page.update()

            def _on_dismiss(_: ft.ControlEvent) -> None:
                _finish(False)

            try:
                page.window.minimized = False
                page.window.visible = True
                page.window.skip_task_bar = False
                await page.window.to_front()
            except Exception:
                pass

            dlg = ft.AlertDialog(
                modal=True,
                title=ft.Text("Auto-Attach Failed", color=theme.text_primary),
                content=ft.Column(
                    [
                        ft.Text(
                            f"{description} (Bus {bus_id}) could not be attached "
                            f"to WSL after multiple attempts.",
                            color=theme.text_secondary,
                            size=12,
                        ),
                        ft.Container(height=4),
                        ft.Text(
                            detail or "The device may not be responding correctly.",
                            color=theme.text_muted,
                            size=11,
                            selectable=True,
                        ),
                        ft.Container(height=8),
                        ft.Text(
                            "Would you like to remove it from your remembered "
                            "devices, or keep it and retry later?",
                            color=theme.text_secondary,
                            size=12,
                        ),
                    ],
                    spacing=6,
                    tight=True,
                ),
                actions=[
                    ft.TextButton(
                        "Keep & Retry",
                        on_click=lambda _: _finish(False),
                    ),
                    ft.FilledButton(
                        "Remove",
                        on_click=lambda _: _finish(True),
                        color=theme.error,
                        bgcolor=theme.error_bg,
                    ),
                ],
                actions_alignment=ft.MainAxisAlignment.END,
                on_dismiss=_on_dismiss,
            )
            page.show_dialog(dlg)
            page.update()
            remove = await completed

            if remove:
                ent = cfg.setdefault("devices", {}).get(instance_id)
                if isinstance(ent, dict):
                    ent.pop("remembered", None)
                    prune_device_entry_if_unused(cfg, instance_id)
                save_config(cfg)
                auto_attach_manager.cancel_background_attach(instance_id)
                show_ok(f"Removed {description} from remembered devices.")
            else:
                auto_attach_manager.retry_background_attach(instance_id)
                show_ok(f"Will retry auto-attach for {description}.")

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
                    content=ft.Text(err, color=theme.error),
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
        await handle_auto_attach_firewall_prompts(devs)

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
            seen_auto_failures.update(new_auto_failures)
            by_inst_map = {
                d.get("InstanceId") or "": d for d in devs if d.get("InstanceId")
            }
            for inst in sorted(new_auto_failures):
                dev_info = by_inst_map.get(inst) or {}
                desc = dev_info.get("Description") or "remembered device"
                bus = dev_info.get("BusId") or "unknown"
                detail = auto_attach_manager.failure_for_instance(inst) or ""
                await _prompt_auto_attach_failure(inst, desc, bus, detail)

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

                async def _request_firewall_fix_for_manual_attach(
                    reason_text: str,
                ) -> tuple[bool, bool]:
                    allow, remember = await ask_firewall_fix_consent(
                        reason_text=reason_text,
                        title="Firewall Is Blocking USB Attach",
                        detail=(
                            "Device attachment to WSL appears blocked by Windows firewall "
                            "settings for the WSL vEthernet interface.\n\n"
                            "Allow this app to adjust that firewall setting now?"
                        ),
                    )
                    if remember:
                        _set_firewall_fix_policy(
                            FIREWALL_FIX_POLICY_ALWAYS
                            if allow
                            else FIREWALL_FIX_POLICY_NEVER
                        )
                    return allow, remember

                ok, msg = await connect_to_wsl(
                    usbipd_exe,
                    d,
                    dev,
                    auto_attach=False,
                    cancel_event=cancel_ev,
                    firewall_fix_policy=_firewall_fix_policy(),
                    request_firewall_fix_consent=_request_firewall_fix_for_manual_attach,
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
        last_list_fingerprint = device_list_fingerprint(
            devs,
            order,
            cfg,
            manual_attaching=manual_attaching,
            auto_attaching_ids=auto_busy_ids,
            auto_failed_ids=auto_failed_ids,
            auto_long_wait_ids=auto_long_wait_ids,
        )
        # A manual refresh can overlap poll-triggered rebuild; clear again at render
        # time so interleaved runs do not append duplicate rows.
        device_list.controls.clear()
        for dev in sort_devices_list(devs, order, cfg.get("device_recency") or {}):
            desc = dev.get("Description") or "(no description)"
            bus = dev.get("BusId") or "—"
            inst = dev.get("InstanceId") or ""
            st = classify(dev)
            if st == "attached":
                st_label = "Attached"
                st_color = theme.pill_attached_text
            elif st == "shared":
                st_label = "Shared"
                st_color = theme.pill_shared_text
            elif st == "available":
                st_label = "Available"
                st_color = theme.pill_available_text
            else:
                st_label = "Offline / persisted"
                st_color = theme.pill_offline_text

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
                "attached": theme.pill_attached_bg,
                "shared": theme.pill_shared_bg,
                "available": theme.pill_available_bg,
                "offline": theme.pill_offline_bg,
            }.get(st, theme.card_bg)

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
                border_color=theme.input_border,
                filled=True,
                bgcolor=theme.input_bg,
                label_style=ft.TextStyle(color=theme.input_label, size=11),
                text_style=ft.TextStyle(color=theme.text_primary, size=12),
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
                        color=theme.text_primary,
                        max_lines=1,
                        overflow=ft.TextOverflow.ELLIPSIS,
                    ),
                    ft.Text(
                        f"Bus {bus}  ·  {vp}",
                        size=11,
                        color=theme.text_muted,
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
                                            bgcolor=theme.input_bg,
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
                                            color=theme.warning,
                                            weight=ft.FontWeight.W_500,
                                            no_wrap=True,
                                        ),
                                        ft.TextButton(
                                            "Retry",
                                            visible=is_auto_attaching and is_long_wait,
                                            style=ft.ButtonStyle(
                                                color=theme.warning,
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
                                            icon_color=theme.error,
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
                                            color=theme.error,
                                        ),
                                        ft.Text(
                                            "Auto-attach failed",
                                            size=10,
                                            color=theme.error,
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
                            color=theme.text_on_accent,
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
                        icon_color=ACCENT if is_rem else theme.text_muted,
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
                bgcolor=theme.card_bg,
                border=ft.border.all(1, theme.border_subtle),
                border_radius=8,
            )
            device_list.controls.append(card)

        if initial_device_load_pending:
            initial_device_load_pending = False
        loading_overlay.visible = False
        page.update()


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

    async def run_auto_update_check(manual: bool = False) -> None:
        if auto_update_inflight[0]:
            if manual:
                show_ok("Update check is already running.")
            return
        if (not manual) and (not auto_update_sw.value):
            return
        if _is_dev_or_dirty_build:
            if manual:
                show_ok("Update checks are disabled for dev/dirty builds.")
            return
        if not getattr(sys, "frozen", False):
            if manual:
                show_ok("Update checks are only available in installed app builds.")
            return
        if _shutdown_started[0]:
            return

        auto_update_inflight[0] = True
        try:
            current_version = get_app_version()
            available = await asyncio.to_thread(
                check_for_available_update,
                current_version,
            )
            if available is None or _shutdown_started[0]:
                if manual:
                    show_ok("No updates available.")
                return

            downloaded = await asyncio.to_thread(
                download_update_installer,
                available,
                target_dir=app_data_dir() / "updates",
            )
            if downloaded is None or _shutdown_started[0]:
                if manual:
                    show_error("Could not download the update installer.")
                return

            await prompt_update_ready(downloaded.version, downloaded.installer_path)
        except Exception:  # noqa: BLE001
            logging.getLogger(__name__).exception("Auto-update check failed")
            if manual:
                show_error("Update check failed. See app.log for details.")
        finally:
            auto_update_inflight[0] = False

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
                await handle_auto_attach_firewall_prompts(devs)

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
            new_fp = device_list_fingerprint(
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
    auto_update_sw.on_change = lambda _: (persist_cfg(), page.run_task(run_auto_update_check))

    def on_auto_refresh_change(_: ft.ControlEvent) -> None:
        persist_cfg()
        sync_header_action_buttons()
        page.update()

    auto_refresh_sw.on_change = on_auto_refresh_change

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
        color=theme.text_secondary,
        bgcolor=ft.Colors.TRANSPARENT,
        overlay_color=theme.button_bg,
        padding=ft.padding.symmetric(horizontal=10, vertical=4),
    )
    _caption_close_style = ft.ButtonStyle(
        color=theme.text_secondary,
        bgcolor=ft.Colors.TRANSPARENT,
        overlay_color=theme.error_bg,
        padding=ft.padding.symmetric(horizontal=10, vertical=4),
    )
    _settings_caption_style_closed = ft.ButtonStyle(
        color=theme.text_secondary,
        bgcolor=ft.Colors.TRANSPARENT,
        overlay_color=theme.button_bg,
        padding=ft.padding.symmetric(horizontal=10, vertical=4),
        animation_duration=220,
    )
    _settings_caption_style_open = ft.ButtonStyle(
        color=theme.tab_active_text,
        bgcolor=theme.tab_active_bg,
        overlay_color=theme.accent_hover,
        padding=ft.padding.symmetric(horizontal=10, vertical=4),
        animation_duration=220,
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
    refresh_header_btn = ft.IconButton(
        icon=ft.Icons.REFRESH,
        icon_size=18,
        tooltip="Refresh device list",
        style=_caption_style,
        on_click=lambda _: page.run_task(rebuild_devices),
    )
    settings_header_btn = ft.IconButton(
        icon=ft.Icons.SETTINGS,
        icon_size=18,
        tooltip="Show settings",
        style=_settings_caption_style_closed,
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
        [refresh_header_btn, settings_header_btn, minimize_btn, maximize_btn, close_btn],
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
        bgcolor=theme.surface_bg,
        border=ft.border.only(bottom=ft.BorderSide(1, theme.border_subtle)),
        padding=ft.padding.only(right=4),
    )

    list_padded = ft.Container(
        content=device_list_stack,
        padding=ft.padding.only(left=8, right=8, top=8),
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
        padding=ft.padding.only(bottom=0),
        expand=True,
    )

    async def open_usbip_setup_from_footer() -> None:
        ok_host = await asyncio.to_thread(usbipd_cli_works, usbipd_exe)
        ad_hoc_cancel: list[asyncio.Event | None] = [None]

        def _update_usbipd_exe_ad_hoc(exe: str) -> None:
            nonlocal usbipd_exe
            usbipd_exe = exe

        shell = SetupShell(page, theme, theme_manager, overlay_host=content_host)
        await shell.run(
            steps=[
                SetupStepRegistration(
                    key="usb_adhoc",
                    should_show=lambda: True,
                    initial_completed=lambda: False,
                    size_resolver=lambda page_obj, _content: calculate_usb_step_preferred_size(
                        page_obj,
                        test_first_time_setup=False,
                        need_usbipd=not ok_host,
                        has_wsl_distros=bool(wsl_distro_names),
                    ),
                    build_content=lambda ctx: build_usb_step_content(
                        ctx,
                        page=page,
                        cfg=cfg,
                        wsl_distro_names=wsl_distro_names,
                        test_first_time_setup=False,
                        install_cancel_holder=ad_hoc_cancel,
                        need_usbipd=not ok_host,
                        on_usbipd_updated=_update_usbipd_exe_ad_hoc,
                    ),
                    on_leave=lambda direction: on_usb_step_leave(
                        direction, install_cancel_holder=ad_hoc_cancel
                    ),
                ),
            ],
        )
        await rebuild_devices()

    def _build_relaunch_command() -> tuple[list[str], str | None]:
        if getattr(sys, "frozen", False):
            return [sys.executable], None

        entry = Path(sys.argv[0]).resolve() if sys.argv else None
        if entry is not None and entry.is_file() and entry.suffix.lower() == ".py":
            return [sys.executable, str(entry)], str(entry.parent)

        return [sys.executable, "-m", "usb_device_bridge"], str(Path.cwd())

    async def reset_preferences_and_relaunch() -> None:
        if _shutdown_started[0]:
            return

        completed: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        def _finish(confirm: bool) -> None:
            if completed.done():
                return
            completed.set_result(confirm)
            dlg.open = False
            page.update()

        def _on_dismiss(_: ft.ControlEvent) -> None:
            _finish(False)

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("Reset Preferences", color=theme.text_primary),
            content=ft.Column(
                [
                    ft.Text(
                        "Reset all saved preferences and relaunch the app?",
                        color=theme.text_secondary,
                    ),
                    ft.Text(
                        "This clears settings and device preferences, then starts "
                        "first-time setup again.",
                        color=theme.text_muted,
                        size=12,
                    ),
                ],
                spacing=6,
                tight=True,
            ),
            actions=[
                ft.TextButton("Cancel", on_click=lambda _: _finish(False)),
                ft.FilledButton("Reset & relaunch", on_click=lambda _: _finish(True)),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
            on_dismiss=_on_dismiss,
        )

        page.show_dialog(dlg)
        page.update()
        if not await completed:
            return

        if _start_win_available:
            ok, err = set_run_at_logon(False)
            if not ok:
                show_error(err or "Could not reset Windows startup setting.")
                return
            _start_win_suppress[0] = True
            start_with_windows_sw.value = False
            _start_win_suppress[0] = False

        cfg.clear()
        cfg.update(default_config())
        save_config(cfg)

        try:
            cmd, cwd = _build_relaunch_command()
            release_singleton_mutex_for_handoff()
            subprocess.Popen(cmd, cwd=cwd)
        except OSError as ex:
            show_error(f"Could not relaunch app: {ex}")
            return

        await full_shutdown(yield_to_replacement=True)

    btn_install_usbipd = ft.OutlinedButton(
        content="Install usbipd-win / WSL packages",
        icon=ft.Icons.DOWNLOAD,
        tooltip="Install usbipd-win and/or WSL USB client packages (apt)",
        on_click=lambda _: page.run_task(open_usbip_setup_from_footer),
    )
    btn_check_updates_now = ft.OutlinedButton(
        content="Check now",
        icon=ft.Icons.SYSTEM_UPDATE_ALT,
        tooltip="Check GitHub Releases for updates now",
        on_click=lambda _: page.run_task(run_auto_update_check, True),
        disabled=_is_dev_or_dirty_build,
    )
    btn_reset_preferences = ft.OutlinedButton(
        content="Reset & relaunch",
        icon=ft.Icons.RESTART_ALT,
        tooltip="Clear all saved preferences and relaunch first-time setup",
        on_click=lambda _: page.run_task(reset_preferences_and_relaunch),
    )

    # Create theme dropdown for settings panel
    def _on_theme_change(theme_name: str) -> None:
        cfg["theme"] = theme_name
        persist_cfg()

    theme_dropdown = ThemeDropdownSelector(
        theme_manager=theme_manager,
        on_change_callback=_on_theme_change,
        initial_value=theme_manager.theme_name,
    )

    settings_panel = create_settings_panel(
        page,
        btn_install_usbipd=btn_install_usbipd,
        btn_check_updates_now=btn_check_updates_now,
        btn_reset_preferences=btn_reset_preferences,
        auto_update_sw=auto_update_sw,
        sort_dd=sort_dd,
        remember_startup=remember_startup,
        auto_refresh_sw=auto_refresh_sw,
        minimize_to_tray_sw=minimize_to_tray_sw,
        start_with_windows_sw=start_with_windows_sw,
        start_win_available=_start_win_available,
        settings_header_btn=settings_header_btn,
        settings_caption_style_closed=_settings_caption_style_closed,
        settings_caption_style_open=_settings_caption_style_open,
        theme_dropdown=theme_dropdown,
        theme=theme,
    )

    def _rebuild_settings_panel_for_theme(new_theme: AppTheme) -> None:
        nonlocal settings_panel
        settings_panel = create_settings_panel(
            page,
            btn_install_usbipd=btn_install_usbipd,
            btn_check_updates_now=btn_check_updates_now,
            btn_reset_preferences=btn_reset_preferences,
            auto_update_sw=auto_update_sw,
            sort_dd=sort_dd,
            remember_startup=remember_startup,
            auto_refresh_sw=auto_refresh_sw,
            minimize_to_tray_sw=minimize_to_tray_sw,
            start_with_windows_sw=start_with_windows_sw,
            start_win_available=_start_win_available,
            settings_header_btn=settings_header_btn,
            settings_caption_style_closed=_settings_caption_style_closed,
            settings_caption_style_open=_settings_caption_style_open,
            theme_dropdown=theme_dropdown,
            theme=new_theme,
        )

    def sync_header_action_buttons() -> None:
        refresh_header_btn.visible = not bool(auto_refresh_sw.value)

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

    content_host = ft.Stack(
        [
            list_padded,
            settings_panel.overlay,
        ],
        expand=True,
    )
    startup_flow_active = [True]

    def _apply_runtime_theme(new_theme: AppTheme) -> None:
        nonlocal theme, ACCENT
        theme = new_theme
        ACCENT = new_theme.accent
        settings_state = settings_panel.export_view_state()

        page.bgcolor = new_theme.page_bg
        status_text.color = new_theme.text_muted
        title_heading.color = new_theme.text_primary

        loading_overlay.bgcolor = new_theme.page_bg
        shutdown_overlay.bgcolor = new_theme.page_bg
        shutdown_status_text.color = new_theme.text_muted

        sort_dd.border_color = new_theme.input_border
        sort_dd.bgcolor = new_theme.input_bg
        sort_dd.label_style = ft.TextStyle(color=new_theme.input_label, size=12)
        sort_dd.text_style = ft.TextStyle(color=new_theme.text_primary, size=13)

        top_bar.bgcolor = new_theme.surface_bg
        top_bar.border = ft.border.only(bottom=ft.BorderSide(1, new_theme.border_subtle))

        remember_startup.active_color = ACCENT
        auto_refresh_sw.active_color = ACCENT
        auto_update_sw.active_color = ACCENT
        minimize_to_tray_sw.active_color = ACCENT
        start_with_windows_sw.active_color = ACCENT

        _rebuild_settings_panel_for_theme(new_theme)
        settings_panel.restore_view_state(settings_state)
        content_host.controls[1] = settings_panel.overlay
        if settings_state.get("is_open"):
            settings_panel.toggle()

        if not startup_flow_active[0]:
            page.run_task(rebuild_devices)
        page.update()

    theme_manager.subscribe(_apply_runtime_theme)

    body.content = ft.Column(
        [
            top_bar,
            content_host,
        ],
        expand=True,
        spacing=0,
    )

    page.add(body)

    page.window.prevent_close = True
    page.window.on_event = on_window_event
    sync_header_action_buttons()
    sync_caption_icons()

    await refresh_distros()
    usbip_ok = await asyncio.to_thread(usbipd_cli_works, usbipd_exe)
    _no_wsl = not wsl_distro_names
    btn_install_usbipd.disabled = _no_wsl and usbip_ok
    page.update()

    force_startup_setup = test_first_time_setup
    if force_startup_setup:
        cfg["startup_first_run_shown"] = False

    def _startup_should_run() -> bool:
        return force_startup_setup or not bool(cfg.get("startup_first_run_shown"))

    # Hide the loading overlay before startup panels open
    loading_overlay.visible = False
    page.update()

    def _update_usbipd_exe(exe: str) -> None:
        nonlocal usbipd_exe
        usbipd_exe = exe

    shell = SetupShell(page, theme, theme_manager, overlay_host=content_host)
    try:
        await shell.run(
            steps=[
                SetupStepRegistration(
                    key="theme",
                    should_show=_startup_should_run,
                    initial_completed=lambda: bool((cfg.get("theme") or "").strip()),
                    size_resolver=calculate_theme_step_preferred_size,
                    build_content=lambda ctx: build_theme_step_content(
                        ctx,
                        page=page,
                        theme_manager=theme_manager,
                        cfg=cfg,
                        save_config_fn=persist_cfg,
                    ),
                ),
                SetupStepRegistration(
                    key="usb",
                    should_show=lambda: _startup_should_run() and ((not usbip_ok) or test_first_time_setup),
                    initial_completed=lambda: False,
                    size_resolver=lambda page_obj, _content: calculate_usb_step_preferred_size(
                        page_obj,
                        test_first_time_setup=test_first_time_setup,
                        need_usbipd=not usbip_ok,
                        has_wsl_distros=bool(wsl_distro_names),
                    ),
                    build_content=lambda ctx: build_usb_step_content(
                        ctx,
                        page=page,
                        cfg=cfg,
                        wsl_distro_names=wsl_distro_names,
                        test_first_time_setup=test_first_time_setup,
                        install_cancel_holder=install_cancel_holder,
                        need_usbipd=not usbip_ok,
                        on_usbipd_updated=_update_usbipd_exe,
                    ),
                    on_leave=lambda direction: on_usb_step_leave(
                        direction, install_cancel_holder=install_cancel_holder
                    ),
                ),
                SetupStepRegistration(
                    key="preferences",
                    should_show=_startup_should_run,
                    initial_completed=lambda: True,
                    size_resolver=calculate_preferences_step_preferred_size,
                    build_content=lambda ctx: build_preferences_step_content(
                        ctx,
                        page=page,
                        cfg=cfg,
                        save_config_fn=persist_cfg,
                        auto_update_sw=auto_update_sw,
                        start_with_windows_sw=start_with_windows_sw,
                        start_win_available=_start_win_available,
                        on_start_with_windows_change=on_start_with_windows_change,
                    ),
                ),
            ],
        )
    finally:
        startup_flow_active[0] = False

    if _startup_should_run():
        cfg["startup_first_run_shown"] = True
        cfg["theme_first_run_shown"] = True
        save_config(cfg)

    loading_overlay.visible = True
    page.update()
    try:
        await rebuild_devices()
    finally:
        loading_overlay.visible = False
        page.update()

    if (
        cfg.get("apply_on_startup")
        and remembered_instance_ids(cfg)
        and wsl_distro_names
    ):
        await apply_remembered(quiet=True)

    page.run_task(poll_loop)
    if auto_update_sw.value:
        page.run_task(run_auto_update_check)

    async def on_disconnect(_: ft.ControlEvent) -> None:
        if _shutdown_started[0]:
            return
        await full_shutdown(yield_to_replacement=False)

    page.on_disconnect = on_disconnect
