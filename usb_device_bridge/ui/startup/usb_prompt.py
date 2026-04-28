from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

import flet as ft

from usb_device_bridge.config import save_config
from usb_device_bridge.system_package_install import (
    find_winget,
    powershell_stream_setup_dialog_test,
    winget_install_usbipd,
    wsl_install_usbip_client_packages,
)
from usb_device_bridge.ui.startup.shell import SetupShellContext, StepNavDirection
from usb_device_bridge.ui.theme import AppTheme
from usb_device_bridge.usbipd import find_usbipd, usbipd_cli_works

_LOG_SETUP_INSTALL = logging.getLogger(__name__ + ".setup_install")

USB_STEP_CONTENT_WIDTH = 520
USB_LOG_HEIGHT = 104
USB_STEP_BASE_HEIGHT = 480
USB_STEP_TEST_MODE_EXTRA = 22
USB_STEP_NO_WSL_EXTRA = 20
SHELL_VERTICAL_MARGIN = 64


def calculate_usb_step_preferred_height(
    page: ft.Page,
    *,
    test_first_time_setup: bool,
    need_usbipd: bool,
    has_wsl_distros: bool,
) -> float:
    """Return a USB step height that avoids scrolling unless space is limited."""
    required = USB_STEP_BASE_HEIGHT
    if test_first_time_setup and not need_usbipd:
        required += USB_STEP_TEST_MODE_EXTRA
    if not has_wsl_distros:
        required += USB_STEP_NO_WSL_EXTRA

    page_height = float(page.height) if isinstance(page.height, (int, float)) else 0.0
    if page_height <= 0:
        return float(required)

    available = page_height - SHELL_VERTICAL_MARGIN
    return max(320.0, min(float(required), available))


def calculate_usb_step_preferred_size(
    page: ft.Page,
    *,
    test_first_time_setup: bool,
    need_usbipd: bool,
    has_wsl_distros: bool,
) -> tuple[float | None, float | None]:
    """Standard step-size hook for SetupShell registrations."""
    return (
        USB_STEP_CONTENT_WIDTH,
        calculate_usb_step_preferred_height(
            page,
            test_first_time_setup=test_first_time_setup,
            need_usbipd=need_usbipd,
            has_wsl_distros=has_wsl_distros,
        ),
    )


async def on_usb_step_leave(
    direction: StepNavDirection,
    *,
    install_cancel_holder: list[asyncio.Event | None],
) -> None:
    """Cancel any running install before the shell navigates away from the USB step."""
    ev = install_cancel_holder[0]
    if ev is not None:
        ev.set()
        await asyncio.sleep(0.05)


def build_usb_step_content(
    ctx: SetupShellContext,
    *,
    page: ft.Page,
    cfg: dict[str, Any],
    wsl_distro_names: list[str],
    test_first_time_setup: bool,
    install_cancel_holder: list[asyncio.Event | None],
    need_usbipd: bool,
    on_usbipd_updated: Callable[[str], None],
) -> ft.Control:
    """Build the USB setup content for the setup shell content slot."""
    theme: AppTheme = ctx.theme

    setup_success_pending = [False]

    # Intro text
    if need_usbipd:
        intro_parts: list[ft.Control] = [
            ft.Text(
                "Installs usbipd-win (WinGet) and WSL USB packages "
                "(usbutils, linux-tools-generic, hwdata).",
                color=theme.text_secondary,
                text_align=ft.TextAlign.CENTER,
            ),
        ]
    else:
        intro_parts = [
            ft.Text(
                "Installs WSL USB packages "
                "(usbutils, linux-tools-generic, hwdata). usbipd-win is already installed.",
                color=theme.text_secondary,
                text_align=ft.TextAlign.CENTER,
            ),
        ]
    if test_first_time_setup and not need_usbipd:
        intro_parts.insert(
            0,
            ft.Text(
                "Test mode: WinGet step skipped; running a short streamed PowerShell test.",
                color=theme.warning,
                text_align=ft.TextAlign.CENTER,
            ),
        )
    if not wsl_distro_names:
        intro_parts.append(
            ft.Text(
                "No WSL distros found. Run wsl --install and restart, or continue with usbipd-win only.",
                color=theme.warning,
                text_align=ft.TextAlign.CENTER,
            ),
        )
    intro_col = ft.Column(
        intro_parts,
        spacing=8,
        tight=True,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
    )

    status = ft.Text(
        "",
        color=theme.text_muted,
        selectable=True,
        text_align=ft.TextAlign.CENTER,
    )
    log_heading = ft.Text(
        "Install log",
        color=theme.text_muted,
        text_align=ft.TextAlign.CENTER,
    )
    settings_note = ft.Text(
        "All settings can be changed any time in Settings.",
        size=11,
        color=theme.text_muted,
        text_align=ft.TextAlign.CENTER,
        italic=True,
    )

    stick_install_log = [True]
    _log_append_depth = [0]

    install_log_text = ft.Text(
        "",
        selectable=True,
        font_family="monospace",
        color=theme.text_secondary,
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
        stick_install_log[0] = (m <= 1) or (p >= m - 36)

    log_scroll = ft.Column(
        [install_log_text],
        scroll=ft.ScrollMode.AUTO,
        height=USB_LOG_HEIGHT,
        tight=True,
        spacing=0,
        auto_scroll=False,
        on_scroll=on_install_log_scroll,
        horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
    )

    log_view = ft.Container(
        content=log_scroll,
        bgcolor=theme.surface_bg,
        border=ft.border.all(1, theme.input_border),
        border_radius=6,
        padding=ft.padding.all(10),
        expand=True,
    )

    async def _scroll_to_end() -> None:
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
        border_color=theme.input_border,
        filled=True,
        bgcolor=theme.input_bg,
        label_style=ft.TextStyle(color=theme.input_label),
        text_style=ft.TextStyle(color=theme.text_primary),
        options=[ft.dropdown.Option(n) for n in wsl_distro_names],
        value=default_dist,
        hint_text="No distributions" if not wsl_distro_names else None,
        disabled=not wsl_distro_names,
    )

    install_btn = ft.FilledButton(
        content="Install",
        icon=ft.Icons.DOWNLOAD,
        disabled=(not wsl_distro_names) and (not need_usbipd),
    )
    skip_btn = ft.TextButton("Skip")

    async def do_primary(_: ft.ControlEvent) -> None:
        if setup_success_pending[0]:
            # "Continue" pressed after successful install — navigate forward
            ctx.navigate("finish")
            return

        usbipd_exe_local = find_usbipd()
        cancel_ev = asyncio.Event()
        install_cancel_holder[0] = cancel_ev
        try:
            setup_success_pending[0] = False
            install_btn.disabled = True
            status.value = ""
            status.color = theme.text_muted
            stick_install_log[0] = True
            install_log_text.value = ""
            page.update()

            install_log: list[str] = [""]
            last_log_ui = [0.0]

            def _flush(*, force: bool = False) -> None:
                now = time.monotonic()
                if not force and now - last_log_ui[0] < 0.12:
                    return
                last_log_ui[0] = now
                page.update()
                if stick_install_log[0]:
                    page.run_task(_scroll_to_end)

            def _append(delta: str) -> None:
                _log_append_depth[0] += 1
                try:
                    if delta:
                        _LOG_SETUP_INSTALL.info("%s", delta)
                    install_log[0] += delta
                    if len(install_log[0]) > 120_000:
                        install_log[0] = install_log[0][-100_000:]
                    install_log_text.value = install_log[0]
                    _flush()
                finally:
                    _log_append_depth[0] -= 1

            install_log_text.value = ""
            install_log[0] = ""
            _LOG_SETUP_INSTALL.info("--- setup install output (stream follows) ---")

            if need_usbipd:
                if not find_winget():
                    status.value = (
                        "WinGet not found. Install App Installer from Microsoft Store, then retry."
                    )
                    install_btn.disabled = False
                    skip_btn.disabled = False
                    page.update()
                    return
                status.value = "Installing usbipd-win via WinGet (see log below)…"
                _append("— WinGet (usbipd-win) —\n")
                page.update()
                ok, msg = await winget_install_usbipd(
                    on_output_text=_append,
                    cancel_event=cancel_ev,
                )
                _flush(force=True)
                if not ok:
                    status.value = "Installation cancelled." if "[Cancelled.]" in msg else msg
                    install_btn.disabled = False
                    skip_btn.disabled = False
                    page.update()
                    return
                usbipd_exe_local = find_usbipd()
                if not await asyncio.to_thread(usbipd_cli_works, usbipd_exe_local):
                    status.value = (
                        "usbipd is still not responding. Restart the app and try again."
                    )
                    install_btn.disabled = False
                    skip_btn.disabled = False
                    page.update()
                    return
                on_usbipd_updated(usbipd_exe_local)

            if test_first_time_setup and not need_usbipd:
                status.value = (
                    "Test mode: running streamed PowerShell test…"
                )
                _append("\n— Test: PowerShell stream (WinGet-style code path) —\n")
                page.update()
                ok_ps, msg_ps = await powershell_stream_setup_dialog_test(
                    on_output_text=_append,
                    cancel_event=cancel_ev,
                )
                _flush(force=True)
                if not ok_ps:
                    status.value = (
                        "Installation cancelled." if "[Cancelled.]" in msg_ps else msg_ps
                    )
                    install_btn.disabled = False
                    skip_btn.disabled = False
                    page.update()
                    return

            d = (distro_dd.value or "").strip()
            if not d:
                status.value = "Select a WSL distro first."
                install_btn.disabled = False
                skip_btn.disabled = False
                page.update()
                return

            cfg["wsl_distro"] = d
            save_config(cfg)

            status.value = f"Installing packages in \u201c{d}\u201d…"
            _append("\n— WSL (apt) —\n")
            page.update()

            ok2, msg2 = await wsl_install_usbip_client_packages(
                d,
                on_output_text=_append,
                cancel_event=cancel_ev,
            )
            _flush(force=True)
            if not ok2:
                status.value = (
                    "Installation cancelled."
                    if "[Cancelled.]" in msg2
                    else (
                        "Install failed. See log. "
                        f"{msg2[:400]}{'…' if len(msg2) > 400 else ''}"
                    )
                )
                install_btn.disabled = False
                skip_btn.disabled = False
                page.update()
                return

            # Success
            setup_success_pending[0] = True
            status.value = "Setup complete. Review the log, then click Continue."
            status.color = theme.success
            install_btn.content = "Continue"
            install_btn.icon = ft.Icons.CHECK_CIRCLE
            install_btn.disabled = False
            install_btn.style = ft.ButtonStyle(
                bgcolor={
                    ft.ControlState.DEFAULT: theme.success,
                    ft.ControlState.HOVERED: theme.success,
                },
                color=theme.text_on_accent,
                icon_color=theme.text_on_accent,
                overlay_color=theme.success_bg,
                animation_duration=700,
            )
            ctx.mark_completed(True)
            page.update()

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
        ctx.mark_completed(True)
        status.value = "Skipped. Use the right arrow to finish."
        status.color = theme.warning
        page.update()

    install_btn.on_click = lambda e: page.run_task(do_primary, e)
    skip_btn.on_click = lambda e: page.run_task(do_skip, e)

    return ft.Container(
        width=USB_STEP_CONTENT_WIDTH,
        padding=ft.padding.only(left=24, right=24, bottom=24),
        content=ft.Column(
            [
                ft.Column(
                    [
                        ft.Text(
                            "Set up USB Device Bridge for WSL",
                            size=20,
                            weight=ft.FontWeight.W_600,
                            color=theme.text_primary,
                            text_align=ft.TextAlign.CENTER,
                        ),
                        ft.Container(height=6),
                        ft.Text(
                            "Install the prerequisites for USB passthrough.",
                            size=13,
                            color=theme.text_secondary,
                            text_align=ft.TextAlign.CENTER,
                        ),
                    ],
                    spacing=0,
                    tight=True,
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                ft.Container(height=6),
                intro_col,
                distro_dd,
                status,
                log_heading,
                log_view,
                ft.Container(height=2),
                ft.Row(
                    [skip_btn, install_btn],
                    spacing=8,
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
                ft.Container(height=8),
                settings_note,
            ],
            spacing=6,
            tight=True,
            scroll=ft.ScrollMode.HIDDEN,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
    )


__all__ = [
    "build_usb_step_content",
    "calculate_usb_step_preferred_height",
    "calculate_usb_step_preferred_size",
    "on_usb_step_leave",
]
