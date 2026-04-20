from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path
from typing import Any

from usbipd_attach_manager.config import app_data_dir

_CONFIGURED = False
_FAULT_FILE: Any = None
_PREV_SYS_EXCEPTHOOK: Any = None
_PREV_THREAD_EXCEPTHOOK: Any = None


def setup_logging() -> None:
    """Configure file logging (and console when stderr is a TTY). Call once at process start."""
    global _CONFIGURED, _FAULT_FILE, _PREV_SYS_EXCEPTHOOK, _PREV_THREAD_EXCEPTHOOK
    if _CONFIGURED:
        return
    _CONFIGURED = True

    level_name = os.environ.get("USBIPD_ATTACH_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    log_file = app_data_dir() / "app.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=2_000_000,
        backupCount=5,
        encoding="utf-8",
        delay=True,
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    err = getattr(sys, "stderr", None)
    if err is not None and getattr(err, "isatty", lambda: False)():
        console = logging.StreamHandler(err)
        console.setLevel(level)
        console.setFormatter(fmt)
        root.addHandler(console)

    logging.captureWarnings(True)

    log = logging.getLogger(__name__)
    log.info("Logging initialized; log file: %s", log_file)

    _PREV_SYS_EXCEPTHOOK = sys.excepthook

    def _sys_excepthook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: Any,
    ) -> None:
        logging.getLogger("crash").critical(
            "Uncaught exception in main thread",
            exc_info=(exc_type, exc, tb),
        )
        _PREV_SYS_EXCEPTHOOK(exc_type, exc, tb)

    sys.excepthook = _sys_excepthook

    _PREV_THREAD_EXCEPTHOOK = threading.excepthook

    def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
        logging.getLogger("crash").critical(
            "Uncaught exception in thread %r",
            args.thread.name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        _PREV_THREAD_EXCEPTHOOK(args)

    threading.excepthook = _thread_excepthook

    _enable_faulthandler(app_data_dir())


def _enable_faulthandler(log_dir: Path) -> None:
    global _FAULT_FILE
    try:
        import faulthandler
    except ImportError:
        return
    path = log_dir / "fault.txt"
    try:
        _FAULT_FILE = open(path, "a", encoding="utf-8")  # noqa: SIM115
        faulthandler.enable(_FAULT_FILE, all_threads=True)
    except OSError:
        _FAULT_FILE = None


def install_asyncio_exception_logging() -> None:
    """Call from the running asyncio app (e.g. first line of run_app)."""
    import asyncio

    log = logging.getLogger("asyncio")

    def handler(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        msg = context.get("message", "")
        exc = context.get("exception")
        if exc is not None:
            log.error("%s", msg, exc_info=exc)
        else:
            extra = {k: v for k, v in context.items() if k != "message"}
            log.error("%s | context=%s", msg, extra)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.set_exception_handler(handler)
