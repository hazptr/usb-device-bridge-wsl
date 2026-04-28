from __future__ import annotations

import flet as ft
from typing import Any

from usb_device_bridge.ui.theme import AppTheme


class SettingsPanelController:
    def __init__(
        self,
        page: ft.Page,
        *,
        btn_install_usbipd: ft.OutlinedButton,
        btn_check_updates_now: ft.OutlinedButton,
        btn_reset_preferences: ft.OutlinedButton,
        auto_update_sw: ft.Switch,
        sort_dd: ft.Dropdown,
        remember_startup: ft.Switch,
        auto_refresh_sw: ft.Switch,
        minimize_to_tray_sw: ft.Switch,
        start_with_windows_sw: ft.Switch,
        start_win_available: bool,
        settings_header_btn: ft.IconButton,
        settings_caption_style_closed: ft.ButtonStyle,
        settings_caption_style_open: ft.ButtonStyle,
        theme_dropdown: ft.Dropdown | None = None,
        theme: AppTheme,
    ) -> None:
        self._page = page
        self._settings_header_btn = settings_header_btn
        self._style_closed = settings_caption_style_closed
        self._style_open = settings_caption_style_open
        self._settings_open = False
        self._theme_dropdown = theme_dropdown
        # Theme is always valid - defaults handled at entry point (create_settings_panel)
        self._theme = theme

        # Use theme colors directly
        t = theme
        tab_inactive_text = t.tab_inactive_text
        tab_inactive_bg = t.tab_inactive_bg
        tab_overlay = t.button_bg
        tab_active_text = t.tab_active_text
        tab_active_bg = t.tab_active_bg
        tab_active_overlay = t.accent_hover

        self._settings_rows: dict[int, list[tuple[ft.Container, str, bool]]] = {
            0: [],
            1: [],
            2: [],
            3: [],
        }

        self._tab_style_idle = ft.ButtonStyle(
            color=tab_inactive_text,
            bgcolor=tab_inactive_bg,
            overlay_color=tab_overlay,
            padding=ft.padding.symmetric(horizontal=10, vertical=6),
            alignment=ft.alignment.Alignment(-1, 0),
            shape=ft.RoundedRectangleBorder(radius=4),
        )
        self._tab_style_active = ft.ButtonStyle(
            color=tab_active_text,
            bgcolor=tab_active_bg,
            overlay_color=tab_active_overlay,
            padding=ft.padding.symmetric(horizontal=10, vertical=6),
            alignment=ft.alignment.Alignment(-1, 0),
            shape=ft.RoundedRectangleBorder(radius=4),
        )
        self._settings_tab_index = 0
        self._settings_tab_buttons: list[ft.TextButton] = []

        self._tab_titles = {
            0: "Setup & updates",
            1: "Device list",
            2: "Window",
            3: "Appearance",
        }

        # Theme colors for search and tabs
        search_border = t.input_border
        search_bg = t.input_bg
        search_text = t.text_primary
        elevated_bg = t.elevated_surface_bg
        border_color = t.border_default
        divider_color = t.border_subtle
        sidebar_bg = t.surface_bg

        self.settings_search = ft.TextField(
            hint_text="Search settings",
            prefix_icon=ft.Icons.SEARCH,
            dense=True,
            border_color=search_border,
            filled=True,
            bgcolor=search_bg,
            text_style=ft.TextStyle(color=search_text, size=12),
        )

        self.setup_tab_content = ft.Container(
            bgcolor=elevated_bg,
            border=ft.border.all(1, border_color),
            border_radius=10,
            padding=ft.padding.symmetric(horizontal=12, vertical=10),
            content=ft.Column(
                [
                    self._settings_item(
                        0,
                        "Install usbipd-win / WSL packages",
                        btn_install_usbipd,
                    ),
                    ft.Divider(color=divider_color, height=10),
                    self._settings_item(
                        0,
                        "Auto-update",
                        ft.Row(
                            [btn_check_updates_now, auto_update_sw],
                            spacing=8,
                            tight=True,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                        hint=(
                            "Checks GitHub Releases for a newer installer, "
                            "downloads it in the background, and prompts when "
                            "the update is ready to install."
                        ),
                    ),
                    ft.Divider(color=divider_color, height=10),
                    self._settings_item(
                        0,
                        "Reset saved preferences",
                        btn_reset_preferences,
                        hint=(
                            "Clears saved app preferences and restarts the app, "
                            "including first-time setup prompts."
                        ),
                    ),
                ],
                spacing=8,
                tight=True,
            ),
        )

        self.behavior_tab_content = ft.Container(
            bgcolor=elevated_bg,
            border=ft.border.all(1, border_color),
            border_radius=10,
            padding=ft.padding.symmetric(horizontal=12, vertical=10),
            content=ft.Column(
                [
                    self._settings_item(1, "Sort order", sort_dd),
                    self._settings_item(
                        1,
                        "Apply remembered on startup",
                        remember_startup,
                    ),
                    self._settings_item(
                        1,
                        "Auto-refresh (3s)",
                        auto_refresh_sw,
                        hint=(
                            "Refreshes the device list on a timer. If you have "
                            "remembered devices, the list still updates periodically "
                            "so plug-ins are noticed; remembered attachment runs in "
                            "the background either way."
                        ),
                    ),
                ],
                spacing=8,
                tight=True,
            ),
        )

        self.window_tab_content = ft.Container(
            bgcolor=elevated_bg,
            border=ft.border.all(1, border_color),
            border_radius=10,
            padding=ft.padding.symmetric(horizontal=12, vertical=10),
            content=ft.Column(
                [
                    self._settings_item(2, "Minimize to tray", minimize_to_tray_sw),
                    self._settings_item(
                        2,
                        "Start with Windows",
                        start_with_windows_sw,
                        visible=start_win_available,
                    ),
                ],
                spacing=8,
                tight=True,
            ),
        )

        # Build appearance tab content
        appearance_content: list[ft.Control] = []
        if self._theme_dropdown:
            appearance_content.append(
                self._settings_item(3, "Theme", self._theme_dropdown)
            )
        else:
            appearance_content.append(
                ft.Text(
                    "Theme settings not available.",
                    color=t.text_muted,
                    size=12,
                )
            )

        self.appearance_tab_content = ft.Container(
            bgcolor=elevated_bg,
            border=ft.border.all(1, border_color),
            border_radius=10,
            padding=ft.padding.symmetric(horizontal=12, vertical=10),
            content=ft.Column(
                appearance_content,
                spacing=8,
                tight=True,
            ),
        )
        self.settings_tab_body = ft.Container(content=self.setup_tab_content)

        self._settings_tab_buttons = [
            ft.TextButton(
                "Setup & updates",
                style=self._tab_style_active,
                on_click=lambda _, i=0: self.set_settings_tab(i),
            ),
            ft.TextButton(
                "Device list",
                style=self._tab_style_idle,
                on_click=lambda _, i=1: self.set_settings_tab(i),
            ),
            ft.TextButton(
                "Window",
                style=self._tab_style_idle,
                on_click=lambda _, i=2: self.set_settings_tab(i),
            ),
            ft.TextButton(
                "Appearance",
                style=self._tab_style_idle,
                on_click=lambda _, i=3: self.set_settings_tab(i),
            ),
        ]

        settings_panel_content = ft.Column(
            [
                ft.Row(
                    [
                        ft.Container(
                            width=220,
                            bgcolor=sidebar_bg,
                            border=ft.border.all(1, border_color),
                            border_radius=10,
                            padding=ft.padding.symmetric(horizontal=8, vertical=8),
                            content=ft.Column(
                                [
                                    self.settings_search,
                                    ft.Divider(color=divider_color, height=8),
                                    *self._settings_tab_buttons,
                                ],
                                spacing=6,
                                tight=True,
                                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                            ),
                        ),
                        ft.Container(self.settings_tab_body, expand=True),
                    ],
                    expand=True,
                    spacing=10,
                    vertical_alignment=ft.CrossAxisAlignment.START,
                ),
            ],
            spacing=8,
            tight=False,
            expand=True,
        )

        self.overlay = ft.Container(
            visible=False,
            expand=True,
            bgcolor=self._theme.overlay_bg,
            content=ft.Container(
                content=settings_panel_content,
                padding=ft.padding.symmetric(horizontal=16, vertical=12),
                expand=True,
            ),
        )

        self.settings_search.on_change = lambda _: (
            self.apply_settings_search(),
            self._page.update(),
        )
        self._settings_header_btn.on_click = lambda _: self.toggle()

    def _settings_item(
        self,
        tab_index: int,
        label: str,
        control: ft.Control,
        *,
        hint: str | None = None,
        visible: bool = True,
    ) -> ft.Container:
        row = ft.Container(
            visible=visible,
            content=ft.Row(
                [
                    ft.Text(
                        label,
                        color=self._theme.text_secondary,
                        size=12,
                        expand=True,
                        tooltip=hint,
                    ),
                    control,
                ],
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        self._settings_rows[tab_index].append(
            (row, f"{label} {hint or ''}".strip().lower(), bool(visible))
        )
        return row

    def set_settings_tab(self, index: int) -> None:
        self._settings_tab_index = index
        for i, button in enumerate(self._settings_tab_buttons):
            button.style = (
                self._tab_style_active
                if i == self._settings_tab_index
                else self._tab_style_idle
            )
        self.apply_settings_search()
        self._page.update()

    def apply_settings_search(self) -> None:
        query = (self.settings_search.value or "").strip().lower()
        if query:
            sections: list[ft.Control] = []
            for tab, rows in self._settings_rows.items():
                any_visible = False
                for row, hay, base_visible in rows:
                    row.visible = base_visible and (query in hay)
                    any_visible = any_visible or row.visible
                if any_visible:
                    content = (
                        self.setup_tab_content
                        if tab == 0
                        else self.behavior_tab_content
                        if tab == 1
                        else self.window_tab_content
                        if tab == 2
                        else self.appearance_tab_content
                    )
                    sections.append(
                        ft.Text(
                            self._tab_titles[tab],
                            size=11,
                            color=self._theme.text_muted,
                            weight=ft.FontWeight.W_600,
                        )
                    )
                    sections.append(content)
            self.settings_tab_body.content = (
                ft.Column(sections, spacing=8, tight=True)
                if sections
                else ft.Container(
                    bgcolor=self._theme.elevated_surface_bg,
                    border=ft.border.all(1, self._theme.border_default),
                    border_radius=10,
                    padding=ft.padding.symmetric(horizontal=12, vertical=12),
                    content=ft.Text(
                        "No settings matched your search.",
                        color=self._theme.text_muted,
                        size=12,
                    ),
                )
            )
            return

        for tab, rows in self._settings_rows.items():
            selected = tab == self._settings_tab_index
            for row, _, base_visible in rows:
                row.visible = base_visible and selected

        if self._settings_tab_index == 0:
            self.settings_tab_body.content = self.setup_tab_content
        elif self._settings_tab_index == 1:
            self.settings_tab_body.content = self.behavior_tab_content
        elif self._settings_tab_index == 2:
            self.settings_tab_body.content = self.window_tab_content
        else:
            self.settings_tab_body.content = self.appearance_tab_content

    def toggle(self) -> None:
        self._settings_open = not self._settings_open
        self.overlay.visible = self._settings_open
        self._settings_header_btn.icon = (
            ft.Icons.CLOSE if self._settings_open else ft.Icons.SETTINGS
        )
        self._settings_header_btn.tooltip = (
            "Hide settings" if self._settings_open else "Show settings"
        )
        self._settings_header_btn.style = (
            self._style_open if self._settings_open else self._style_closed
        )
        if self._settings_open:
            self.apply_settings_search()
        self._page.update()

    def export_view_state(self) -> dict[str, Any]:
        return {
            "tab_index": self._settings_tab_index,
            "search_query": self.settings_search.value or "",
            "is_open": self._settings_open,
        }

    def restore_view_state(self, state: dict[str, Any]) -> None:
        tab_index = state.get("tab_index", 0)
        if not isinstance(tab_index, int):
            tab_index = 0
        max_tab = max(0, len(self._settings_tab_buttons) - 1)
        self._settings_tab_index = max(0, min(tab_index, max_tab))

        for i, button in enumerate(self._settings_tab_buttons):
            button.style = (
                self._tab_style_active
                if i == self._settings_tab_index
                else self._tab_style_idle
            )

        search_query = state.get("search_query", "")
        self.settings_search.value = search_query if isinstance(search_query, str) else ""
        self.apply_settings_search()


def create_settings_panel(
    page: ft.Page,
    *,
    btn_install_usbipd: ft.OutlinedButton,
    btn_check_updates_now: ft.OutlinedButton,
    btn_reset_preferences: ft.OutlinedButton,
    auto_update_sw: ft.Switch,
    sort_dd: ft.Dropdown,
    remember_startup: ft.Switch,
    auto_refresh_sw: ft.Switch,
    minimize_to_tray_sw: ft.Switch,
    start_with_windows_sw: ft.Switch,
    start_win_available: bool,
    settings_header_btn: ft.IconButton,
    settings_caption_style_closed: ft.ButtonStyle,
    settings_caption_style_open: ft.ButtonStyle,
    theme_dropdown: ft.Dropdown | None = None,
    theme: AppTheme,
) -> SettingsPanelController:
    return SettingsPanelController(
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
        start_win_available=start_win_available,
        settings_header_btn=settings_header_btn,
        settings_caption_style_closed=settings_caption_style_closed,
        settings_caption_style_open=settings_caption_style_open,
        theme_dropdown=theme_dropdown,
        theme=theme,
    )
