# =============================================================================
# workbuddy2api —— 确保 MySQL 可用（供 start-service.bat / start-console.bat 调用）
# -----------------------------------------------------------------------------
# 为什么需要这个脚本：
#   ADMIN_DATABASE_URL 指向本机 MySQL，但这台机器上 MySQL **没有注册成 Windows
#   服务**（只有一次性的 mysqld 进程），关机或被结束后不会自启。此时 `python
#   main.py` 看起来也能起来，但 uvicorn 的 lifespan 在**绑定端口之前**要重试数据库
#   30 次 × 2s = 60s，这 60 秒里 8790 根本不监听 ——
#     * bat 里 11s 的端口检测会误报 "Start FAILED"；
#     * 守护脚本每 8s 探活一次，会判定"服务不在"并再拉一个进程，越堆越多。
#   所以启动服务之前先把 MySQL 拉起来，把这段 60s 等待直接消掉。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\ensure-mysql.ps1
#   powershell ... -File scripts\ensure-mysql.ps1 -TimeoutSec 90 -Quiet
#   powershell ... -File scripts\ensure-mysql.ps1 -NoStart      # 只检查，不启动
#
# 可通过环境变量覆盖探测结果：
#   WB_MYSQL_EXE = mysqld.exe 的完整路径
#   WB_MYSQL_INI = my.ini 的完整路径
#
# 退出码：0 = MySQL 可用（本来就在跑，或本次拉起来了）；1 = 不可用
# =============================================================================
param(
    [int]$TimeoutSec = 60,
    [switch]$Quiet,
    [switch]$NoStart
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root ".env"

function Say([string]$msg) {
    if (-not $Quiet) { Write-Host $msg }
}

# ---- 从 .env 读数据库目标 ---------------------------------------------------
# 不写死 3306：端口、库名都可能被改过；也不是所有部署都用本机 MySQL
# （sqlite / 远端库时本机没有任何东西可启动，必须直接放行）。
function Get-DbTarget {
    $default = @{ Scheme = "mysql"; Host = "127.0.0.1"; Port = 3306; Name = "workbuddy_admin" }
    if (-not (Test-Path -LiteralPath $EnvFile)) { return $default }
    $line = Get-Content -LiteralPath $EnvFile -ErrorAction SilentlyContinue |
            Where-Object { $_ -match '^\s*ADMIN_DATABASE_URL\s*=' } |
            Select-Object -First 1
    if (-not $line) { return $default }
    $url = ($line -split '=', 2)[1].Trim().Trim('"').Trim("'")
    if ($url -notmatch '^(?<scheme>[a-z0-9+]+)://(?:[^@/]*@)?(?<host>[^:/?]+):(?<port>\d+)/(?<name>[^?]+)') {
        return $default
    }
    return @{
        Scheme = ($Matches.scheme -split '\+')[0]
        Host   = $Matches.host
        Port   = [int]$Matches.port
        Name   = $Matches.name
    }
}

function Test-PortListening([int]$p) {
    $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue |
         Select-Object -First 1
    return [bool]$c
}

# ---- 找 mysqld.exe ---------------------------------------------------------
# 解析顺序：WB_MYSQL_EXE > C:\Program Files\MySQL\MySQL Server *\bin\mysqld.exe
# 多版本并存时取版本号最大的那个。
function Resolve-MySqlExe {
    $tried = @()
    if ($env:WB_MYSQL_EXE) {
        if (Test-Path -LiteralPath $env:WB_MYSQL_EXE -PathType Leaf) {
            return (Get-Item -LiteralPath $env:WB_MYSQL_EXE).FullName
        }
        $tried += $env:WB_MYSQL_EXE
    }
    $found = @()
    foreach ($base in @("C:\Program Files\MySQL", "C:\Program Files (x86)\MySQL")) {
        if (-not (Test-Path -LiteralPath $base)) { continue }
        Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue | ForEach-Object {
            $exe = Join-Path $_.FullName "bin\mysqld.exe"
            if (Test-Path -LiteralPath $exe -PathType Leaf) {
                $ver = [version]"0.0"
                try { $ver = [version]($_.Name -replace '^MySQL Server\s*', '') } catch { }
                $found += [pscustomobject]@{ Exe = $exe; Ver = $ver }
            }
        }
    }
    $best = $found | Sort-Object -Property Ver -Descending | Select-Object -First 1
    if ($best) { return $best.Exe }
    $tried += "C:\Program Files\MySQL\MySQL Server *\bin\mysqld.exe"
    throw "找不到 mysqld.exe。已尝试：$($tried -join '；')。请用 WB_MYSQL_EXE 指定路径。"
}

# ---- 找 my.ini -------------------------------------------------------------
# Windows 版 mysqld 的默认搜索列表里**没有** C:\ProgramData\MySQL\...，
# 不显式传 --defaults-file 它会用编译期默认值去起一个空 datadir（起不来或建错库）。
function Resolve-MyIni([string]$Exe) {
    if ($env:WB_MYSQL_INI) {
        if (Test-Path -LiteralPath $env:WB_MYSQL_INI -PathType Leaf) {
            return (Get-Item -LiteralPath $env:WB_MYSQL_INI).FullName
        }
    }
    $base = Join-Path $env:ProgramData "MySQL"
    if (Test-Path -LiteralPath $base) {
        $hits = Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue |
                ForEach-Object { Join-Path $_.FullName "my.ini" } |
                Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }
        if ($hits) { return ($hits | Select-Object -First 1) }
    }
    # 兜底：mysql 安装目录根下的 my.ini
    $sibling = Join-Path (Split-Path -Parent (Split-Path -Parent $Exe)) "my.ini"
    if (Test-Path -LiteralPath $sibling -PathType Leaf) { return $sibling }
    return $null
}

function Get-MySqlErrTail([int]$Lines = 15) {
    $base = Join-Path $env:ProgramData "MySQL"
    if (-not (Test-Path -LiteralPath $base)) { return @("(MySQL 错误日志目录不存在)") }
    $err = Get-ChildItem -LiteralPath $base -Recurse -Filter "*.err" -File -ErrorAction SilentlyContinue |
           ForEach-Object { $_ } |
           Sort-Object LastWriteTime -Descending |
           Select-Object -First 1
    if (-not $err) { return @("(找不到 MySQL 错误日志 *.err)") }
    return @(Get-Content -LiteralPath $err.FullName -Tail $Lines -ErrorAction SilentlyContinue)
}

# =============================================================================
# 主流程
# =============================================================================
$target = Get-DbTarget

if ($target.Scheme -ne "mysql") {
    Say "[mysql] 数据库不是 MySQL（$($target.Scheme)），无需启动，跳过。"
    exit 0
}
if ($target.Host -notin @("127.0.0.1", "localhost", "::1")) {
    Say "[mysql] 数据库在远端（$($target.Host):$($target.Port)），本机无需启动。"
    exit 0
}

if (Test-PortListening $target.Port) {
    Say "[mysql] 已在运行：$($target.Host):$($target.Port)（库 $($target.Name)）"
    exit 0
}

if ($NoStart) {
    Write-Host "[mysql] 未运行：$($target.Host):$($target.Port)（按 -NoStart 只检查，不启动）"
    exit 1
}

$exe = $null
try { $exe = Resolve-MySqlExe } catch {
    Write-Host "[mysql] $($_.Exception.Message)"
    exit 1
}
$ini = Resolve-MyIni $exe

Say "[mysql] 未运行，正在启动：$exe"
if ($ini) { Say "[mysql] 配置文件：$ini" }
else      { Say "[mysql] 警告：没找到 my.ini，将按 mysqld 默认参数启动。" }

# 路径带空格时必须自己补引号：Start-Process 只是把 -ArgumentList 用空格拼接，
# 不会替我们加引号 —— `--defaults-file=C:\ProgramData\MySQL\MySQL Server 8.4\my.ini`
# 会被拆成两个参数，mysqld 立刻失败。
$argList = @()
if ($ini) { $argList += ('--defaults-file="{0}"' -f $ini) }

try {
    $proc = Start-Process -FilePath $exe -ArgumentList $argList `
                          -WindowStyle Hidden -PassThru -ErrorAction Stop
} catch {
    Write-Host "[mysql] 启动失败：$($_.Exception.Message)"
    exit 1
}
Say "[mysql] 已拉起 mysqld PID=$($proc.Id)，等待端口 $($target.Port) 就绪（最多 ${TimeoutSec}s）…"

$started = Get-Date
$deadline = $started.AddSeconds($TimeoutSec)
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 500
    if (Test-PortListening $target.Port) {
        $elapsed = [int]((Get-Date) - $started).TotalSeconds
        Say "[mysql] 就绪（约 ${elapsed}s）。"
        exit 0
    }
    # 进程提前退出时别再白等：把 mysqld 的错误日志末尾打出来，省得靠猜。
    if (-not (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) {
        Write-Host "[mysql] mysqld 已退出，启动失败。错误日志末尾："
        foreach ($l in (Get-MySqlErrTail)) { Write-Host ("    " + $l) }
        exit 1
    }
}

Write-Host "[mysql] 等待 $TimeoutSec s 后端口 $($target.Port) 仍未监听，启动失败。错误日志末尾："
foreach ($l in (Get-MySqlErrTail)) { Write-Host ("    " + $l) }
exit 1
