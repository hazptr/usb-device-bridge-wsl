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
    ``py .\\main.py`` or ``py -m usb_device_bridge``. Used so a running dev
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
                if "usb_device_bridge" in [x.lower() for x in p.parts]:
                    return True
        except (OSError, ValueError):
            pass
    return False


@lru_cache
def get_app_version() -> str:
    """Project / package version from setuptools-scm generated _version.py or importlib metadata."""
    # 1) Try setuptools-scm generated version file (present in dev builds from git)
    try:
        from usb_device_bridge._version import __version__

        return __version__
    except Exception:
        pass
    # 2) Try importlib metadata (installed package)
    try:
        from importlib.metadata import version

        return version("usb-device-bridge-wsl")
    except Exception:
        pass
    # 3) For frozen builds, read the version embedded at PyInstaller build time
    frozen = _read_frozen_build_version()
    if frozen:
        return frozen
    # 4) Try git describe directly (fallback for dev source runs)
    g = _git_describe_text()
    if g:
        return g
    return "0.0.0"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _strip_v_prefix(s: str) -> str:
    t = s.strip()
    if len(t) > 1 and t[0] in "vV" and t[1].isdigit():
        return t[1:].lstrip()
    return t


def _format_describe_for_display(s: str) -> str:
    """
    ``git describe --long`` on an exact tag looks like ``1.0.1-0-gabc``. For
    release builds, show only the tag / semver part (plus ``-dirty`` if present).
    """
    t = s.strip()
    m = re.match(
        r"^(.*)-(\d+)-g([0-9a-f]+)(-dirty)?$",
        t,
        flags=re.IGNORECASE,
    )
    if not m:
        return t
    prefix, distance, dirty = m.group(1), int(m.group(2)), m.group(4)
    if distance != 0:
        return t
    return prefix + (dirty or "")


def _read_frozen_build_version() -> str | None:
    if not getattr(sys, "frozen", False):
        return None
    meip = getattr(sys, "_MEIPASS", None)
    if not meip:
        return None
    p = Path(meip) / "usb_device_bridge" / "build_version.txt"
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
                "--dirty",
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


def _git_worktree_dirty() -> bool:
    root = _repo_root()
    if not (root / ".git").is_dir():
        return False
    try:
        cp = subprocess.run(
            ("git", "-C", str(root), "status", "--porcelain"),
            capture_output=True,
            text=True,
            timeout=8.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return cp.returncode == 0 and bool((cp.stdout or "").strip())


@lru_cache
def get_display_version() -> str:
    """
    Version string for UI, including commits-after-tag from ``git describe``
    (or the same string embedded at PyInstaller build time). Uncommitted
    changes append ``-dirty``. Checkouts exactly on a release tag show only
    the semver (no ``-0-g<hash>``). Falls back to :func:`get_app_version`
    when neither is available.
    """
    frozen = _read_frozen_build_version()
    if frozen:
        return _format_describe_for_display(frozen)
    g = _git_describe_text()
    if g:
        return _format_describe_for_display(g)
    base = get_app_version()
    if _git_worktree_dirty():
        return f"{base}-dirty"
    return base


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
