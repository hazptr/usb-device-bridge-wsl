from __future__ import annotations

import logging
from typing import Any, Callable

import flet as ft

from usb_device_bridge.ui.startup.shell import SetupShellContext
from usb_device_bridge.ui.theme import AppTheme
from usb_device_bridge.windows.startup import (
    can_configure_run_at_logon,
    set_run_at_logon,
)

_log = logging.getLogger(__name__)

PREFERENCES_STEP_CONTENT_WIDTH = 460
PREFERENCES_STEP_CONTENT_HEIGHT = 340


def calculate_preferences_step_preferred_size(
    page: ft.Page,
    _: ft.Any = None,
) -> tuple[float | None, float | None]:
    """Standard step-size hook for SetupShell registrations."""
    page_width = float(page.width) if isinstance(page.width, (int, float)) else 0.0
    page_height = float(page.height) if isinstance(page.height, (int, float)) else 0.0

    width = float(PREFERENCES_STEP_CONTENT_WIDTH)
    height = float(PREFERENCES_STEP_CONTENT_HEIGHT)

    if page_width > 0:
        width = min(width, page_width - 128)
    if page_height > 0:
        height = min(height, page_height - 64)

    return max(320.0, width), max(240.0, height)


def build_preferences_step_content(
    ctx: SetupShellContext,
    *,
    page: ft.Page,
    cfg: dict[str, Any],
    save_config_fn: Callable[[], None],
    auto_update_sw: ft.Switch,
    start_with_windows_sw: ft.Switch,
    start_win_available: bool,
    on_start_with_windows_change: Callable[[ft.ControlEvent], None],
) -> ft.Control:
    """Build the preferences step content for the setup shell."""
    theme: AppTheme = ctx.theme

    # Preferences step is always immediately completable (it has no required action)
    ctx.mark_completed(True)

    def _row(
        icon: str,
        label: str,
        sublabel: str,
        switch: ft.Switch,
        disabled: bool = False,
    ) -> ft.Container:
        return ft.Container(
            content=ft.Row(
                [
                    ft.Container(
                        content=ft.Icon(icon, size=22, color=theme.text_secondary if not disabled else theme.text_muted),
                        width=36,
                        alignment=ft.Alignment.CENTER,
                    ),
                    ft.Column(
                        [
                            ft.Text(
                                label,
                                size=14,
                                weight=ft.FontWeight.W_500,
                                color=theme.text_primary if not disabled else theme.text_muted,
                            ),
                            ft.Text(
                                sublabel,
                                size=12,
                                color=theme.text_muted,
                            ),
                        ],
                        spacing=1,
                        tight=True,
                        expand=True,
                    ),
                    switch,
                ],
                spacing=12,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.symmetric(horizontal=16, vertical=10),
            border_radius=10,
            bgcolor=theme.card_bg,
            border=ft.border.all(1, theme.border_subtle),
        )

    auto_update_row = _row(
        ft.Icons.SYSTEM_UPDATE_ALT,
        "Automatic updates",
        "Check GitHub Releases for new versions on startup.",
        auto_update_sw,
        disabled=auto_update_sw.disabled,
    )

    start_on_login_row = _row(
        ft.Icons.LOGIN,
        "Start on login",
        "Launch automatically when you sign into Windows."
        if start_win_available
        else "Available in the installed Windows build only.",
        start_with_windows_sw,
        disabled=not start_win_available,
    )

    title_text = ft.Text(
        "Preferences",
        size=20,
        weight=ft.FontWeight.W_600,
        color=theme.text_primary,
        text_align=ft.TextAlign.CENTER,
    )
    body_text = ft.Text(
        "Quick settings to get you started.",
        size=13,
        color=theme.text_secondary,
        text_align=ft.TextAlign.CENTER,
    )
    footer_text = ft.Text(
        "All of these can be changed any time in Settings.",
        size=11,
        color=theme.text_muted,
        text_align=ft.TextAlign.CENTER,
        italic=True,
    )

    def _on_theme_changed(new_theme: AppTheme) -> None:
        ctx.theme = new_theme
        title_text.color = new_theme.text_primary
        body_text.color = new_theme.text_secondary
        footer_text.color = new_theme.text_muted
        page.update()

    ctx.register_theme_listener(_on_theme_changed)

    return ft.Container(
        width=PREFERENCES_STEP_CONTENT_WIDTH,
        content=ft.Column(
            [
                title_text,
                ft.Container(height=4),
                body_text,
                ft.Container(height=20),
                auto_update_row,
                ft.Container(height=8),
                start_on_login_row,
                ft.Container(height=16),
                footer_text,
            ],
            spacing=0,
            tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        padding=ft.padding.only(left=24, right=24, bottom=28),
    )


__all__ = [
    "build_preferences_step_content",
    "calculate_preferences_step_preferred_size",
]
