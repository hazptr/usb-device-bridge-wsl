from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import flet as ft


@dataclass(frozen=True)
class AppTheme:
    """Complete theme definition for the app.

    All color values are hex strings (e.g., "#0b1220").
    """

    name: str
    display_name: str

    # Background colors
    page_bg: str
    surface_bg: str
    card_bg: str
    elevated_surface_bg: str

    # Border colors
    border_subtle: str
    border_default: str

    # Text colors
    text_primary: str
    text_secondary: str
    text_muted: str
    text_on_accent: str

    # Status/Accent colors
    accent: str
    accent_hover: str
    success: str
    warning: str
    error: str
    info: str

    # Semantic backgrounds for status
    success_bg: str
    warning_bg: str
    error_bg: str
    info_bg: str

    # Control colors
    input_bg: str
    input_border: str
    input_label: str
    button_bg: str
    button_hover: str
    overlay_bg: str

    # Tab/sidebar colors
    tab_inactive_bg: str
    tab_active_bg: str
    tab_inactive_text: str
    tab_active_text: str

    # Device status pill colors
    pill_attached_bg: str
    pill_attached_text: str
    pill_shared_bg: str
    pill_shared_text: str
    pill_available_bg: str
    pill_available_text: str
    pill_offline_bg: str
    pill_offline_text: str


# =============================================================================
# Predefined Themes
# =============================================================================

DARK_THEME = AppTheme(
    name="dark",
    display_name="Dark",
    page_bg="#0b1220",
    surface_bg="#0f172a",
    card_bg="#111827",
    elevated_surface_bg="#111b2e",
    border_subtle="#1e293b",
    border_default="#23324b",
    text_primary="#f8fafc",
    text_secondary="#e2e8f0",
    text_muted="#94a3b8",
    text_on_accent="#0f172a",
    accent="#2dd4bf",
    accent_hover="#14b8a6",
    success="#34d399",
    warning="#fbbf24",
    error="#fca5a5",
    info="#60a5fa",
    success_bg="#14532d",
    warning_bg="#713f12",
    error_bg="#7f1d1d",
    info_bg="#1e3a8a",
    input_bg="#1e293b",
    input_border="#334155",
    input_label="#94a3b8",
    button_bg="#334155",
    button_hover="#475569",
    overlay_bg="#0b1220",
    tab_inactive_bg="#0b1220",
    tab_active_bg="#115e59",
    tab_inactive_text="#94a3b8",
    tab_active_text="#ecfeff",
    pill_attached_bg="#14532d",
    pill_attached_text="#34d399",
    pill_shared_bg="#713f12",
    pill_shared_text="#fbbf24",
    pill_available_bg="#0f172a",
    pill_available_text="#94a3b8",
    pill_offline_bg="#312e81",
    pill_offline_text="#a78bfa",
)

LIGHT_THEME = AppTheme(
    name="light",
    display_name="Light",
    page_bg="#ffffff",
    surface_bg="#f8fafc",
    card_bg="#f1f5f9",
    elevated_surface_bg="#e2e8f0",
    border_subtle="#e2e8f0",
    border_default="#cbd5e1",
    text_primary="#0f172a",
    text_secondary="#334155",
    text_muted="#64748b",
    text_on_accent="#ffffff",
    accent="#0d9488",
    accent_hover="#0f766e",
    success="#16a34a",
    warning="#d97706",
    error="#dc2626",
    info="#2563eb",
    success_bg="#dcfce7",
    warning_bg="#fef3c7",
    error_bg="#fee2e2",
    info_bg="#dbeafe",
    input_bg="#ffffff",
    input_border="#cbd5e1",
    input_label="#64748b",
    button_bg="#e2e8f0",
    button_hover="#cbd5e1",
    overlay_bg="#f8fafc",
    tab_inactive_bg="#f8fafc",
    tab_active_bg="#ccfbf1",
    tab_inactive_text="#64748b",
    tab_active_text="#115e59",
    pill_attached_bg="#dcfce7",
    pill_attached_text="#16a34a",
    pill_shared_bg="#fef3c7",
    pill_shared_text="#d97706",
    pill_available_bg="#f1f5f9",
    pill_available_text="#64748b",
    pill_offline_bg="#ede9fe",
    pill_offline_text="#7c3aed",
)

# Registry of available themes
THEME_REGISTRY: dict[str, AppTheme] = {
    "dark": DARK_THEME,
    "light": LIGHT_THEME,
}


def get_theme(name: str) -> AppTheme:
    """Get a theme by name, defaulting to dark if not found."""
    return THEME_REGISTRY.get(name, DARK_THEME)


def get_default_theme() -> AppTheme:
    """Get the default theme (dark)."""
    return DARK_THEME


def list_available_themes() -> list[tuple[str, str]]:
    """Return list of (theme_name, display_name) tuples."""
    return [(t.name, t.display_name) for t in THEME_REGISTRY.values()]


# =============================================================================
# Theme Manager
# =============================================================================

ThemeChangeCallback = Callable[[AppTheme], None]


class ThemeManager:
    """Manages the active theme and notifies subscribers of changes.

    This is designed to be a singleton instance that coordinates
theme changes across the entire app.
    """

    def __init__(self, page: ft.Page, initial_theme: str = "dark") -> None:
        self._page = page
        self._current_theme = get_theme(initial_theme)
        self._subscribers: list[ThemeChangeCallback] = []

    def _theme_mode_for(self, theme: AppTheme) -> ft.ThemeMode:
        return ft.ThemeMode.LIGHT if theme.name == "light" else ft.ThemeMode.DARK

    @property
    def current_theme(self) -> AppTheme:
        return self._current_theme

    @property
    def theme_name(self) -> str:
        return self._current_theme.name

    def subscribe(self, callback: ThemeChangeCallback) -> None:
        """Register a callback to be called when theme changes."""
        self._subscribers.append(callback)

    def unsubscribe(self, callback: ThemeChangeCallback) -> None:
        """Unregister a previously registered callback."""
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def set_theme(self, theme_name: str) -> None:
        """Change the active theme and notify all subscribers."""
        new_theme = get_theme(theme_name)
        if new_theme.name == self._current_theme.name:
            return

        self._current_theme = new_theme

        # Apply to page
        self._apply_to_page()

        # Notify subscribers
        for callback in self._subscribers:
            try:
                callback(new_theme)
            except Exception:
                pass  # Ignore subscriber errors

    def preview_theme(self, theme_name: str) -> None:
        """Apply theme temporarily without notifying subscribers (for preview)."""
        preview = get_theme(theme_name)
        self._apply_theme_to_page(preview)

    def restore_current(self) -> None:
        """Restore the current committed theme."""
        self._apply_to_page()

    def _apply_to_page(self) -> None:
        """Apply the current theme to the page."""
        self._apply_theme_to_page(self._current_theme)

    def _apply_theme_to_page(self, theme: AppTheme) -> None:
        """Apply a specific theme to the page."""
        self._page.bgcolor = theme.page_bg
        self._page.theme_mode = self._theme_mode_for(theme)
        self._page.update()


def create_flet_theme_from_app_theme(theme: AppTheme) -> ft.Theme:
    """Create a Flet Theme object from our AppTheme.

    This provides deeper Flet integration when needed.
    """
    return ft.Theme(
        color_scheme=ft.ColorScheme(
            primary=theme.accent,
            on_primary=theme.text_on_accent,
            surface=theme.surface_bg,
            on_surface=theme.text_primary,
            background=theme.page_bg,
            on_background=theme.text_primary,
        )
    )
