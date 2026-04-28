from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import TypeAlias

import flet as ft


ConditionResult: TypeAlias = bool | Awaitable[bool]
SetupStepRunner: TypeAlias = Callable[[], Awaitable[None]]
SetupStepCondition: TypeAlias = Callable[[], ConditionResult]


@dataclass(slots=True)
class SetupStep:
    key: str
    run: SetupStepRunner
    should_run: SetupStepCondition | None = None


@dataclass(slots=True)
class SetupPanelState:
    key: str
    index: int
    total: int
    completed: bool = False

    @property
    def prev_enabled(self) -> bool:
        return self.index > 0

    @property
    def next_enabled(self) -> bool:
        return self.completed

    @property
    def is_last(self) -> bool:
        return self.index >= (self.total - 1)

    def mark_completed(self) -> None:
        self.completed = True


async def _should_run_step(step: SetupStep) -> bool:
    if step.should_run is None:
        return True
    result = step.should_run()
    if inspect.isawaitable(result):
        return bool(await result)
    return bool(result)


async def run_setup_sequence(steps: Sequence[SetupStep]) -> None:
    for step in steps:
        if not await _should_run_step(step):
            continue
        await step.run()


def build_setup_navigation_content(
    content_shell: ft.Container,
    *,
    show_navigation: bool,
    on_prev: Callable[[ft.ControlEvent], None] | None = None,
    on_next: Callable[[ft.ControlEvent], None] | None = None,
    prev_enabled: bool = True,
    next_enabled: bool = True,
    prev_tooltip: str = "Previous setup step",
    next_tooltip: str = "Next setup step",
    current_step: int | None = None,
    total_steps: int | None = None,
    include_drag_handle: bool = False,
    drag_handle_color: str | None = None,
    on_drag_double_tap: Callable[[ft.ControlEvent], None] | None = None,
) -> tuple[ft.Control, ft.IconButton | None, ft.IconButton | None]:
    """Wrap setup content with optional left/right step navigation arrows.

    The arrows are placed outside the popup content so switching steps feels like
    navigating a sequence rather than interacting with in-content controls.
    """
    if not show_navigation:
        return content_shell, None, None

    prev_btn = ft.IconButton(
        icon=ft.Icons.CHEVRON_LEFT,
        icon_size=20,
        tooltip=prev_tooltip,
        on_click=on_prev,
        disabled=(on_prev is None) or (not prev_enabled),
        style=ft.ButtonStyle(shape=ft.CircleBorder()),
    )
    next_btn = ft.IconButton(
        icon=ft.Icons.CHEVRON_RIGHT,
        icon_size=20,
        tooltip=next_tooltip,
        on_click=on_next,
        disabled=(on_next is None) or (not next_enabled),
        style=ft.ButtonStyle(shape=ft.CircleBorder()),
    )

    wrapped_core = ft.Row(
        [
            ft.Container(
                content=prev_btn,
                padding=ft.padding.only(right=10),
                alignment=ft.Alignment.CENTER,
            ),
            content_shell,
            ft.Container(
                content=next_btn,
                padding=ft.padding.only(left=10),
                alignment=ft.Alignment.CENTER,
            ),
        ],
        spacing=0,
        tight=True,
        vertical_alignment=ft.CrossAxisAlignment.CENTER,
    )
    top_controls: list[ft.Control] = []

    if isinstance(total_steps, int) and total_steps > 0:
        step = current_step if isinstance(current_step, int) else 0
        dots: list[ft.Control] = []
        for i in range(total_steps):
            active = i == step
            dots.append(
                ft.Container(
                    width=12 if active else 8,
                    height=12 if active else 8,
                    border_radius=6,
                    bgcolor=drag_handle_color if active else "transparent",
                    border=ft.border.all(
                        1,
                        drag_handle_color or "#94a3b8",
                    ),
                    animate=ft.Animation(140, ft.AnimationCurve.EASE_OUT),
                )
            )
        top_controls.append(
            ft.Container(
                alignment=ft.Alignment.CENTER,
                padding=ft.padding.only(bottom=2),
                content=ft.Row(
                    dots,
                    spacing=8,
                    tight=True,
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
            )
        )

    if include_drag_handle:
        top_controls.append(
            ft.WindowDragArea(
                content=ft.GestureDetector(
                    on_double_tap=on_drag_double_tap,
                    content=ft.Container(
                        height=28,
                        alignment=ft.Alignment.CENTER,
                        content=ft.Icon(
                            ft.Icons.DRAG_HANDLE,
                            size=16,
                            color=drag_handle_color,
                        ),
                    ),
                )
            )
        )

    if not top_controls:
        return wrapped_core, prev_btn, next_btn

    wrapped = ft.Column(
        [
            *top_controls,
            wrapped_core,
        ],
        spacing=4,
        tight=True,
        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
    )
    return wrapped, prev_btn, next_btn


def build_setup_panel_chrome(
    content_shell: ft.Container,
    *,
    panel: SetupPanelState,
    on_prev: Callable[[ft.ControlEvent], None] | None,
    on_next: Callable[[ft.ControlEvent], None] | None,
    include_drag_handle: bool,
    drag_handle_color: str | None,
    on_drag_double_tap: Callable[[ft.ControlEvent], None] | None,
) -> tuple[ft.Control, ft.IconButton | None, ft.IconButton | None]:
    return build_setup_navigation_content(
        content_shell,
        show_navigation=True,
        on_prev=on_prev,
        on_next=on_next,
        prev_enabled=panel.prev_enabled,
        next_enabled=panel.next_enabled,
        prev_tooltip="Previous setup step",
        next_tooltip=("Finish setup" if panel.is_last else "Next setup step"),
        current_step=panel.index,
        total_steps=panel.total,
        include_drag_handle=include_drag_handle,
        drag_handle_color=drag_handle_color,
        on_drag_double_tap=on_drag_double_tap,
    )


def close_setup_dialog(page: ft.Page, dialog: ft.AlertDialog | None) -> None:
    """Close only the provided setup dialog instance.

    Using stack-pop style APIs here can race with opening the next step and end up
    dismissing the wrong modal. We close by handle to keep step transitions stable.
    """
    if dialog is not None:
        dialog.open = False
    page.update()
