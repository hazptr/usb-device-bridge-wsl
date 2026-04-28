from __future__ import annotations

import logging
from typing import Callable

import flet as ft

from usb_device_bridge.ui.startup.shell import SetupShellContext
from usb_device_bridge.ui.theme import (
    DARK_THEME,
    LIGHT_THEME,
    AppTheme,
    ThemeManager,
)

_log = logging.getLogger(__name__)

THEME_PREVIEW_INNER_WIDTH = 220
THEME_PREVIEW_CARD_OUTER_PADDING = 24  # 12 left + 12 right
THEME_PREVIEW_GAP = 16
THEME_CONTENT_HORIZONTAL_PADDING = 48  # 24 left + 24 right
SHELL_SIDE_NAV_GUTTER = 112  # 56 left + 56 right
SHELL_EXTRA_HORIZONTAL_MARGIN = 16


def calculate_theme_step_preferred_width(page: ft.Page) -> float:
    """Compute a safe theme-step width from actual content geometry.

    Width is derived from card dimensions + spacing + content padding, then clamped
    to the currently available page width so high DPI / scaling / smaller windows
    don't clip the right edge.
    """
    card_outer_width = THEME_PREVIEW_INNER_WIDTH + THEME_PREVIEW_CARD_OUTER_PADDING
    content_required_width = (
        (card_outer_width * 2)
        + THEME_PREVIEW_GAP
        + THEME_CONTENT_HORIZONTAL_PADDING
    )

    page_width = float(page.width) if isinstance(page.width, (int, float)) else 0.0
    if page_width <= 0:
        return content_required_width

    available_width = (
        page_width
        - SHELL_SIDE_NAV_GUTTER
        - SHELL_EXTRA_HORIZONTAL_MARGIN
    )
    return max(320.0, min(content_required_width, available_width))


def calculate_theme_step_preferred_size(
    page: ft.Page,
    _: ft.Control | None = None,
) -> tuple[float | None, float | None]:
    """Standard step-size hook for SetupShell registrations."""
    return calculate_theme_step_preferred_width(page), None


class ThemePreviewCard(ft.Container):
    """A stylized theme preview card for the theme picker."""

    def __init__(
        self,
        theme: AppTheme,
        *,
        label_color: str,
        on_hover: Callable[[AppTheme], None] | None = None,
        on_click: Callable[[AppTheme], None] | None = None,
    ) -> None:
        self._app_theme = theme
        self._label_color = label_color
        self._on_hover = on_hover
        self._on_click = on_click
        self._selected = False
        self._label_text = ft.Text(
            theme.display_name,
            size=14,
            weight=ft.FontWeight.W_500,
            color=self._label_color,
            text_align=ft.TextAlign.CENTER,
        )

        super().__init__(
            content=ft.Column(
                [
                    ft.Container(
                        content=self._build_preview_content(theme),
                        width=220,
                        height=140,
                        border_radius=8,
                        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                        border=ft.border.all(1, theme.border_default),
                    ),
                    self._label_text,
                ],
                spacing=12,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            padding=ft.padding.all(12),
            border_radius=12,
            border=ft.border.all(2, "transparent"),
            bgcolor="transparent",
            animate=ft.Animation(200, ft.AnimationCurve.EASE_OUT),
            on_hover=self._handle_hover,
            on_click=self._handle_click,
        )

    def _build_preview_content(self, theme: AppTheme) -> ft.Control:
        mini_top_bar = ft.Container(
            content=ft.Row(
                [
                    ft.Container(width=60, height=8, bgcolor=theme.text_muted, border_radius=4),
                    ft.Container(expand=True),
                    ft.Container(width=8, height=8, bgcolor=theme.text_muted, border_radius=4),
                    ft.Container(width=4),
                    ft.Container(width=8, height=8, bgcolor=theme.text_muted, border_radius=4),
                    ft.Container(width=4),
                    ft.Container(width=8, height=8, bgcolor=theme.error, border_radius=4),
                ],
                spacing=4,
            ),
            padding=ft.padding.symmetric(horizontal=8, vertical=6),
            bgcolor=theme.surface_bg,
            border=ft.border.only(bottom=ft.BorderSide(1, theme.border_subtle)),
        )

        def mini_card(status_text: str, status_bg: str, status_color: str) -> ft.Container:
            return ft.Container(
                content=ft.Row(
                    [
                        ft.Container(
                            width=40, height=14, bgcolor=status_bg, border_radius=4,
                            content=ft.Text(
                                status_text, size=5, color=status_color,
                                weight=ft.FontWeight.W_600,
                                text_align=ft.TextAlign.CENTER,
                            ),
                            alignment=ft.Alignment.CENTER,
                        ),
                        ft.Column(
                            [
                                ft.Container(width=70, height=6, bgcolor=theme.text_primary, border_radius=3),
                                ft.Container(width=50, height=4, bgcolor=theme.text_muted, border_radius=2),
                            ],
                            spacing=2,
                            tight=True,
                        ),
                        ft.Container(expand=True),
                        ft.Container(width=12, height=12, bgcolor=theme.accent, border_radius=6),
                    ],
                    spacing=6,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                padding=ft.padding.symmetric(horizontal=8, vertical=5),
                bgcolor=theme.card_bg,
                border_radius=6,
                border=ft.border.all(1, theme.border_subtle),
            )

        return ft.Column(
            [
                mini_top_bar,
                ft.Container(
                    content=ft.Column(
                        [
                            mini_card("Attached", theme.pill_attached_bg, theme.pill_attached_text),
                            ft.Container(height=4),
                            mini_card("Shared", theme.pill_shared_bg, theme.pill_shared_text),
                            ft.Container(height=4),
                            mini_card("Available", theme.pill_available_bg, theme.pill_available_text),
                        ],
                        spacing=0,
                        tight=True,
                    ),
                    padding=ft.padding.all(8),
                    bgcolor=theme.page_bg,
                    expand=True,
                ),
            ],
            spacing=0,
            tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            expand=True,
        )

    def _handle_hover(self, e: ft.ControlEvent) -> None:
        is_hovering = bool(e.data == "true")
        if is_hovering:
            self.border = ft.border.all(2, self._app_theme.accent)
            self.bgcolor = self._app_theme.elevated_surface_bg
            if self._on_hover:
                self._on_hover(self._app_theme)
        else:
            self._apply_selection_style()
        self.update()

    def _handle_click(self, _: ft.ControlEvent) -> None:
        if self._on_click:
            self._on_click(self._app_theme)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._apply_selection_style()
        self.update()

    def _apply_selection_style(self) -> None:
        if self._selected:
            self.border = ft.border.all(2, self._app_theme.accent)
            self.bgcolor = self._app_theme.elevated_surface_bg
        else:
            self.border = ft.border.all(2, "transparent")
            self.bgcolor = "transparent"

    def set_label_color(self, color: str) -> None:
        self._label_color = color
        self._label_text.color = color
        self.update()


def build_theme_step_content(
    ctx: SetupShellContext,
    *,
    page: ft.Page,
    theme_manager: ThemeManager,
    cfg: dict,
    save_config_fn: Callable[[], None],
) -> ft.Control:
    """Build the theme-picker content for the setup shell content slot."""
    theme = ctx.theme
    initial_selected = (cfg.get("theme") or "").strip() or None
    preview_cards_by_theme: dict[str, ThemePreviewCard] = {}

    if initial_selected:
        ctx.mark_completed(True)

    def on_preview_hover(t: AppTheme) -> None:
        theme_manager.preview_theme(t.name)

    def on_preview_click(t: AppTheme) -> None:
        theme_manager.set_theme(t.name)
        cfg["theme"] = t.name
        save_config_fn()
        for name, card in preview_cards_by_theme.items():
            card.set_selected(name == t.name)
        ctx.mark_completed(True)
        page.update()

    preview_cards: list[ft.Control] = []
    for t in [DARK_THEME, LIGHT_THEME]:
        card = ThemePreviewCard(
            t,
            label_color=theme.text_primary,
            on_hover=on_preview_hover,
            on_click=on_preview_click,
        )
        preview_cards_by_theme[t.name] = card
        preview_cards.append(card)

    # Apply pre-selection styling (before mount; set_selected triggers update() which needs page)
    if initial_selected and initial_selected in preview_cards_by_theme:
        c = preview_cards_by_theme[initial_selected]
        c._selected = True
        c._apply_selection_style()

    title_text = ft.Text(
        "Select Your Theme",
        size=20,
        weight=ft.FontWeight.W_600,
        color=theme.text_primary,
    )
    body_text = ft.Text(
        "Choose how you'd like the app to look. You can change this later in Settings.",
        size=13,
        color=theme.text_secondary,
        text_align=ft.TextAlign.CENTER,
    )
    hint_text = ft.Text(
        "Select a theme, then use the right arrow.",
        size=12,
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

    def _on_theme_changed(new_theme: AppTheme) -> None:
        ctx.theme = new_theme
        title_text.color = new_theme.text_primary
        body_text.color = new_theme.text_secondary
        hint_text.color = new_theme.text_muted
        settings_note.color = new_theme.text_muted
        for card in preview_cards_by_theme.values():
            card.set_label_color(new_theme.text_primary)
        page.update()

    ctx.register_theme_listener(_on_theme_changed)

    return ft.Container(
        content=ft.Column(
            [
                title_text,
                ft.Container(height=8),
                body_text,
                ft.Container(height=24),
                ft.Row(
                    preview_cards,
                    spacing=16,
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
                ft.Container(height=8),
                hint_text,
                ft.Container(height=12),
                settings_note,
            ],
            spacing=0,
            tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        padding=ft.padding.only(left=24, right=24, bottom=28),
    )


__all__ = [
    "ThemePreviewCard",
    "calculate_theme_step_preferred_size",
    "calculate_theme_step_preferred_width",
    "build_theme_step_content",
]
