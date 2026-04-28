from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from usb_device_bridge.version_info import version_is_newer

_GITHUB_LATEST_RELEASE_URL = (
    "https://api.github.com/repos/delabrcd/usbip-attach-manager/releases/latest"
)
_HTTP_USER_AGENT = "usbipd-attach-manager-updater"
_INSTALLER_NAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class AvailableUpdate:
    version: str
    tag_name: str
    installer_name: str
    installer_url: str


@dataclass(frozen=True)
class DownloadedUpdate:
    version: str
    installer_path: Path


def _strip_tag_prefix(tag_name: str) -> str:
    text = (tag_name or "").strip()
    if len(text) > 1 and text[0] in ("v", "V") and text[1].isdigit():
        return text[1:].strip()
    return text


def _safe_installer_name(asset_name: str) -> str:
    base = _INSTALLER_NAME_SAFE.sub("_", (asset_name or "").strip()).strip("._")
    return base or "UsbipdWslAttach-Setup.exe"


def _pick_installer_asset(release_data: dict[str, object]) -> tuple[str, str] | None:
    assets = release_data.get("assets")
    if not isinstance(assets, list):
        return None

    best_name = ""
    best_url = ""
    best_rank = -1
    for raw in assets:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        url = str(raw.get("browser_download_url") or "").strip()
        if not name or not url:
            continue
        if not name.lower().endswith(".exe"):
            continue

        lowered = name.lower()
        rank = 1
        if "setup" in lowered:
            rank += 2
        if "usbipd" in lowered and "wsl" in lowered:
            rank += 1
        if rank > best_rank:
            best_rank = rank
            best_name = name
            best_url = url

    if not best_name or not best_url:
        return None
    return best_name, best_url


def check_for_available_update(
    current_version: str,
    *,
    timeout: float = 20.0,
) -> AvailableUpdate | None:
    request = urllib.request.Request(
        _GITHUB_LATEST_RELEASE_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": _HTTP_USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except (TimeoutError, OSError, urllib.error.HTTPError, urllib.error.URLError):
        return None

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None

    release_version = _strip_tag_prefix(str(parsed.get("tag_name") or ""))
    if not release_version:
        return None
    if not version_is_newer(release_version, current_version):
        return None

    picked = _pick_installer_asset(parsed)
    if not picked:
        return None
    installer_name, installer_url = picked

    return AvailableUpdate(
        version=release_version,
        tag_name=str(parsed.get("tag_name") or "").strip() or release_version,
        installer_name=installer_name,
        installer_url=installer_url,
    )


def download_update_installer(
    update: AvailableUpdate,
    *,
    target_dir: Path,
    timeout: float = 45.0,
) -> DownloadedUpdate | None:
    target_dir.mkdir(parents=True, exist_ok=True)

    safe_name = _safe_installer_name(update.installer_name)
    target_file = target_dir / f"{update.version}-{safe_name}"
    if target_file.is_file() and target_file.stat().st_size > 0:
        return DownloadedUpdate(version=update.version, installer_path=target_file)

    tmp_file = target_file.with_suffix(target_file.suffix + ".part")

    request = urllib.request.Request(
        update.installer_url,
        headers={"User-Agent": _HTTP_USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            with open(tmp_file, "wb") as out:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
    except (TimeoutError, OSError, urllib.error.HTTPError, urllib.error.URLError):
        try:
            if tmp_file.exists():
                tmp_file.unlink()
        except OSError:
            pass
        return None

    try:
        os.replace(tmp_file, target_file)
    except OSError:
        try:
            if tmp_file.exists():
                tmp_file.unlink()
        except OSError:
            pass
        return None

    return DownloadedUpdate(version=update.version, installer_path=target_file)
