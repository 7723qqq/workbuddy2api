# =============================================================================
# workbuddy2api 守护脚本
# -----------------------------------------------------------------------------
# 解决的问题：把 `python main.py` 挂在某个终端 / Agent 后台会话里跑，
# 会话结束时子进程会被连带回收 —— 日志最后一行还是 "200 OK"，但端口没了。
#
# 本脚本用 Start-Process -WindowStyle Hidden 让服务脱离当前会话，
# 并循环探活：进程不在就拉起来。
#
# 用法：
#   powershell -File scripts\daemon.ps1            启动守护（本进程探活）
#   powershell -File scripts\daemon.ps1 -Once      只启动一次，不做守护
#   powershell -File scripts\daemon.ps1 -Stop      停止服务 + 守护
#   powershell -File scripts\daemon.ps1 -Status    查看状态
#   powershell -File scripts\daemon.ps1 -Python D:\Py\python.exe
#                                                 指定解释器（默认自动探测）
# =============================================================================
param(
    [switch]$Once,
    [switch]$Stop,
    [switch]$Status,
    [switch]$Detach,
    [string]$Python = "",
    [int]$Port = 8790,
    [int]$IntervalSec = 10,
    # 拉起服务后等它开始监听端口的宽限期（秒）。
    # 必须 > 服务冷启动耗时：数据库不可用时 uvicorn 要在 lifespan 里重试
    # 30×2s = 60s 才绑端口，原来的 8s 死等会把「还在启动」误判成「已经没了」，
    # 于是每轮再拉一个新进程 —— 实测堆到过 5 组 main.py + uvicorn 同时活着。
    [int]$StartGraceSec = 30
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$PidFile = Join-Path $Root "logs\daemon.pid"
$SvcPidFile = Join-Path $Root ".workbuddy2api.pid"
$WatchLog = Join-Path $Root "logs\daemon.log"
$OutLog = Join-Path $Root "logs\service.out.log"
$ErrLog = Join-Path $Root "logs\service.err.log"

# ---- Python 解释器 -----------------------------------------------------------
# 解析顺序：-Python 参数 > WB_PY 环境变量 > 项目 .venv > PATH 里的 python.exe。
# WB_PY 与 Linux 看门狗（scripts/watchdog.sh）同名，换机器不必改脚本正文。
# 原来这里写死 .venv\Scripts\python.exe：本机没有 .venv（依赖装在系统 Python）
# 时守护会直接起不来，而且**没有任何提示** —— 必须能兜底、且失败要说清楚。
function Resolve-Python {
    param([string]$Explicit)
    $tried = @()
    foreach ($c in @($Explicit, $env:WB_PY, (Join-Path $Root ".venv\Scripts\python.exe"))) {
        if ([string]::IsNullOrWhiteSpace($c)) { continue }
        if (Test-Path -LiteralPath $c -PathType Leaf) { return (Resolve-Path -LiteralPath $c).Path }
        $tried += $c
    }
    # 「应用商店占位符」（WindowsApps\python.exe）会弹商店而不是跑解释器，跳过。
    $cmd = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue |
           Where-Object { $_.Source -notlike "*\WindowsApps\*" } |
           Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    $tried += "PATH 里的 python.exe"
    throw "找不到可用的 Python 解释器。已尝试：$($tried -join '；')。请用 -Python <路径> 指定，或设置 WB_PY 环境变量。"
}

# ---- 守护进程探活 -----------------------------------------------------------
# 只看 pid 文件里那个 pid 是否**真的还活着**：文件可能来自上次异常退出，
# 拿一个死 pid 当「守护在运行」会让我们拒绝启动（服务就永远起不来）。
function Get-WatchdogPid {
    if (-not (Test-Path $PidFile)) { return 0 }
    $wpid = 0
    try { $wpid = [int]((Get-Content $PidFile -ErrorAction Stop | Select-Object -First 1).ToString().Trim()) } catch { return 0 }
    if ($wpid -gt 0 -and (Get-Process -Id $wpid -ErrorAction SilentlyContinue)) { return $wpid }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    return 0
}

$PythonError = $null
try { $Python = Resolve-Python -Explicit $Python } catch { $PythonError = $_.Exception.Message; $Python = $null }
# 只有真要拉起服务时才必须解释器可用；-Status / -Stop 在解释器搬家后也要能用。
if (-not $Python -and -not ($Status -or $Stop)) {
    Write-Host "[daemon] $PythonError"
    exit 1
}

function Write-WatchLog([string]$msg) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    try {
        $dir = Split-Path -Parent $WatchLog
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Add-Content -Path $WatchLog -Value $line -Encoding UTF8
    } catch { }
}

function Get-ListenerPid {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
         Select-Object -First 1
    if ($c) { return [int]$c.OwningProcess }
    return 0
}

function Start-ServiceProcess {
    $env:PYTHONIOENCODING = "utf-8"
    $env:PYTHONUTF8 = "1"
    try {
        $dir = Split-Path -Parent $OutLog
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    } catch { }
    # -WindowStyle Hidden + Start-Process：完全脱离当前会话/控制台。
    # 父进程、启动窗口、Agent 会话退出都不会连带回收本服务。
    $p = Start-Process -FilePath $Python -ArgumentList "main.py" `
                       -WorkingDirectory $Root -WindowStyle Hidden `
                       -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog `
                       -PassThru
    Write-WatchLog "已启动服务 PID=$($p.Id)"
    return $p
}

# 等一个刚拉起的服务就绪。返回 ready / dead / timeout。
# 关键点：**timeout 不等于失败** —— 只要进程还活着，它很可能正卡在启动期
# （例如数据库不可用时的 30×2s 重试），这时再拉一个只会去抢端口、堆出僵尸。
# 只有进程真的退出了（dead）才值得马上重试。
function Wait-ServiceReady($Proc, [int]$GraceSec) {
    $deadline = (Get-Date).AddSeconds($GraceSec)
    while ((Get-Date) -lt $deadline) {
        if ((Get-ListenerPid) -gt 0) { return "ready" }
        if ($Proc -and -not (Get-Process -Id $Proc.Id -ErrorAction SilentlyContinue)) { return "dead" }
        Start-Sleep -Milliseconds 500
    }
    if ((Get-ListenerPid) -gt 0) { return "ready" }
    return "timeout"
}

function Stop-Watchdog {
    if (Test-Path $PidFile) {
        try {
            $wpid = Get-Content $PidFile -ErrorAction Stop | Select-Object -First 1
            $wpid = [int]($wpid.ToString().Trim())
            if ($wpid -gt 0 -and $wpid -ne $PID) {
                Stop-Process -Id $wpid -Force -ErrorAction SilentlyContinue
                Write-WatchLog "已停止守护 PID=$wpid"
            }
        } catch { }
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }
}

# ---- -Detach：把自己以隐藏窗口重新拉起，当前进程即可退出 ----------------
# 启动 bat 用这个开关即可避开「路径含空格 + PowerShell 引号」的坑。
if ($Detach) {
    # 已经有守护在跑就别再起一个：两个守护会互相抢着拉起服务，停止时还可能
    # 「停掉一个、另一个又把它拉回来」。这里查一次，守护主循环入口再查一次，
    # 双重保险 —— 控制台菜单每次打开都会调 -Detach，不查就会攒出多个守护。
    $running = Get-WatchdogPid
    if ($running -gt 0) {
        Write-Host "守护已在运行：PID=$running（端口 $Port）"
        exit 0
    }
    # 路径可能含空格（如 "2025College Student..."），必须显式加引号，
    # 否则 -File 会被按空格拆开，子进程立刻失败且无提示。
    $scriptQuoted = '"' + $PSCommandPath + '"'
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $scriptQuoted)
    if ($Once) { $argList += '-Once' }
    if ($Stop) { $argList += '-Stop' }
    if ($Status) { $argList += '-Status' }
    # 解释器路径要一起带过去：子进程会重新解析参数，丢了 -Python 就回退成自动探测。
    if ($Python) { $argList += @('-Python', ('"' + $Python + '"')) }
    $argList += @('-Port', $Port, '-IntervalSec', $IntervalSec)
    Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -WindowStyle Hidden
    exit 0
}

# ---- -Stop --------------------------------------------------------------
if ($Stop) {
    Stop-Watchdog
    $svcPid = Get-ListenerPid
    if ($svcPid -gt 0) {
        Stop-Process -Id $svcPid -Force -ErrorAction SilentlyContinue
        Write-WatchLog "已停止服务 PID=$svcPid"
    } else {
        Write-WatchLog "端口 $Port 上没有正在监听的服务"
    }
    # 再按项目自己的 pid 文件精确收尾：监听进程被杀后 main.py 通常自行退出，
    # 但「无监听窗口期」里它可能还活着，留着就是个连不上的孤儿。
    # 不用 "*main.py*" 模糊匹配全机扫描 —— 那会误杀别的项目里同名的进程。
    if (Test-Path $SvcPidFile) {
        $mainPid = 0
        try { $mainPid = [int]((Get-Content $SvcPidFile -ErrorAction Stop | Select-Object -First 1).ToString().Trim()) } catch { $mainPid = 0 }
        if ($mainPid -gt 0 -and (Get-Process -Id $mainPid -ErrorAction SilentlyContinue)) {
            Stop-Process -Id $mainPid -Force -ErrorAction SilentlyContinue
            Write-WatchLog "已停止 main.py PID=$mainPid"
        }
        Remove-Item $SvcPidFile -Force -ErrorAction SilentlyContinue
    }
    exit 0
}

# ---- -Status ------------------------------------------------------------
if ($Status) {
    $svcPid = Get-ListenerPid
    if ($svcPid -gt 0) {
        Write-Host "服务运行中：PID=$svcPid  端口=$Port"
    } else {
        Write-Host "服务未运行（端口 $Port 空闲）"
    }
    $wpid = Get-WatchdogPid
    if ($wpid -gt 0) {
        Write-Host "守护进程运行中：PID=$wpid（每 ${IntervalSec}s 探活一次）"
    } else {
        Write-Host "守护进程未运行"
    }
    if ($Python) {
        Write-Host "Python 解释器：$Python"
    } else {
        Write-Host "Python 解释器：不可用 —— $PythonError"
    }
    exit 0
}

# ---- -Once --------------------------------------------------------------
if ($Once) {
    if ((Get-ListenerPid) -gt 0) {
        Write-WatchLog "端口 $Port 已被占用，跳过启动"
        exit 0
    }
    $proc = Start-ServiceProcess
    $state = Wait-ServiceReady $proc $StartGraceSec
    $svcPid = Get-ListenerPid
    if ($state -eq "ready") {
        Write-WatchLog "启动成功 PID=$svcPid"
    } elseif ($state -eq "timeout") {
        # 进程还活着但还没监听：这是「启动慢」不是「启动失败」，别误报。
        Write-WatchLog "进程 PID=$($proc.Id) 仍在启动中（${StartGraceSec}s 内未监听端口），稍后请查看 logs\admin.log"
    } else {
        Write-WatchLog "启动失败，请检查 logs\admin.log / logs\service.err.log"
    }
    exit 0
}

# ---- 守护模式 -----------------------------------------------------------
# 再查一次：-Detach 并发或反复点击时别起出两个守护，两个守护会互相抢活，
# 停止时还会「停掉一个、另一个又把它拉回来」。
$running = Get-WatchdogPid
if ($running -gt 0 -and $running -ne $PID) {
    Write-WatchLog "守护已在运行 PID=$running，本次退出（端口 $Port）"
    exit 0
}
$PID | Out-File -FilePath $PidFile -Encoding ASCII -Force
Write-WatchLog "守护启动（每 ${IntervalSec}s 探活一次，端口 $Port，Python=$Python）"

$missCount = 0
while ($true) {
    $svcPid = Get-ListenerPid
    if ($svcPid -eq 0) {
        $missCount++
        Write-WatchLog "检测到服务不在（连续 $missCount 次），正在拉起..."
        $proc = Start-ServiceProcess
        $state = Wait-ServiceReady $proc $StartGraceSec
        switch ($state) {
            "ready" {
                $newPid = Get-ListenerPid
                Write-WatchLog "已恢复 PID=$newPid"
                $missCount = 0
            }
            "dead" {
                # 进程已经退出了，下一轮再拉才安全（不会有残留抢端口）。
                Write-WatchLog "服务进程 PID=$($proc.Id) 启动后退出了，$($IntervalSec)s 后重试（看 logs\admin.log）"
            }
            default {
                # 还活着只是没监听：极可能卡在启动期（数据库不可用时的 30×2s 重试）。
                # 这里**不能再拉起**，否则会抢端口、越堆越多。
                Write-WatchLog "服务进程 PID=$($proc.Id) 在 ${StartGraceSec}s 内未监听端口，继续观察（不重复拉起）"
            }
        }
    } else {
        if ($missCount -ne 0) { $missCount = 0 }
    }
    Start-Sleep -Seconds $IntervalSec
}
