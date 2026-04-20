from __future__ import annotations

import subprocess
import sys


def parse_wsl_distros() -> list[str]:
    r = subprocess.run(
        ["wsl.exe", "-l", "-v"],
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    if r.returncode != 0:
        return []
    text = (r.stdout or b"").decode("utf-16-le", errors="replace")
    names: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or ("NAME" in line and "STATE" in line):
            continue
        parts = line.lstrip("*").strip().rsplit(None, 2)
        if len(parts) == 3:
            names.append(parts[0])
    return names
