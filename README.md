# USB Device Bridge for WSL

<div align="center">
  <img src="assets/app_icon_source.png" alt="USB Device Bridge for WSL Logo" width="200" />
</div>

**Easily attach USB devices to Windows Subsystem for Linux (WSL2) with automatic reconnection support.**

A Windows desktop application that simplifies USB device sharing between Windows and Linux in WSL2. Quickly list, attach, and manage USB peripherals in your Linux environment with one-click operations and automatic device reconnection when devices are plugged in.

## Features

- **Simple USB Management:** Browse all USB devices connected to your Windows PC in an intuitive interface
- **Automatic Reconnection:** Mark devices to remember them—USB Device Bridge automatically reattaches them to WSL2 when plugged in while the app is running
- **Per-Device Configuration:** Assign each USB device to a specific WSL distribution
- **GUI Simplicity:** No command-line required—full GUI for device discovery and attachment
- **Persistent Settings:** Your device preferences survive app restarts

## Requirements

- **Windows 10/11** with Administrator rights
- **WSL2** installed and configured
- **[usbipd-win](https://github.com/dorssel/usbipd-win)** (the app guides you to install it if missing)
- **Python 3.10+** (for running from source)

## Quick Start

### Windows Installer

Download the latest installer from [releases](https://github.com/delabrcd/usbip-attach-manager/releases). Run the `.exe` file and follow the setup wizard. The app will prompt you to install any missing prerequisites.

### Run from Source

```bash
py -m pip install -r requirements.txt
py main.py
```

For development with editable install:
```bash
py -m pip install -e .
usb-device-bridge
```

## Build Windows Installer Locally

Install [Inno Setup 6](https://jrsoftware.org/isinfo.php), then run:
```bash
.\scripts\build_installer.ps1
```
Output appears in `dist-installer\`

## Development

- **Product specification:** see [AGENTS.md](AGENTS.md) for functional requirements and architecture
- **Contribution guide:** same document includes code organization guidelines

## License

See [LICENSE](LICENSE).
