# Agent / contributor guide

This file describes **what the product must do** for users. Implementation (modules, file formats, subprocess details) is left to the codebase and may change as long as the behavior below stays true.

## What this is

A Windows desktop app that lists USB devices (via usbipd-win), lets the user attach them to a WSL2 distribution, and optionally **remembers** devices so the app keeps working toward attachment over time. Administrator rights are required for usbipd bind/attach; the app should obtain elevation in a standard Windows way (e.g. UAC relaunch) without asking users to run obscure commands.

## Functional requirements

### Attachment and WSL choice

- The user can see devices and their state, choose a WSL distro for a device, and connect or disconnect as appropriate.
- **Per-device distro preference** should be meaningful: different devices may target different distros where the UI allows it.

### Remembered devices (core promise)

Unless a task explicitly changes this story, preserve the intent. **How** you implement ongoing attachment (flags, processes, timers) is flexible.

- **Remember means “take care of this for me.”** If the user marks a device as remembered and has a WSL distro in play for it, they expect that device to **end up attached to WSL** whenever it is present **while this app is running**—not only on the first click.
- **No separate “enable auto-attach” control.** Remembering the device is the signal that ongoing attachment is wanted.
- **Discovery without requiring a manual refresh.** If they plug in a remembered device later, the app should **notice** and work toward attaching it without making “Refresh” the only path (timing and UI polish can vary).
- **No background work that assumes a distro when none applies.** If there is no valid WSL target for that device, do not leave stray work running as if there were.
- **Direct “Connect” on a row** can remain a one-shot action; **remembered** devices are the ones that get the ongoing “keep it attached” behavior.

### Persisted settings

- User choices should **survive app restarts** in a sensible location (e.g. under the user’s local app data on Windows). That includes global preferences the UI exposes and **per-device data** tied to a stable device identity (so the same physical device is recognized across sessions). Exact schema and filenames are implementation details; **migrate** old stored data when you change format so users are not silently reset.

### Startup

- If the user opts in, the app should **apply remembered attachment behavior** when it starts, consistent with the rules above (e.g. when WSL distros are available).

### First-run and optional setup

- If usbipd-win (or other prerequisites) are missing, the user should get a guided path to install or fix what’s needed, without assuming deep CLI knowledge.

### Tray and window behavior

- If the product offers minimizing to the notification area, closing or minimizing should follow what the user selected (window vs tray), and the user should be able to open or exit from the tray in a predictable way.
- **Full exit** (window close or Exit from the tray when not using “to tray” for that action) should **detach and unbind** devices that were shared or attached via usbipd, so USB is no longer in use through this app after it closes. **Do not** tear down attachment when a **new instance replaces** the current one (same app handoff), so the replacement can keep working without a gap.

### Resilience

- If attach fails for common Windows reasons (e.g. firewall around WSL networking), the app may attempt a reasonable recovery or clear messaging—exact mechanism is technical detail.

## Environment

- **Python:** 3.10+
- **Dependencies and packaging:** see `pyproject.toml` and `requirements.txt` in the repo.
- **Runtime:** Windows with usbipd-win and WSL2.

## Running locally

```text
py -m pip install -r requirements.txt
py main.py
```

Or: `py -m usbipd_attach_manager` (editable install: `py -m pip install -e .` then `usbipd-attach-ui`).

**Shipped Windows build:** PyInstaller produces an onedir app under `dist\UsbipdWslAttach\`. **Inno Setup 6** packages that into an installer (`.\scripts\build_installer.ps1` after installing Inno Setup).

## Contributing

1. **Scope:** Keep changes focused; avoid unrelated refactors or formatting-only churn.
2. **Consistency:** Match patterns already in the codebase (types, structure) unless you are deliberately improving them in a focused way.
3. **Regression check:** After edits, run `py -m compileall usbipd_attach_manager` and, when behavior or UI is touched, do a quick manual run on Windows.
4. **This document:** Update it when **user-visible behavior** or **product intent** changes—not for every internal refactor.

If you add tests or CI, record the exact commands in the repo (e.g. README or a developer note) so others can run them.

**CI (installer):** On push/PR to `main` or `master`, on **published** GitHub Releases, or via “Run workflow”, `.github/workflows/build-installer.yml` runs on `windows-latest` (PyInstaller, Chocolatey `innosetup`, `scripts\build_installer.ps1` with `USBIPD_BUILD_PYTHON=python`) and saves `dist-installer\` as a workflow artifact. For a **published** release, the same workflow sets `MyAppVersion` in `packaging\UsbipdWslAttach.iss` from the release tag (optional leading `v`, e.g. `v0.2.0` → `0.2.0`), rebuilds, then attaches the setup EXE to that release.
