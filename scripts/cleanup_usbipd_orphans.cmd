@echo off
setlocal
REM Bypasses per-script execution policy (AllSigned etc.) for this repo tool only.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0cleanup_usbipd_orphans.ps1" %*
exit /b %ERRORLEVEL%
