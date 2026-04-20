# PyInstaller spec: onedir build (exe + dependencies folder) for bundling in an installer.
# Run from repo root:
#   py -m PyInstaller UsbipdWslAttach.spec
# Then build the setup EXE (requires Inno Setup 6):
#   .\scripts\build_installer.ps1

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

datas = [("assets", "assets")]
_build_version = Path("packaging") / "build_version.txt"
if _build_version.is_file():
    datas += [(str(_build_version), "usbipd_attach_manager")]
binaries = []
hiddenimports = []
tmp_ret = collect_all("flet")
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]
# Flet on Windows uses the separate flet_desktop (pip: flet-desktop) native shell; it is not
# re-exported by the base "flet" tree, so the frozen app must bundle it.
if sys.platform == "win32":
    tmp_ret = collect_all("flet_desktop")
    datas += tmp_ret[0]
    binaries += tmp_ret[1]
    hiddenimports += tmp_ret[2]
for pkg in ("pystray", "PIL"):
    t = collect_all(pkg)
    datas += t[0]
    binaries += t[1]
    hiddenimports += t[2]

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="UsbipdWslAttach",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="assets/app_icon.ico",
    version="packaging/pyinstaller_version_info.txt",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="UsbipdWslAttach",
)
