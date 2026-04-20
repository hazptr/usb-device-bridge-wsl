from __future__ import annotations

import os
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path


def is_dev_source_launch() -> bool:
    """
    True when this process is running from source (not a frozen build), e.g.
    ``py .\\main.py`` or ``py -m usbipd_attach_manager``. Used so a running dev
    instance always yields to a newly started one for faster iteration.

    Set ``USBIPD_ATTACH_DEV=1`` to force dev behavior when detection does not apply.
    """
    if getattr(sys, "frozen", False):
        return False
    env = os.environ.get("USBIPD_ATTACH_DEV", "").strip().lower()
    if env in ("1", "true", "yes"):
        return True
    try:
        if Path(sys.argv[0]).resolve().name.lower() == "main.py":
            return True
    except (OSError, ValueError):
        pass
    main_mod = sys.modules.get("__main__")
    mf = getattr(main_mod, "__file__", None) if main_mod else None
    if mf:
        try:
            p = Path(mf).resolve()
            if p.name.lower() == "__main__.py":
                if "usbipd_attach_manager" in [x.lower() for x in p.parts]:
                    return True
        except (OSError, ValueError):
            pass
    return False


@lru_cache
def get_app_version() -> str:
    """Project / package version (e.g. from importlib metadata)."""
    try:
        from importlib.metadata import version

        return version("usbipd-device-attach-manager")
    except Exception:
        return "0.0.0"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _strip_v_prefix(s: str) -> str:
    t = s.strip()
    if len(t) > 1 and t[0] in "vV" and t[1].isdigit():
        return t[1:].lstrip()
    return t


def _read_frozen_build_version() -> str | None:
    if not getattr(sys, "frozen", False):
        return None
    meip = getattr(sys, "_MEIPASS", None)
    if not meip:
        return None
    p = Path(meip) / "usbipd_attach_manager" / "build_version.txt"
    try:
        t = p.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return _strip_v_prefix(t) if t else None


def _git_describe_text() -> str | None:
    if getattr(sys, "frozen", False):
        return None
    root = _repo_root()
    if not (root / ".git").is_dir():
        return None
    try:
        cp = subprocess.run(
            (
                "git",
                "-C",
                str(root),
                "describe",
                "--tags",
                "--long",
                "--match",
                "v*",
                "--always",
            ),
            capture_output=True,
            text=True,
            timeout=8.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0 or not (cp.stdout or "").strip():
        return None
    return _strip_v_prefix(cp.stdout.strip())


@lru_cache
def get_display_version() -> str:
    """
    Version string for UI, including commits-after-tag from ``git describe``
    (or the same string embedded at PyInstaller build time). Falls back to
    :func:`get_app_version` when neither is available.
    """
    frozen = _read_frozen_build_version()
    if frozen:
        return frozen
    g = _git_describe_text()
    if g:
        return g
    return get_app_version()


def _version_tuple(v: str) -> tuple[int, ...]:
    """Parse semver-like strings for ordering (dev/pre-release suffixes stripped)."""
    v = v.strip()
    v = re.split(r"[-+]", v, maxsplit=1)[0].strip()
    parts: list[int] = []
    for seg in v.split("."):
        m = re.match(r"^(\d+)", seg)
        parts.append(int(m.group(1)) if m else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:8])


def version_is_newer(a: str, b: str) -> bool:
    """True iff version string a is strictly greater than b."""
    return _version_tuple(a) > _version_tuple(b)
