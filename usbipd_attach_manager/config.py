from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    d = Path(base) / "usbipd-device-attach-manager"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _config_path() -> Path:
    return app_data_dir() / "config.json"


def default_config() -> dict[str, Any]:
    return {
        "wsl_distro": "",
        "devices": {},
        "apply_on_startup": False,
        "auto_refresh": True,
        "sort_order": "state_attached_first",
        "device_recency": {},
        "minimize_to_tray": False,
    }


def _migrate_legacy_devices(data: dict[str, Any]) -> None:
    """Merge legacy ``remember_instance_ids`` / ``device_wsl_distro`` into ``devices``."""
    devices: dict[str, dict[str, Any]] = {}
    raw = data.get("devices")
    if isinstance(raw, dict):
        for k, v in raw.items():
            if not isinstance(k, str) or not k:
                continue
            if isinstance(v, dict):
                devices[k] = {kk: vv for kk, vv in v.items() if isinstance(kk, str)}
            else:
                devices[k] = {}

    ri = data.get("remember_instance_ids")
    if isinstance(ri, list):
        for iid in ri:
            if isinstance(iid, str) and iid:
                devices.setdefault(iid, {})["remembered"] = True

    wd = data.get("device_wsl_distro")
    if isinstance(wd, dict):
        for k, v in wd.items():
            if not isinstance(k, str) or not k:
                continue
            ent = devices.setdefault(k, {})
            if isinstance(v, str) and v.strip():
                ent["wsl_distro"] = v.strip()

    data["devices"] = devices
    data.pop("remember_instance_ids", None)
    data.pop("device_wsl_distro", None)


def remembered_instance_ids(cfg: dict[str, Any]) -> set[str]:
    """Instance IDs marked Remember (ongoing attach while the app runs)."""
    out: set[str] = set()
    raw = cfg.get("devices")
    if not isinstance(raw, dict):
        return out
    for iid, ent in raw.items():
        if not isinstance(iid, str) or not iid:
            continue
        if isinstance(ent, dict) and ent.get("remembered"):
            out.add(iid)
    return out


def prune_device_entry_if_unused(cfg: dict[str, Any], instance_id: str) -> None:
    """Drop a ``devices`` entry when it only stored ``remembered`` and that is off."""
    if not instance_id:
        return
    devices = cfg.get("devices")
    if not isinstance(devices, dict):
        return
    ent = devices.get(instance_id)
    if not isinstance(ent, dict):
        return
    if ent.get("remembered"):
        return
    if (ent.get("wsl_distro") or "").strip():
        return
    devices.pop(instance_id, None)


def load_config() -> dict[str, Any]:
    p = _config_path()
    if not p.is_file():
        return default_config()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        data.setdefault("wsl_distro", "")
        data.setdefault("apply_on_startup", False)
        data.setdefault("auto_refresh", True)
        data.setdefault("sort_order", "state_attached_first")
        data.setdefault("device_recency", {})
        data.setdefault("minimize_to_tray", False)
        _migrate_legacy_devices(data)
        if not isinstance(data["devices"], dict):
            data["devices"] = {}
        if not isinstance(data["device_recency"], dict):
            data["device_recency"] = {}
        return data
    except (OSError, json.JSONDecodeError):
        return default_config()


def save_config(cfg: dict[str, Any]) -> None:
    p = _config_path()
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
