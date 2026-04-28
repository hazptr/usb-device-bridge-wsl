from __future__ import annotations

import json
from typing import Callable

import flet as ft

from usb_device_bridge.ui.theme import (
    AppTheme,
    ThemeManager,
    list_available_themes,
)


class ThemeDropdownSelector(ft.Dropdown):
    """A dropdown selector for choosing themes in settings.

    Updates the theme in real-time when changed.
    """

    def __init__(
        self,
        theme_manager: ThemeManager,
        on_change_callback: Callable[[str], None] | None = None,
        initial_value: str = "dark",
    ) -> None:
        self._theme_manager = theme_manager
        self._on_change_callback = on_change_callback
        self._display_to_name: dict[str, str] = {
            display_name.lower(): name
            for name, display_name in list_available_themes()
        }
        self._name_set = {name for name, _display in list_available_themes()}

        theme_options = [
            ft.DropdownOption(key=name, text=display_name)
            for name, display_name in list_available_themes()
        ]

        normalized_initial = self._normalize_theme_value(initial_value)

        super().__init__(
            label="Theme",
            value=normalized_initial,
            options=theme_options,
            width=200,
            dense=True,
        )

        self.on_change = self._handle_change
        if hasattr(self, "on_select"):
            setattr(self, "on_select", self._handle_change)

        self._theme_manager = theme_manager
        theme_manager.subscribe(self._apply_theme_styling)

    def _apply_theme_styling(self, theme: AppTheme) -> None:
        self.value = theme.name
        self.border_color = theme.input_border
        self.filled = True
        self.bgcolor = theme.input_bg
        self.label_style = ft.TextStyle(color=theme.input_label, size=12)
        self.text_style = ft.TextStyle(color=theme.text_primary, size=13)
        if self.page is not None:
            self.update()

    def _handle_change(self, e: ft.ControlEvent) -> None:
        raw_value: str | None = getattr(e.control, "value", None)
        data = getattr(e, "data", None)
        if not raw_value and isinstance(data, str):
            raw_value = data
            if raw_value.strip().startswith("{"):
                try:
                    parsed = json.loads(raw_value)
                    if isinstance(parsed, dict):
                        v = parsed.get("value")
                        if isinstance(v, str):
                            raw_value = v
                except json.JSONDecodeError:
                    pass
        selected = self._normalize_theme_value(raw_value)
        if selected and selected != self._theme_manager.theme_name:
            self._theme_manager.set_theme(selected)
            if self._on_change_callback:
                self._on_change_callback(selected)

    def _normalize_theme_value(self, value: str | None) -> str:
        raw = (value or "").strip()
        if not raw:
            return self._theme_manager.theme_name
        low = raw.lower()
        if low in self._name_set:
            return low
        return self._display_to_name.get(low, self._theme_manager.theme_name)


__all__ = ["ThemeDropdownSelector"]
