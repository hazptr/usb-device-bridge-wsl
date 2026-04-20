#Requires -Version 5.1
<#
.SYNOPSIS
  Find or stop leftover "usbipd attach" processes (and their child trees) without rebooting.

.DESCRIPTION
  Matches Win32 processes named usbipd.exe whose command line contains "attach".
  Those are the long-running background jobs from "usbipd attach ... -a" / attach --wsl.
  Uses taskkill /T /F so wsl.exe / conhost.exe under that tree are torn down too.

  Optional -ShutdownWsl terminates all WSL 2 VMs (wsl --shutdown). Use if Task Manager
  still shows stray "Windows Subsystem for Linux" after usbipd trees are gone — that
  closes every WSL session, not only USB/IP.

  If not already running elevated, the script prompts for Administrator (UAC) and
  re-launches itself with the same -Kill / -ShutdownWsl flags.

  If running the .ps1 fails with "not digitally signed" / execution policy, run
  cleanup_usbipd_orphans.cmd instead, or: powershell -ExecutionPolicy Bypass -File ...

.PARAMETER Kill
  Actually terminate matching processes. Without this flag, only lists PIDs.

.PARAMETER ShutdownWsl
  After optional -Kill, run "wsl --shutdown" to stop all WSL2 virtual machines.

.EXAMPLE
  .\cleanup_usbipd_orphans.cmd
  List usbipd attach PIDs only (use .cmd when .ps1 is blocked by execution policy).

.EXAMPLE
  .\cleanup_usbipd_orphans.cmd -Kill
  End those process trees.

.EXAMPLE
  .\cleanup_usbipd_orphans.cmd -Kill -ShutdownWsl
  End usbipd trees, then shut down all WSL2 VMs.
#>
param(
    [switch]$Kill,
    [switch]$ShutdownWsl
)

$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
    try {
        $principal = [Security.Principal.WindowsPrincipal]::new(
            [Security.Principal.WindowsIdentity]::GetCurrent()
        )
        return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch {
        return $false
    }
}

if (-not (Test-IsAdministrator)) {
    $scriptPath = $PSCommandPath
    if (-not $scriptPath) {
        Write-Error "Cannot determine script path; run with: powershell -File `"path\to\cleanup_usbipd_orphans.ps1`""
        exit 1
    }
    $argList = @(
        '-NoProfile'
        '-ExecutionPolicy'
        'Bypass'
        '-File'
        $scriptPath
    )
    if ($Kill) { $argList += '-Kill' }
    if ($ShutdownWsl) { $argList += '-ShutdownWsl' }
    Write-Host "Requesting Administrator approval (UAC)..."
    try {
        $elevated = Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
            -Verb RunAs `
            -ArgumentList $argList `
            -Wait `
            -PassThru
        exit $(if ($null -ne $elevated.ExitCode) { $elevated.ExitCode } else { 0 })
    } catch {
        Write-Error "Administrator elevation was cancelled or failed: $_"
        exit 1
    }
}

function Get-UsbipdAttachProcesses {
    Get-CimInstance Win32_Process -Filter "Name = 'usbipd.exe'" |
        Where-Object {
            $cmd = $_.CommandLine
            if (-not $cmd) { return $false }
            return ($cmd -match '(?i)\battach\b')
        }
}

$list = @(Get-UsbipdAttachProcesses)

if ($list.Count -eq 0) {
    Write-Host "No usbipd.exe processes with 'attach' in the command line were found."
} else {
    foreach ($p in $list) {
        $line = if ($p.CommandLine) { $p.CommandLine } else { "(no command line)" }
        Write-Host ("PID {0,-6} {1}" -f $p.ProcessId, $line)
    }

    if (-not $Kill) {
        Write-Host ""
        Write-Host "Dry run only. Re-run with -Kill to terminate these process trees (taskkill /T /F)."
    } else {
        foreach ($p in $list) {
            $procId = $p.ProcessId
            Write-Host "Stopping tree at PID $procId ..."
            & taskkill.exe /PID $procId /T /F | Out-Null
            if ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 128) {
                Write-Warning "taskkill exited with code $LASTEXITCODE for PID $procId"
            }
        }
        Write-Host "Done."
    }
}

if ($ShutdownWsl) {
    if (-not $Kill -and $list.Count -gt 0) {
        Write-Warning "You asked for -ShutdownWsl without -Kill; usbipd processes may still be running."
    }
    Write-Host ""
    Write-Host "Running: wsl --shutdown (stops all WSL 2 distributions / VMs) ..."
    & wsl.exe --shutdown
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "wsl --shutdown exited with code $LASTEXITCODE (is WSL installed?)"
    } else {
        Write-Host "WSL shutdown complete."
    }
}
