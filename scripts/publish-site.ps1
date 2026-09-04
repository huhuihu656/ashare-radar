<#
Publishes the daily scan to the public GitHub Pages dashboard.

Pipeline: run scanner -> export sanitized payload -> commit -> push.
The dashboard (docs/) is served by GitHub Pages from the repo, so a successful
push is what makes the site update.  This script never trades or connects to a
broker; it only publishes the research snapshot.

Requirements:
  - .venv exists with the project installed (README).
  - The repo has a `main` branch whose GitHub remote is reachable.  Push
    credentials must be available to the calling context (scheduled task runs
    as the same Windows user that stored them via credential manager).
  - Config file must exist.

Exit codes: 0 ok; 2 scan/export/push failure; 3 not a trading day (ok, nothing
to publish).
#>
[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$Config = "config.yaml",
    [double]$MinCoverage = 0.5,
    [switch]$SkipScan
)

$ErrorActionPreference = "Stop"
Set-Location $ProjectRoot
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$configPath = Join-Path $ProjectRoot $Config
if (!(Test-Path -LiteralPath $python)) { throw "找不到虚拟环境：$python（先按 README 安装）" }
if (!(Test-Path -LiteralPath $configPath)) { throw "找不到配置文件：$configPath" }

if (-not $SkipScan) {
    & $python -m ashare_monitor.cli scan --config $configPath
    if ($LASTEXITCODE -ne 0) {
        Write-Error "扫描失败（exit=$LASTEXITCODE）；保留已上线的旧数据，不发布。"
        exit 2
    }
    # scan exits 0 both on success and on a clean non-trading-day skip; the
    # report directory only appears when a scan actually ran today.
    $today = Get-Date -Format 'yyyyMMdd'
    if (!(Test-Path -LiteralPath (Join-Path $ProjectRoot "data\reports\$today\signals.json")) -or
        !(Test-Path -LiteralPath (Join-Path $ProjectRoot "data\reports\$today\run.json"))) {
        Write-Host "[publish] 今天不是交易日或扫描未生成报告；保留已上线数据，干净退出。"
        exit 3
    }
    Write-Host "[publish] 扫描完成。"
}

# Export the newest complete report to the static payload (coverage-gated).
& $python "$PSScriptRoot\export_dashboard.py" --reports-dir data/reports --out docs/data/latest.json --min-coverage $MinCoverage
if ($LASTEXITCODE -ne 0) {
    Write-Error "导出失败或覆盖率低于发布门槛（$MinCoverage）；已上线的旧数据保持不变。"
    exit 2
}

# Commit + push only when the payload actually changed.
$status = git status --porcelain -- docs/data/latest.json
if ($LASTEXITCODE -ne 0) { Write-Error "git status 失败"; exit 2 }
if ([string]::IsNullOrWhiteSpace($status)) {
    Write-Host "[publish] latest.json 无变化（同一交易日重复运行），跳过提交。"
    exit 0
}
git add -- docs/data/latest.json
if ($LASTEXITCODE -ne 0) { Write-Error "git add 失败"; exit 2 }
git commit -m "docs: 更新 $(Get-Date -Format 'yyyy-MM-dd') 收盘前扫描结果" --quiet
if ($LASTEXITCODE -ne 0) { Write-Error "git commit 失败"; exit 2 }
git push origin main
if ($LASTEXITCODE -ne 0) {
    Write-Error "git push 失败。请检查 GitHub 凭据（git credential-manager）和网络。"
    exit 2
}
Write-Host "[publish] 已推送到 GitHub；Pages 稍后自动更新。"
exit 0
