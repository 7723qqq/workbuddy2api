# =============================================================================
# workbuddy2api 控制台（启动服务 + 菜单）
# -----------------------------------------------------------------------------
# 入口是项目根目录的 start-console.bat：双击即可，先确保服务跑起来，再显示菜单。
#
#   1. 关闭服务              停守护 + 停服务；界面保留，可继续选 2 重启或 3 退出
#   2. 重启服务              先停再起，等端口就绪后回到界面
#   3. 退出界面（不关服务）  关掉本窗口；服务与守护继续在后台运行
#
# 为什么菜单写在 .ps1 而不是 .bat 里：bat 存 UTF-8 中文时，在默认的 GBK 控制台
# 里显示出来就是一堆乱码（现有 .bat 里的中文全在 REM 注释里，从不显示）；
# PowerShell 会按控制台代码页正确转码，中文菜单才显示得对。
#
# 服务与守护都是「脱离会话」启动的（daemon.ps1 -Detach → Start-Process
# -WindowStyle Hidden），所以关掉本窗口、按 Ctrl+C 都不会把它们带走。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\service-console.ps1
#   powershell ... -File scripts\service-console.ps1 -Port 8790 -WaitSec 30
# =============================================================================
param(
    [int]$Port = 8790,
    [int]$WaitSec = 30
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Daemon = Join-Path $PSScriptRoot "daemon.ps1"
$WatchPidFile = Join-Path $Root "logs\daemon.pid"
$DaemonLog = Join-Path $Root "logs\daemon.log"
$ServiceErr = Join-Path $Root "logs\service.err.log"
$AdminUrl = "http://127.0.0.1:$Port/admin"

function Get-ListenerPid {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
         Select-Object -First 1
    if ($c) { return [int]$c.OwningProcess }
    return 0
}

function Get-WatchdogPid {
    if (-not (Test-Path $WatchPidFile)) { return 0 }
    $wpid = 0
    try { $wpid = [int]((Get-Content $WatchPidFile -ErrorAction Stop | Select-Object -First 1).ToString().Trim()) } catch { return 0 }
    if ($wpid -gt 0 -and (Get-Process -Id $wpid -ErrorAction SilentlyContinue)) { return $wpid }
    return 0
}

function Wait-Listener([int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if ((Get-ListenerPid) -gt 0) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return ((Get-ListenerPid) -gt 0)
}

# daemon.ps1 里到处是 exit —— 必须在**子进程**里调用它，
# 直接在本进程调用会把整个控制台界面一起关掉。
function Invoke-Daemon([string[]]$ArgList) {
    $all = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $Daemon) + $ArgList
    & powershell.exe @all 2>&1 | ForEach-Object {
        $line = "$_".TrimEnd()
        if ($line) { Write-Host "  $line" }
    }
}

# 启动服务前先把 MySQL 拉起来：数据库不可用时 uvicorn 要在 lifespan 里重试
# 30×2s 才绑定端口，这段时间 Wait-Listener 一定超时，菜单上就显示"启动失败"。
# 必须用子进程调用：ensure-mysql.ps1 内部是 exit，直接在本进程跑会把控制台关掉。
function Ensure-MySql {
    $p = Join-Path $PSScriptRoot "ensure-mysql.ps1"
    if (-not (Test-Path -LiteralPath $p)) { return }
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $p 2>&1 | ForEach-Object {
        $line = "$_".TrimEnd()
        if ($line) { Write-Host "  $line" }
    }
}

function Read-MenuKey {
    try { return $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown').Character }
    catch { return (Read-Host "  请输入选项 1 / 2 / 3") }
}

function Wait-AnyKey([string]$Tip) {
    Write-Host ""
    Write-Host $Tip -ForegroundColor DarkGray
    try { $null = $Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown') } catch { $null = Read-Host }
}

function Start-ServiceAndWait {
    Write-Host "  正在启动服务…" -ForegroundColor Cyan
    Ensure-MySql
    Invoke-Daemon @('-Detach', '-Port', "$Port")
    if (Wait-Listener $WaitSec) {
        Write-Host ("  启动成功：{0}" -f $AdminUrl) -ForegroundColor Green
        return $true
    }
    Write-Host "  启动失败或超时，请看日志：" -ForegroundColor Red
    Write-Host ("    {0}" -f $DaemonLog)
    Write-Host ("    {0}" -f $ServiceErr)
    return $false
}

function Show-Menu {
    Clear-Host
    $svcPid = Get-ListenerPid
    $wpid = Get-WatchdogPid
    Write-Host "========================================"
    Write-Host "  WorkBuddy2API 控制台"
    Write-Host "========================================"
    if ($svcPid -gt 0) {
        Write-Host ("  服务状态：运行中    PID={0}   端口={1}" -f $svcPid, $Port) -ForegroundColor Green
    } else {
        Write-Host ("  服务状态：未运行    端口={0}" -f $Port) -ForegroundColor Yellow
    }
    if ($wpid -gt 0) {
        Write-Host ("  守护进程：运行中    PID={0}（服务挂掉会自动拉起）" -f $wpid) -ForegroundColor Green
    } else {
        Write-Host "  守护进程：未运行（服务挂了不会自动拉起）" -ForegroundColor Yellow
    }
    Write-Host ("  管理后台：{0}" -f $AdminUrl)
    Write-Host ""
    Write-Host "  1. 关闭服务"
    Write-Host "  2. 重启服务"
    Write-Host "  3. 退出界面（服务继续在后台运行）"
    Write-Host ""
}

# ---- 进入界面：先确保服务在跑 ----------------------------------------------
Clear-Host
Write-Host "  WorkBuddy2API：正在检查服务状态…" -ForegroundColor Cyan
if ((Get-ListenerPid) -gt 0) {
    Write-Host "  服务已在运行。" -ForegroundColor Green
    # 服务在跑但守护可能不在（例如上一轮是手工启动的）。-Detach 是幂等的：
    # 已有守护就直接返回，不会起出第二个。
    Invoke-Daemon @('-Detach', '-Port', "$Port")
} else {
    Start-ServiceAndWait | Out-Null
}

# ---- 菜单 ------------------------------------------------------------------
while ($true) {
    Show-Menu
    $key = "$(Read-MenuKey)"
    if (-not $key) {
        # 输入被重定向或已关闭（例如用管道驱动、终端被关掉）时 Read-Host 返回空 ——
        # 直接退出，不要在原地空转刷屏。真实控制台下按回车拿到的是换行符，不受影响。
        return
    }

    if ($key -eq '1') {
        Write-Host ""
        Invoke-Daemon @('-Stop', '-Port', "$Port")
        if ((Get-ListenerPid) -gt 0) {
            Write-Host "  关闭失败：端口仍被占用，请看日志。" -ForegroundColor Red
        } else {
            Write-Host "  服务已关闭（界面保留：可选 2 重启，或选 3 退出）。" -ForegroundColor Green
        }
        Wait-AnyKey "  按任意键返回菜单…"
    }
    elseif ($key -eq '2') {
        Write-Host ""
        Write-Host "  正在停止旧进程…" -ForegroundColor Cyan
        Invoke-Daemon @('-Stop', '-Port', "$Port")
        Start-ServiceAndWait | Out-Null
        Wait-AnyKey "  按任意键返回菜单…"
    }
    elseif ($key -eq '3') {
        # 服务与守护都是脱离本会话启动的，关掉这个窗口它们照常跑。
        return
    }
    # 其它按键：忽略即可，循环会重画菜单。
}
