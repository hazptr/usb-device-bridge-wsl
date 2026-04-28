from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def test_first_time_setup_requested() -> bool:
    if "--test-first-time-setup" in sys.argv:
        return True
    v = os.environ.get(
        "USBIPD_ATTACH_MANAGER_TEST_FIRST_TIME_SETUP", ""
    ).strip().lower()
    return v in ("1", "true", "yes", "on")


def device_list_fingerprint(
    devs: list[dict[str, Any]],
    order: str,
    cfg: dict[str, Any],
    *,
    manual_attaching: set[str],
    auto_attaching_ids: set[str],
    auto_failed_ids: set[str],
    auto_long_wait_ids: set[str],
) -> str:
    """Stable hash for whether the rendered device list would change."""
    normalized = sorted(devs, key=lambda d: d.get("InstanceId") or "")
    dev_prefs = sorted(
        (k, sorted(ent.items()))
        for k, ent in (cfg.get("devices") or {}).items()
        if isinstance(ent, dict)
    )
    return json.dumps(
        {
            "d": normalized,
            "o": order,
            "r": cfg.get("device_recency") or {},
            "dev": dev_prefs,
            "m": sorted(manual_attaching),
            "a": sorted(auto_attaching_ids),
            "f": sorted(auto_failed_ids),
            "l": sorted(auto_long_wait_ids),
        },
        sort_keys=True,
        default=str,
    )


def assets_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "assets"
    return Path(__file__).resolve().parent.parent.parent / "assets"
