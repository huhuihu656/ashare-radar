<#
Registers a weekday 15:30 (post-close) China-time task for the current Windows account.
Run after creating .venv and installing the project.  This only schedules the
research scanner + static-site publish chain; it never creates an order or
starts trading software.
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [string]$TaskName = "AshareCloseMonitor",
    [string]$RunAt = "15:20"
)

# $PSScriptRoot can be empty during parameter defaults on Windows PowerShell
# 5.1; resolve the project root here in the body instead.
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $scriptsDir = if ($PSScriptRoot) { $PSScriptRoot }
                  else { Split-Path -Parent (if ($PSCommandPath) { $PSCommandPath } else { $MyInvocation.MyCommand.Path }) }
    $ProjectRoot = Split-Path -Parent $scriptsDir
}

$ErrorActionPreference = "Stop"
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$config = Join-Path $ProjectRoot "config.yaml"
if (!(Test-Path -LiteralPath $python)) { throw "找不到虚拟环境：$python" }
if (!(Test-Path -LiteralPath $config)) { throw "找不到配置文件：$config" }

# Explicitly set the local China Standard Time so the instruction remains clear
# when this script is copied to a machine with a different timezone.
$tz = Get-TimeZone
if ($tz.Id -ne "China Standard Time") {
    Write-Warning "Current timezone is '$($tz.Id)'; Task Scheduler uses local time. Set it to China Standard Time first."
}

# Runs the full publish chain (scan -> export -> git push) so the public
# GitHub Pages dashboard updates automatically after each trading day.
$arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$(Join-Path $ProjectRoot 'scripts\publish-site.ps1')`""
$action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arguments -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $RunAt
# 备份触发：16:30 兜底（若 15:20 因行情未发布而失败；已发布则脚本自动早退）
$triggerBackup = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At "16:30"
# 电池供电时也运行（笔记本拔电不跳过）+ 睡眠可唤醒 + 错过立即补跑
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -WakeToRun
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($trigger, $triggerBackup) -Settings $settings `
    -Description "A-share post-close research scanner; no automated trading." -Force | Out-Null
Write-Host "Created or updated task '$TaskName': weekdays at $RunAt."
Write-Host ('Remove with: Unregister-ScheduledTask -TaskName "{0}" -Confirm:$false' -f $TaskName)
