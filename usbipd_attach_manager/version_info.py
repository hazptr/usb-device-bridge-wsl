from __future__ import annotations

import os
import re
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
