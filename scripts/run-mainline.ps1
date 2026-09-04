<#
Runs monthly mainline detection, then publishes the site payload if changed.
#>
[CmdletBinding()]
param([string]$ProjectRoot = "")

if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $scriptsDir = if ($PSScriptRoot) { $PSScriptRoot }
                  else { Split-Path -Parent (if ($PSCommandPath) { $PSCommandPath } else { $MyInvocation.MyCommand.Path }) }
    $ProjectRoot = Split-Path -Parent $scriptsDir
}

$ErrorActionPreference = "Continue"
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
& $python (Join-Path $ProjectRoot "scripts\monthly_mainline.py")
if ($LASTEXITCODE -ne 0) {
    Write-Error "主线判定失败（exit=$LASTEXITCODE）。"
    exit 2
}
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $ProjectRoot "scripts\publish-site.ps1") -SkipScan
exit $LASTEXITCODE
