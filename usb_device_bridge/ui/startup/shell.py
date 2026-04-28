from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

import flet as ft

from usb_device_bridge.ui.theme import AppTheme, ThemeManager

_log = logging.getLogger(__name__)

StepNavDirection = Literal["prev", "next", "finish"]
StepSize = tuple[float | None, float | None]
StepSizeResolver = Callable[[ft.Page, ft.Control], StepSize]


class SetupShellContext:
    """Passed to each step''s build_content() so it can interact with the shell."""

    def __init__(
        self,
        theme: AppTheme,
        mark_completed_fn: Callable[[bool], None],
        navigate_fn: Callable[[StepNavDirection], None],
        register_theme_listener_fn: Callable[[Callable[[AppTheme], None]], None],
    ) -> None:
        self.theme = theme
        self._mark_completed = mark_completed_fn
        self._navigate = navigate_fn
        self._register_theme_listener = register_theme_listener_fn

    def mark_completed(self, completed: bool = True) -> None:
        """Signal that this step''s required action is done, enabling the next arrow."""
        self._mark_completed(completed)

    def navigate(self, direction: StepNavDirection) -> None:
        """Programmatically trigger a navigation action (e.g. from an in-content button)."""
        self._navigate(direction)

    def register_theme_listener(self, callback: Callable[[AppTheme], None]) -> None:
        """Register a callback that fires whenever the theme changes while this step is active."""
        self._register_theme_listener(callback)


@dataclass(slots=True)
class SetupStepRegistration:
    """Registration for a single setup step."""

    key: str
    should_show: Callable[[], bool]
    initial_completed: Callable[[], bool]
    build_content: Callable[[SetupShellContext], ft.Control]
    on_leave: Callable[[StepNavDirection], Awaitable[None]] | None = None
    size_resolver: StepSizeResolver | None = None
    preferred_width: float | None = None
    preferred_height: float | None = None


class SetupShell:
    """Persistent setup overlay for all first-run setup steps.

    The shell owns the backdrop, card frame, drag handle, progress dots, and nav
    arrows for the entire setup lifetime.  Each step provides only a content builder
    that returns the inner ``ft.Control`` for that step.  Switching between steps
    animates the content slot without tearing down and rebuilding the outer chrome.
    """

    def __init__(
        self,
        page: ft.Page,
        theme: AppTheme,
        theme_manager: ThemeManager | None = None,
        overlay_host: ft.Stack | None = None,
    ) -> None:
        self._page = page
        self._theme = theme
        self._theme_manager = theme_manager
        self._overlay_host = overlay_host

        # Overlay controls (built lazily in run())
        self._overlay: ft.Stack | None = None
        self._content_slot: ft.Container | None = None
        self._card_container: ft.Container | None = None
        self._prev_btn: ft.IconButton | None = None
        self._next_btn: ft.IconButton | None = None
        self._dots_controls: list[ft.Container] = []

        # Navigation state
        self._nav_queue: asyncio.Queue[StepNavDirection] = asyncio.Queue()
        self._current_step: int = 0
        self._total_steps: int = 0
        self._step_completed: bool = False

        # Per-step theme listeners (cleared between steps)
        self._step_theme_listeners: list[Callable[[AppTheme], None]] = []

    # ------------------------------------------------------------------
    # Private: style helpers
    # ------------------------------------------------------------------

    def _nav_btn_style(self, *, enabled: bool) -> ft.ButtonStyle:
        t = self._theme
        color = t.text_on_accent if enabled else t.text_muted
        bg = t.accent if enabled else t.elevated_surface_bg
        bgh = t.accent_hover if enabled else t.elevated_surface_bg
        return ft.ButtonStyle(
            shape=ft.CircleBorder(),
            color=color,
            bgcolor={
                ft.ControlState.DEFAULT: bg,
                ft.ControlState.HOVERED: bgh,
            },
        )

    # ------------------------------------------------------------------
    # Private: build shell overlay (once per run)
    # ------------------------------------------------------------------

    def _build_shell(self) -> None:
        t = self._theme

        # Progress dots
        self._dots_controls = []
        for i in range(self._total_steps):
            active = i == self._current_step
            self._dots_controls.append(
                ft.Container(
                    width=12 if active else 8,
                    height=12 if active else 8,
                    border_radius=6,
                    bgcolor=t.text_muted if active else "transparent",
                    border=ft.border.all(1, t.text_muted),
                    animate=ft.Animation(140, ft.AnimationCurve.EASE_OUT),
                )
            )

        dots_row = ft.Container(
            alignment=ft.Alignment.CENTER,
            padding=ft.padding.only(top=12, bottom=4),
            content=ft.Row(
                self._dots_controls,
                spacing=8,
                tight=True,
                alignment=ft.MainAxisAlignment.CENTER,
            ),
        )

        # Swappable content area — each step''s content is placed here
        self._content_slot = ft.Container(
            content=None,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        )

        self._prev_btn = ft.IconButton(
            icon=ft.Icons.CHEVRON_LEFT,
            icon_size=24,
            tooltip="Previous setup step",
            on_click=self._on_prev_click,
            disabled=True,
            style=self._nav_btn_style(enabled=False),
        )
        self._next_btn = ft.IconButton(
            icon=ft.Icons.CHEVRON_RIGHT,
            icon_size=24,
            tooltip="Next setup step",
            on_click=self._on_next_click,
            disabled=True,
            style=self._nav_btn_style(enabled=False),
        )

        self._card_container = ft.Container(
            content=ft.Column(
                [
                    dots_row,
                    self._content_slot,
                ],
                spacing=0,
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            ),
            bgcolor=t.surface_bg,
            border_radius=16,
            shadow=ft.BoxShadow(
                spread_radius=0,
                blur_radius=40,
                color=ft.Colors.with_opacity(0.35, ft.Colors.BLACK),
                offset=ft.Offset(0, 8),
            ),
        )

        self._overlay = ft.Stack(
            [
                # Non-dismissible backdrop
                ft.Container(
                    expand=True,
                    bgcolor=ft.Colors.with_opacity(0.55, ft.Colors.BLACK),
                ),
                # Centered: [prev]  [card]  [next]
                ft.Container(
                    expand=True,
                    alignment=ft.Alignment.CENTER,
                    content=ft.Row(
                        [
                            ft.Container(
                                content=self._prev_btn,
                                width=56,
                                alignment=ft.Alignment.CENTER,
                            ),
                            self._card_container,
                            ft.Container(
                                content=self._next_btn,
                                width=56,
                                alignment=ft.Alignment.CENTER,
                            ),
                        ],
                        spacing=0,
                        tight=True,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ),
            ]
        )

    def _mount_overlay(self) -> None:
        assert self._overlay is not None
        if self._overlay_host is not None:
            self._overlay_host.controls.append(self._overlay)
            return
        self._page.overlay.append(self._overlay)

    def _unmount_overlay(self) -> None:
        assert self._overlay is not None
        if self._overlay_host is not None:
            if self._overlay in self._overlay_host.controls:
                self._overlay_host.controls.remove(self._overlay)
            return
        if self._overlay in self._page.overlay:
            self._page.overlay.remove(self._overlay)

    # ------------------------------------------------------------------
    # Private: click handlers
    # ------------------------------------------------------------------

    def _on_prev_click(self, _: ft.ControlEvent) -> None:
        self._nav_queue.put_nowait("prev")

    def _on_next_click(self, _: ft.ControlEvent) -> None:
        is_last = self._current_step >= self._total_steps - 1
        self._nav_queue.put_nowait("finish" if is_last else "next")

    # ------------------------------------------------------------------
    # Private: state refresh
    # ------------------------------------------------------------------

    def _refresh_dots(self) -> None:
        t = self._theme
        for i, dot in enumerate(self._dots_controls):
            active = i == self._current_step
            dot.width = 12 if active else 8
            dot.height = 12 if active else 8
            dot.bgcolor = t.text_muted if active else "transparent"

    def _refresh_nav(self) -> None:
        is_last = self._current_step >= self._total_steps - 1
        prev_enabled = self._current_step > 0
        next_enabled = self._step_completed

        if self._prev_btn is not None:
            self._prev_btn.disabled = not prev_enabled
            self._prev_btn.style = self._nav_btn_style(enabled=prev_enabled)

        if self._next_btn is not None:
            self._next_btn.disabled = not next_enabled
            self._next_btn.tooltip = "Finish setup" if is_last else "Next setup step"
            self._next_btn.style = self._nav_btn_style(enabled=next_enabled)

    def _set_step_completed(self, completed: bool) -> None:
        self._step_completed = completed
        self._refresh_nav()
        self._page.update()

    def _put_nav(self, direction: StepNavDirection) -> None:
        self._nav_queue.put_nowait(direction)

    def _register_step_theme_listener(self, cb: Callable[[AppTheme], None]) -> None:
        self._step_theme_listeners.append(cb)

    def _make_ctx(self) -> SetupShellContext:
        return SetupShellContext(
            theme=self._theme,
            mark_completed_fn=self._set_step_completed,
            navigate_fn=self._put_nav,
            register_theme_listener_fn=self._register_step_theme_listener,
        )

    def _retheme_chrome(self, t: AppTheme) -> None:
        """Update all shell chrome controls to match the new theme."""
        if self._card_container is not None:
            self._card_container.bgcolor = t.surface_bg
        self._refresh_dots()
        self._refresh_nav()

    def _on_shell_theme_changed(self, new_theme: AppTheme) -> None:
        """Called by ThemeManager subscriber when the theme changes."""
        self._theme = new_theme
        self._retheme_chrome(new_theme)
        for cb in list(self._step_theme_listeners):
            try:
                cb(new_theme)
            except Exception:
                pass
        self._page.update()

    # ------------------------------------------------------------------
    # Private: content swap with slide animation
    # ------------------------------------------------------------------

    def _wrap_content(
        self,
        content: ft.Control,
        *,
        initial_offset_x: float = 0.0,
    ) -> ft.Container:
        return ft.Container(
            content=content,
            offset=ft.Offset(initial_offset_x, 0),
            animate_offset=ft.Animation(200, ft.AnimationCurve.EASE_IN_OUT),
        )

    def _apply_slot_size(self, *, width: float | None, height: float | None) -> None:
        if self._content_slot is None:
            return
        self._content_slot.width = width
        self._content_slot.height = height

    @staticmethod
    def _dimension_from_control(control: ft.Control, attr: str) -> float | None:
        value = getattr(control, attr, None)
        return float(value) if isinstance(value, (int, float)) else None

    def _resolve_step_size(
        self,
        step: SetupStepRegistration,
        content: ft.Control,
    ) -> StepSize:
        if step.size_resolver is not None:
            return step.size_resolver(self._page, content)

        width = step.preferred_width
        height = step.preferred_height

        if width is None:
            width = self._dimension_from_control(content, "width")
        if height is None:
            height = self._dimension_from_control(content, "height")
        return width, height

    async def _animate_slot_size(
        self,
        *,
        target_width: float | None,
        target_height: float | None,
        duration: float = 0.22,
    ) -> None:
        slot = self._content_slot
        if slot is None:
            return

        current_width = slot.width if isinstance(slot.width, (int, float)) else target_width
        current_height = slot.height if isinstance(slot.height, (int, float)) else target_height

        if current_width == target_width and current_height == target_height:
            return

        if not isinstance(current_width, (int, float)) or not isinstance(target_width, (int, float)):
            # Fall back to immediate apply when either side is unconstrained.
            self._apply_slot_size(width=target_width, height=target_height)
            self._page.update()
            return

        if not isinstance(current_height, (int, float)) or not isinstance(target_height, (int, float)):
            self._apply_slot_size(width=target_width, height=target_height)
            self._page.update()
            return

        steps = 10
        for i in range(1, steps + 1):
            ratio = i / steps
            width = current_width + (target_width - current_width) * ratio
            height = current_height + (target_height - current_height) * ratio
            self._apply_slot_size(width=width, height=height)
            self._page.update()
            await asyncio.sleep(duration / steps)

    async def _swap_content(
        self,
        new_content: ft.Control,
        direction: Literal["forward", "backward"],
        *,
        target_width: float | None,
        target_height: float | None,
    ) -> None:
        slot = self._content_slot
        if slot is None:
            return

        out_x = -0.15 if direction == "forward" else 0.15
        enter_x = -out_x

        size_task = asyncio.create_task(
            self._animate_slot_size(
                target_width=target_width,
                target_height=target_height,
                duration=0.24,
            )
        )

        # Disable nav buttons during transition to prevent double-fire
        if self._prev_btn:
            self._prev_btn.disabled = True
        if self._next_btn:
            self._next_btn.disabled = True

        # Slide current content out
        old = slot.content
        if isinstance(old, ft.Container):
            old.offset = ft.Offset(out_x, 0)
            self._page.update()
            await asyncio.sleep(0.2)

        # Place new wrapper at enter-side, then slide to center
        new_wrapper = self._wrap_content(new_content, initial_offset_x=enter_x)
        slot.content = new_wrapper
        self._page.update()
        await asyncio.sleep(0.02)

        new_wrapper.offset = ft.Offset(0, 0)
        self._page.update()
        await asyncio.sleep(0.22)
        await size_task

        # Re-enable nav buttons (state already set before this call)
        self._refresh_nav()
        self._page.update()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def run(self, *, steps: list[SetupStepRegistration]) -> None:
        """Run all registered setup steps inside the persistent shell.

        Returns when the user finishes or navigates past the last step.
        """
        active = [s for s in steps if s.should_show()]
        if not active:
            return

        self._total_steps = len(active)
        self._current_step = 0
        self._build_shell()

        assert self._overlay is not None
        assert self._content_slot is not None

        if self._theme_manager is not None:
            self._theme_manager.subscribe(self._on_shell_theme_changed)

        # Mount initial content
        self._step_completed = bool(active[0].initial_completed())
        ctx = self._make_ctx()
        initial_content = active[0].build_content(ctx)
        initial_width, initial_height = self._resolve_step_size(active[0], initial_content)
        self._apply_slot_size(width=initial_width, height=initial_height)
        self._content_slot.content = self._wrap_content(initial_content)
        self._refresh_dots()
        self._refresh_nav()
        self._mount_overlay()
        self._page.update()

        try:
            while 0 <= self._current_step < self._total_steps:
                direction = await self._nav_queue.get()
                step = active[self._current_step]
                _log.info("SetupShell: step=%s direction=%s", step.key, direction)

                # Disable buttons immediately while we process the nav action
                if self._prev_btn:
                    self._prev_btn.disabled = True
                if self._next_btn:
                    self._next_btn.disabled = True
                self._page.update()

                if step.on_leave is not None:
                    await step.on_leave(direction)

                if direction == "finish":
                    return

                new_idx = (
                    self._current_step - 1
                    if direction == "prev"
                    else self._current_step + 1
                )
                if new_idx < 0 or new_idx >= self._total_steps:
                    return

                nav_dir: Literal["forward", "backward"] = (
                    "backward" if direction == "prev" else "forward"
                )
                self._current_step = new_idx
                self._step_completed = bool(active[new_idx].initial_completed())

                # Clear per-step listeners before building new step content
                self._step_theme_listeners.clear()
                ctx = self._make_ctx()
                new_content = active[new_idx].build_content(ctx)
                target_width, target_height = self._resolve_step_size(active[new_idx], new_content)
                self._refresh_dots()
                # Nav buttons stay disabled until swap completes
                await self._swap_content(
                    new_content,
                    nav_dir,
                    target_width=target_width,
                    target_height=target_height,
                )

        finally:
            if self._theme_manager is not None:
                self._theme_manager.unsubscribe(self._on_shell_theme_changed)
            self._step_theme_listeners.clear()
            self._unmount_overlay()
            self._page.update()


__all__ = [
    "SetupShell",
    "SetupShellContext",
    "StepSize",
    "StepSizeResolver",
    "SetupStepRegistration",
    "StepNavDirection",
]
