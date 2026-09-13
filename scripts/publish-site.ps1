<#
Publishes the daily scan to the public GitHub Pages dashboard.

Pipeline: run scanner -> export sanitized payload -> commit -> push.
The dashboard (docs/) is served by GitHub Pages from the repo, so a successful
push is what makes the site update.  This script never trades or connects to a
broker; it only publishes the research snapshot.

Requirements:
  - .venv exists with the project installed (README).
  - The repo has a `main` branch whose GitHub remote is reachable.  Pushes use
    the repo-level SSH command with the deploy key (see `git config
    core.sshCommand`), which works from the scheduled task without a browser.
  - Config file must exist.

Exit codes: 0 ok; 2 scan/export/push failure; 3 not a trading day (ok, nothing
to publish).
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [string]$Config = "config.yaml",
    [double]$MinCoverage = 0.5,
    [switch]$SkipScan,
    [switch]$NoNotify
)

# $PSScriptRoot can be empty during parameter defaults on Windows PowerShell
# 5.1; resolve the project root here in the body instead.
if ([string]::IsNullOrWhiteSpace($ProjectRoot)) {
    $scriptsDir = if ($PSScriptRoot) { $PSScriptRoot }
                  else { Split-Path -Parent (if ($PSCommandPath) { $PSCommandPath } else { $MyInvocation.MyCommand.Path }) }
    $ProjectRoot = Split-Path -Parent $scriptsDir
}

$ErrorActionPreference = "Stop"
# The machine's Windows system proxy (127.0.0.1:12450) can be down or block
# domestic quote feeds.  Scanner traffic goes direct; SSH pushes are unaffected.
$env:NO_PROXY = "*"
$env:no_proxy = "*"
Set-Location $ProjectRoot
# 日志：便于诊断定时任务失败原因（data/logs/，每日一个文件）
$logDir = Join-Path $ProjectRoot "data\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("publish-" + (Get-Date -Format 'yyyyMMdd-HHmmss') + "-" + $PID + ".log")
try { Start-Transcript -Path $logFile -Append | Out-Null } catch { Write-Warning "日志不可写，继续执行：$_" }
Write-Host ("[publish] " + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + " 开始")
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$configPath = Join-Path $ProjectRoot $Config
if (!(Test-Path -LiteralPath $python)) { throw "找不到虚拟环境：$python（先按 README 安装）" }
if (!(Test-Path -LiteralPath $configPath)) { throw "找不到配置文件：$configPath" }

if (-not $SkipScan) {
    & $python -m ashare_monitor.cli scan --config $configPath
    if ($LASTEXITCODE -ne 0) {
        Write-Error -ErrorAction Continue "扫描失败（exit=$LASTEXITCODE）；保留已上线的旧数据，不发布。"
        exit 2
    }
    # 扫描基准日可能不是"今天"（开机补跑会回退到上一交易日）：
    # 检查最近 12 小时内生成的报告目录（其 run.json 的 scan_time 为准），
    # 而非用 Get-Date 猜日期（曾导致补跑成功却不发布）。
    $reportsRoot = Join-Path $ProjectRoot "data\reports"
    $fresh = Get-ChildItem -LiteralPath $reportsRoot -Directory -Filter "20*" -ErrorAction SilentlyContinue |
        Where-Object {
            (Test-Path -LiteralPath (Join-Path $_.FullName "signals.json")) -and
            (Test-Path -LiteralPath (Join-Path $_.FullName "run.json"))
        } |
        Where-Object { $_.LastWriteTime -gt (Get-Date).AddHours(-12) } |
        Sort-Object Name -Descending | Select-Object -First 1
    if (-not $fresh) {
        Write-Host "[publish] 近 12 小时无新扫描报告；保留已上线数据，干净退出。"
        exit 3
    }
    Write-Host "[publish] 扫描完成（基准日 $($fresh.Name)）。"
}

# Export the newest complete report to the static payload (coverage-gated).
& $python (Join-Path $ProjectRoot "scripts\export_dashboard.py") --reports-dir data/reports --out docs/data/latest.json --min-coverage $MinCoverage
if ($LASTEXITCODE -ne 0) {
    Write-Error -ErrorAction Continue "导出失败或覆盖率低于发布门槛（$MinCoverage）；已上线的旧数据保持不变。"
    exit 2
}

# ---- 第一批：核心数据立即提交推送（保证按时上线）----
$status = git status --porcelain -- docs/data/latest.json docs/data/klines.json docs/data/mainline.json docs/data/archive
if ($LASTEXITCODE -ne 0) { Write-Error -ErrorAction Continue "git status 失败"; exit 2 }
if ([string]::IsNullOrWhiteSpace($status)) {
    Write-Host "[publish] 核心数据无变化，跳过提交。"
} else {
    git add -- docs/data/latest.json docs/data/klines.json docs/data/mainline.json docs/data/archive docs/data/archive
    if ($LASTEXITCODE -ne 0) { Write-Error -ErrorAction Continue "git add 失败"; exit 2 }
    git commit -m "docs: 更新 $(Get-Date -Format 'yyyy-MM-dd') 收盘扫描结果" --quiet
    if ($LASTEXITCODE -ne 0) { Write-Error -ErrorAction Continue "git commit 失败"; exit 2 }
    git push origin main
    if ($LASTEXITCODE -ne 0) {
        Write-Error -ErrorAction Continue "git push 失败。请检查本机 git/SSH 配置与网络。"
        exit 2
    }
    Write-Host "[publish] 核心数据已推送到 GitHub；Pages 稍后自动更新。"
}

# ---- 微信通知（Server酱，非阻塞：失败不影响发布；-NoNotify 时跳过）----
if ($NoNotify) {
    Write-Host "[publish] -NoNotify：跳过微信通知。"
} else {
Write-Host "[publish] 发送微信通知…"
& $python (Join-Path $ProjectRoot "scripts\daily_notify.py")
if ($LASTEXITCODE -ne 0) {
    Write-Warning "[publish] 微信通知发送失败（exit=$LASTEXITCODE）；发布不受影响。"
}
}

# ---- 第二批：战绩追踪与模拟盘（重计算，非阻塞，单独提交）----
Write-Host "[publish] 刷新信号战绩（tracked.json）…"
& $python (Join-Path $ProjectRoot "scripts\signal_track.py") --out docs/data/tracked.json
if ($LASTEXITCODE -ne 0) {
    Write-Warning "[publish] 信号战绩刷新失败（exit=$LASTEXITCODE）；核心发布不受影响。"
}
$status2 = git status --porcelain -- docs/data/tracked.json
if ($LASTEXITCODE -ne 0) { Write-Warning "[publish] git status(2) 失败" }
elseif (-not [string]::IsNullOrWhiteSpace($status2)) {
    git add -- docs/data/tracked.json
    git commit -m "docs: 更新信号战绩" --quiet
    if ($LASTEXITCODE -eq 0) { git push origin main }
}

Write-Host "[publish] 已推送到 GitHub；Pages 稍后自动更新。"
Stop-Transcript | Out-Null
exit 0
