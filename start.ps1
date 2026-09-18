# =====================================================================
#  CanaryGuard AntiFraud v1.0.0 - master server one-click launcher
#  (PowerShell version; UTF-8 with BOM, Chinese output safe)
#
#  Usage:
#    powershell -ExecutionPolicy Bypass -File start.ps1
#    or right-click -> "Run with PowerShell" in Windows Explorer.
#  Equivalent to start.bat but without any cmd code-page concern.
#
#  Three-state logic (same as start.bat):
#    1. master NOT running -> start it in a new window, health-probe,
#       then open the browser at /ui/
#    2. master already running -> do NOT start a second one; just open
#       another browser tab at /ui/
#    3. health probe failed -> keep the master window, warn the user to
#       check its log, do not exit silently
# =====================================================================

# Project root: this script's own directory (robust even for non-ASCII paths).
if ([string]::IsNullOrWhiteSpace($PSScriptRoot)) {
    $ProjectRoot = 'C:\Users\Administrator\Desktop\知乎黑客松\金丝雀蜜罐'
} else {
    $ProjectRoot = $PSScriptRoot
}
$Port = 9200
$HealthUrl = "http://127.0.0.1:$Port/api/v1/system/health"
$UiUrl = "http://127.0.0.1:$Port/ui/"

Set-Location -Path $ProjectRoot

$ActivateBat = Join-Path $ProjectRoot '.venv\Scripts\activate.bat'
$VenvPython  = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $ActivateBat)) {
    Write-Host ''
    Write-Host '[af] 未找到 .venv，请先执行以下命令：' -ForegroundColor Yellow
    Write-Host '     python -m venv .venv'
    Write-Host '     .venv\Scripts\pip install -r requirements.txt'
    Write-Host '     (可选 WebUI) cd webui; npm install; npm run build; cd ..'
    Write-Host ''
    Read-Host '按回车退出'
    exit 1
}

if (-not (Test-Path $VenvPython)) {
    Write-Host '[af] 未找到 .venv\Scripts\python.exe，请重建虚拟环境：python -m venv .venv' -ForegroundColor Yellow
    Read-Host '按回车退出'
    exit 1
}

# ---- detect whether the master already listens on PORT ----
$ListenerLine = netstat -ano | Select-String -Pattern ":$Port\s+.*LISTENING" | Select-Object -First 1
$FoundPid = $null
if ($ListenerLine) {
    $parts = ($ListenerLine.ToString().Trim() -split '\s+')
    if ($parts.Count -ge 5) { $FoundPid = $parts[-1] }
}

# ---- P1-5 single-instance guard: clean up zombie instances ------------------
# A zombie is a python process still running `app.run --port 9200` but NOT holding
# the port (dual-instance race: two launchers started in the same second, only one
# bound 9200; the other keeps its scheduler alive and spams errors / may double
# process data). Kill every such process EXCEPT the current listener PID and its
# parent: the venv launcher (`Scripts\python.exe`) is a redirector process that
# spawns the real interpreter as a child (which binds the port) — both show the
# `app.run --port 9200` command line, so the listener's PARENT must be protected
# too (R1 修复事故复盘：曾把健康实例的 venv 重定向父进程误当僵尸杀掉，连带主控下线)。
$zombies = @()
try {
    $safePids = @()
    if ($FoundPid) {
        $safePids += [int]$FoundPid
        $portOwner = Get-CimInstance Win32_Process -Filter "ProcessId=$FoundPid" -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($portOwner -and $portOwner.ParentProcessId) {
            $safePids += [int]$portOwner.ParentProcessId   # venv 重定向父进程
        }
    }
    $zombies = Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object {
            ($_.ProcessId -notin $safePids) -and
            $_.CommandLine -match 'app\.run' -and
            $_.CommandLine -match '--port 9200'
        }
} catch {
    $zombies = @()   # CIM unavailable -> skip cleanup, never block startup
}
foreach ($z in $zombies) {
    Write-Host "[af] 清理僵尸实例 PID $($z.ProcessId)（app.run 但未持有端口 $Port）..." -ForegroundColor Yellow
    Stop-Process -Id $z.ProcessId -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 200
}

if ($FoundPid) {
    # ---- state 2: already running ----
    Write-Host "[af] 主控已在运行（端口 $Port，PID $FoundPid），不再重复启动。" -ForegroundColor Green
    Start-Process $UiUrl
    Write-Host "[af] 已新开浏览器标签页: $UiUrl" -ForegroundColor Green
    exit 0
}

# ---- state 1: start master in a new window ----
Write-Host '[af] 主控未运行，正在新窗口启动...' -ForegroundColor Cyan
Start-Process -FilePath 'cmd.exe' -ArgumentList @('/k', ('"{0}" -m app.run --host 127.0.0.1 --port {1}' -f $VenvPython, $Port))

# ---- P3-5: 启动后复核——收窄双启动互杀窗口 ----
# 等 ~2s 让本实例绑定端口，再清一次僵尸：谁持有端口（及其 venv 重定向父进程）
# 受保护，同秒双启动中落败的一对进程被清理（每次启动收敛为单实例）。
Start-Sleep -Seconds 2
$ListenerLine2 = netstat -ano | Select-String -Pattern ":$Port\s+.*LISTENING" | Select-Object -First 1
if ($ListenerLine2) {
    $parts2 = ($ListenerLine2.ToString().Trim() -split '\s+')
    if ($parts2.Count -ge 5) {
        try {
            $safePids2 = @([int]$parts2[-1])
            $owner2 = Get-CimInstance Win32_Process -Filter "ProcessId=$($parts2[-1])" -ErrorAction SilentlyContinue |
                Select-Object -First 1
            if ($owner2 -and $owner2.ParentProcessId) { $safePids2 += [int]$owner2.ParentProcessId }
            Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                Where-Object {
                    ($_.ProcessId -notin $safePids2) -and
                    $_.CommandLine -match 'app\.run' -and
                    $_.CommandLine -match '--port 9200'
                } | ForEach-Object {
                    Write-Host "[af] 启动后复核：清理僵尸实例 PID $($_.ProcessId)..." -ForegroundColor Yellow
                    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
                    Start-Sleep -Milliseconds 200
                }
        } catch { }   # 复核清理失败不阻塞启动
    }
}

# ---- health probe (up to ~20 seconds) ----
$Healthy = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    try {
        $r = Invoke-WebRequest -Uri $HealthUrl -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $Healthy = $true; break }
    } catch {
        # not up yet, keep probing
    }
}

if ($Healthy) {
    Write-Host "[af] 主控健康检查通过: $HealthUrl" -ForegroundColor Green
    Write-Host "[af] admin key: $ProjectRoot\data\bootstrap_admin_key.txt" -ForegroundColor Yellow
    Write-Host '     (可用 WebUI 登录页的一键登录按钮，无需手动读取)'
    Start-Process $UiUrl
    Write-Host "[af] 已打开浏览器: $UiUrl" -ForegroundColor Green
} else {
    # ---- state 3: probe failed ----
    Write-Host '[af] 警告：主控窗口已打开，但 20 秒内健康检查未通过。' -ForegroundColor Red
    Write-Host '[af] 请查看 "CanaryGuard Master" 窗口日志（端口冲突？venv 问题？）。' -ForegroundColor Red
    Write-Host "[af] 也可手动打开界面: $UiUrl" -ForegroundColor Yellow
}
