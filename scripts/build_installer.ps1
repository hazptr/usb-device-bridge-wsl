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

# Embed git describe (commits after last v* tag) for the in-app title bar; PyInstaller picks it up from packaging\build_version.txt
$buildVerFile = Join-Path $repoRoot "packaging\build_version.txt"
$installerVerFile = Join-Path $repoRoot "packaging\installer_version.txt"
$oldEap = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$desc = & git -C $repoRoot describe --tags --long --match "v*" --always --dirty 2>$null
$ErrorActionPreference = $oldEap
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($desc)) {
    $desc = & $python -c "import pathlib, sys, tomllib; p=pathlib.Path(sys.argv[1])/'pyproject.toml'; print(tomllib.loads(p.read_bytes().decode())['project']['version'])" $repoRoot
    if ($LASTEXITCODE -ne 0) { $desc = "0.0.0" }
}
$desc = [string]($desc).Trim()
if ((Test-Path -LiteralPath (Join-Path $repoRoot ".git")) -and -not $desc.EndsWith("-dirty")) {
    $oldEap2 = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $porcelain = & git -C $repoRoot status --porcelain 2>$null
    $ErrorActionPreference = $oldEap2
    if ($LASTEXITCODE -eq 0 -and $porcelain) { $desc = "$desc-dirty" }
}
$dirPack = Split-Path -Parent $buildVerFile
if (-not (Test-Path -LiteralPath $dirPack)) { New-Item -ItemType Directory -Path $dirPack -Force | Out-Null }

# Full git describe for app display version (e.g., "v1.0.4-0-gabc123-dirty")
Set-Content -LiteralPath $buildVerFile -Value $desc -Encoding utf8 -NoNewline
Write-Host "Build version (title bar / embedded): $desc" -ForegroundColor Cyan

# Clean semver for installer version (e.g., "1.0.4")
# Extract semver from tag like "v1.0.4" or "v1.0.4-0-gabc123" -> "1.0.4"
$cleanVer = $desc -replace '^v', '' -replace '-.*$', ''
if ($cleanVer -match '^(\d+)\.(\d+)\.(\d+)') {
    $cleanVer = $matches[0]
} else {
    $cleanVer = "0.0.0"
}
Set-Content -LiteralPath $installerVerFile -Value $cleanVer -Encoding utf8 -NoNewline
Write-Host "Installer version: $cleanVer" -ForegroundColor Cyan

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
