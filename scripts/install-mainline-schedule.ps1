<#
Registers the monthly mainline-sector task for the current Windows account.
Runs on calendar days 1-3 at 15:20; the Python script itself exits quietly
unless today is the FIRST TRADING DAY of the month, so holiday/weekend
month-starts are handled by the data, not the calendar.
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [string]$TaskName = "AshareMonthlyMainline"
)

# $PSScriptRoot can be empty during parameter defaults on Windows PowerShell
# 5.1; resolve the project root in the body instead.
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $scriptsDir = if ($PSScriptRoot) { $PSScriptRoot }
                  else { Split-Path -Parent (if ($PSCommandPath) { $PSCommandPath } else { $MyInvocation.MyCommand.Path }) }
    $ProjectRoot = Split-Path -Parent $scriptsDir
}

$ErrorActionPreference = "Stop"
$runner = Join-Path $ProjectRoot "scripts\run-mainline.ps1"
if (!(Test-Path -LiteralPath $runner)) { throw "找不到 $runner" }

$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $ProjectRoot
# 本机 ScheduledTasks 模块无 Monthly 参数集：工作日 15:20 每日触发，
# monthly_mainline.py 内置"本月首个交易日"守卫，非首日秒退。
$trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "15:20"
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Monthly mainline-sector detection (first trading day guarded); research only." -Force | Out-Null
Write-Host "Created task '$TaskName': 工作日 15:20（脚本内置首交易日守卫，仅月初首个交易日生效）。"
