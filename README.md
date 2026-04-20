# USB/IP to WSL Attach Manager

Windows desktop app that lists USB devices (via [usbipd-win](https://github.com/dorssel/usbipd-win)), lets you attach them to a WSL2 distribution, and optionally **remembers** devices so the app keeps working toward attachment while it runs.

- **Product behavior and release intent:** see [AGENTS.md](AGENTS.md)  
- **Prerequisites:** Windows, WSL2, Python 3.10+, and usbipd-win installed; administrator rights for bind/attach.

## Run from source

```text
py -m pip install -r requirements.txt
py main.py
```

Editable install: `py -m pip install -e .` then `usbipd-attach-ui`.

## Windows installer

Locally: install [Inno Setup 6](https://jrsoftware.org/isinfo.php), then from the repo root run `.\scripts\build_installer.ps1` (Produced output is under `dist-installer\`, and is not committed to git.)

A GitHub Action builds the same installer and uploads it on pushes and as a [release](https://github.com/delabrcd/usbip-attach-manager/releases) asset when a release is published.

## License

See [LICENSE](LICENSE).
