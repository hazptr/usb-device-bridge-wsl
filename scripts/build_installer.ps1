# Builds the PyInstaller onedir app, then compiles the Inno Setup installer.
# Prerequisites: Python with PyInstaller, Inno Setup 6 (ISCC.exe on PATH or default location).

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

# USBIPD_BUILD_PYTHON: optional override (e.g. "python" on GitHub Actions so `py` does not select the wrong version).
$python = $env:USBIPD_BUILD_PYTHON
if (-not $python) {
    if (Get-Command py -ErrorAction SilentlyContinue) { $python = "py" }
    else { $python = "python" }
}

Write-Host "PyInstaller (onedir) via $python..." -ForegroundColor Cyan
& $python -m PyInstaller --noconfirm UsbipdWslAttach.spec
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$iscc = $null
foreach ($c in @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
    )) {
    if (Test-Path -LiteralPath $c) {
        $iscc = $c
        break
    }
}
if (-not $iscc) {
    $cmd = Get-Command iscc -ErrorAction SilentlyContinue
    if ($cmd) { $iscc = $cmd.Source }
}

if (-not $iscc) {
    Write-Error "Inno Setup 6 not found. Install from https://jrsoftware.org/isinfo.php " `
        "or add ISCC.exe to PATH. Onedir build is in dist\UsbipdWslAttach\"
}

Write-Host "Inno Setup compiler: $iscc" -ForegroundColor Cyan
& $iscc (Join-Path $repoRoot "packaging\UsbipdWslAttach.iss")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Done. Output: dist-installer\UsbipdWslAttach-Setup-<version>.exe (see #define MyAppVersion in packaging\UsbipdWslAttach.iss)" -ForegroundColor Green
