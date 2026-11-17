param(
    [string]$Action = "start",
    [switch]$NoBrowser,
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
# Invoke-WebRequest / Invoke-RestMethod 默认会刷一条进度条，CLI 里很难看
$ProgressPreference = "SilentlyContinue"

# PowerShell 5.1 的 Start-Process 拼环境块时用的是**不区分大小写**的字典：
# 系统里同时有 HTTPS_PROXY 和 https_proxy（Clash 之类的代理客户端很常见）时，
# 它会抛「已添加项。字典中的关键字:“HTTPS_PROXY”所添加的关键字:“https_proxy”」，
# 表现成「启动器报错 / 浏览器没打开 / 服务起不来」。
# 这里把大小写重复的小写副本清掉，保留大写的那份（Python、curl、Edge 都认大写）。
foreach ($proxyVar in @("http_proxy", "https_proxy", "all_proxy", "no_proxy")) {
    if (Test-Path "Env:$proxyVar") { Remove-Item "Env:$proxyVar" -ErrorAction SilentlyContinue }
}

$AppDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir  = Split-Path -Parent $AppDir
$Url      = "http://127.0.0.1:8765/"
$Port     = 8765
$CdpPort  = 9222
$CdpUrl   = "http://127.0.0.1:$CdpPort/json/version"
$RunsDir  = Join-Path $AppDir "runs"
$PidFile  = Join-Path $RunsDir "server.pid"
$OutLog   = Join-Path $RunsDir "server.out.log"
$ErrLog   = Join-Path $RunsDir "server.err.log"
$VerFile  = Join-Path $RootDir "VERSION"
$ProfileDir = Join-Path $AppDir ".cdp-profile"

# Get-Content 的参数是 -TotalCount，不是 -First；写 -First 会被当成 -Filter 的缩写，
# 静默返回空值，版本号就永远显示不出来。
$AppName    = "BOM 采购台"
$AppVersion = ""
if (Test-Path $VerFile) {
    $verLine = Get-Content $VerFile -TotalCount 1 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($verLine) { $AppVersion = ([string]$verLine).Trim() }
}

$C_Brand = "Cyan"
$C_Ok    = "Green"
$C_Warn  = "Yellow"
$C_Bad   = "Red"
$C_Dim   = "DarkGray"

# ---------- 输出：按东亚字宽对齐，中文占两列 ----------
function Get-DisplayWidth([string]$s) {
    $w = 0
    foreach ($ch in $s.ToCharArray()) {
        $code = [int]$ch
        if ($code -ge 0x1100 -and (
                ($code -le 0x115F) -or
                ($code -ge 0x2E80 -and $code -le 0xA4CF) -or
                ($code -ge 0xAC00 -and $code -le 0xD7A3) -or
                ($code -ge 0xF900 -and $code -le 0xFAFF) -or
                ($code -ge 0xFE30 -and $code -le 0xFE6F) -or
                ($code -ge 0xFF00 -and $code -le 0xFF60) -or
                ($code -ge 0xFFE0 -and $code -le 0xFFE6))) {
            $w += 2
        } else {
            $w += 1
        }
    }
    return $w
}

function Pad-Display([string]$s, [int]$width) {
    $pad = $width - (Get-DisplayWidth $s)
    if ($pad -lt 0) { $pad = 0 }
    return $s + (" " * $pad)
}

function Write-Banner {
    $line = if ($AppVersion) { "  $AppName   v$AppVersion" } else { "  $AppName" }
    Write-Host ""
    Write-Host $line -ForegroundColor $C_Brand
    Write-Host ("  " + ("-" * 60)) -ForegroundColor $C_Dim
}

function Write-Section([string]$Title) {
    Write-Host ""
    Write-Host ("  " + $Title) -ForegroundColor White
}

function Write-Row([string]$Label, [string]$Value, [string]$Color = "Gray") {
    Write-Host ("    " + (Pad-Display $Label 24)) -NoNewline -ForegroundColor $C_Dim
    Write-Host $Value -ForegroundColor $Color
}

function Write-Footer([string]$Text, [string]$Color = "Gray") {
    Write-Host ("  " + $Text) -ForegroundColor $Color
}

function Fail([string]$Message, [string]$Hint = "") {
    Write-Host ""
    Write-Host ("  [失败] " + $Message) -ForegroundColor $C_Bad
    if ($Hint) { Write-Host ("         " + $Hint) -ForegroundColor $C_Dim }
    if (-not $NoPause) { try { Read-Host "  按回车退出" | Out-Null } catch { } }
    exit 1
}

# ---------- 探测 ----------
function Test-Health {
    try {
        # 必须显式绕开系统代理：代理开着时 127.0.0.1 也会被丢给代理，
        # 轻则 502、重则弹代理认证框（非交互下直接炸），
        # 结果服务明明活着，启动器却报「健康检查失败」。
        $req = [System.Net.HttpWebRequest]::Create("${Url}api/health")
        $req.Proxy = $null
        $req.Timeout = 2000
        $req.ReadWriteTimeout = 2000
        $resp = $req.GetResponse()
        try { return ([int]$resp.StatusCode -eq 200) } finally { $resp.Close() }
    } catch {
        return $false
    }
}

function Get-PortListener([int]$PortNumber) {
    try {
        return Get-NetTCPConnection -LocalPort $PortNumber -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
    } catch {
        return $null   # 精简版系统 / CI runner 上没有 NetTCPConnection 也不致命
    }
}

function Get-Listener {
    return Get-PortListener $Port
}

function Resolve-Python {
    # python.exe 用来跑 -c 检查（有控制台，能拿到 stdout）；
    # pythonw.exe 用来起服务（GUI 子系统，不留黑框）。
    #
    # 机器上可能装着多个 Python，PATH 上第一个不一定装了依赖。踩过的坑：
    # 从 Git Bash 里调启动器时，PATH 上 3.13 排在前面、它没装 openpyxl，
    # 服务照常起来，但一上传 xlsx 就 500「No module named 'openpyxl'」。
    # 所以把所有能想到的解释器收集起来，优先选依赖齐全的那个。
    $cands = @()
    $seen = @{}
    foreach ($name in @("python.exe", "pythonw.exe")) {
        $c = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $c) { continue }
        $dir = Split-Path $c.Source
        $exe = Join-Path $dir "python.exe"
        $win = Join-Path $dir "pythonw.exe"
        if ((Test-Path $exe) -and -not $seen.ContainsKey($exe)) {
            $seen[$exe] = $true
            $cands += @{ Exe = $exe; Window = $(if (Test-Path $win) { $win } else { $exe }) }
        }
    }
    # py 启动器兜底：某些安装（含 Store 版）PATH 里根本没有 python.exe
    $pyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        try {
            $real = (& $pyLauncher.Source -3 -c "import sys;print(sys.executable)" 2>$null | Select-Object -First 1)
            if ($real) { $real = $real.Trim() }
            if ($real -and (Test-Path $real) -and -not $seen.ContainsKey($real)) {
                $seen[$real] = $true
                $win = Join-Path (Split-Path $real) "pythonw.exe"
                $cands += @{ Exe = $real; Window = $(if (Test-Path $win) { $win } else { $real }) }
            }
        } catch { }
    }
    if (-not $cands.Count) { return @{ Exe = $null; Window = $null } }
    if ($cands.Count -eq 1) { return $cands[0] }
    foreach ($cand in $cands) {
        if (Test-PythonDeps $cand.Exe) { return $cand }
    }
    return $cands[0]
}

function Get-PythonVersion([string]$exe) {
    try {
        $v = & $exe -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v) { return ($v | Select-Object -First 1).Trim() }
    } catch { }
    return $null
}

function Test-PythonDeps([string]$exe) {
    try {
        & $exe -c "import playwright,openpyxl" 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Resolve-Edge {
    $candidates = @()
    foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, $env:LOCALAPPDATA)) {
        if ($base) { $candidates += (Join-Path $base "Microsoft\Edge\Application\msedge.exe") }
    }
    $c = Get-Command "msedge.exe" -ErrorAction SilentlyContinue
    if ($c) { $candidates += $c.Source }
    return $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}

function Get-DebugEdgeProcesses {
    # 只认带 --remote-debugging-port=9222 的 Edge，绝不误伤用户自己的普通浏览器
    try {
        return @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -match "remote-debugging-port=$CdpPort" })
    } catch {
        return @()   # 无 CIM / 无权限时按「没在跑」处理，不影响体检
    }
}

function Invoke-Loopback {
    # 回环地址上的请求一律绕开系统代理：代理开着时 127.0.0.1 也会被丢给代理
    # （轻则 502，重则弹认证框），服务明明活着却判定「没起来 / 没就绪」。
    param([string]$Uri, [string]$Method = "GET", [string]$Body = $null, [int]$TimeoutSec = 3)
    $req = [System.Net.HttpWebRequest]::Create($Uri)
    $req.Proxy = $null
    $req.Method = $Method
    $req.Timeout = $TimeoutSec * 1000
    $req.ReadWriteTimeout = $TimeoutSec * 1000
    if ($null -ne $Body) {
        $bytes = [Text.Encoding]::UTF8.GetBytes($Body)
        $req.ContentType = "application/json"
        $req.ContentLength = $bytes.Length
        $stream = $req.GetRequestStream()
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Close()
    }
    if ($Method -eq "POST" -and $null -eq $Body) {
        $req.ContentLength = 0
    }
    $resp = $req.GetResponse()
    try {
        $reader = New-Object System.IO.StreamReader($resp.GetResponseStream(), [Text.Encoding]::UTF8)
        $text = $reader.ReadToEnd()
        $reader.Close()
        if ([string]::IsNullOrWhiteSpace($text)) { return $null }
        return ($text | ConvertFrom-Json)
    } finally {
        $resp.Close()
    }
}

function Test-Cdp {
    try {
        $v = Invoke-Loopback -Uri $CdpUrl -TimeoutSec 2
        if ($v -and $v.Browser) { return $v }
    } catch { }
    return $null
}

function Open-Site {
    if ($NoBrowser) { return "已跳过（-NoBrowser）" }
    $edge = Resolve-Edge
    if ($edge) {
        Start-Process -FilePath $edge -ArgumentList @("--new-window", $Url)
        return "已用 Edge 新窗口打开"
    }
    Start-Process $Url
    return "已交给默认浏览器打开"
}

# ---------- stop ----------
function Stop-App {
    Write-Banner
    Write-Section "停止服务"

    $rows = @()
    $savedPid = $null
    if (Test-Path $PidFile) {
        $savedPid = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    }

    if ($savedPid -and (Get-Process -Id $savedPid -ErrorAction SilentlyContinue)) {
        try {
            Invoke-Loopback -Uri "${Url}api/session/stop" -Method POST -Body "{}" -TimeoutSec 10 | Out-Null
        } catch { }
        Start-Sleep -Milliseconds 800
        taskkill /PID $savedPid /T /F 2>$null | Out-Null
        $rows += ,@("服务进程", "已停止（PID $savedPid）", $C_Ok)
    } else {
        $conn = Get-Listener
        if ($conn) {
            $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            if ($proc -and $proc.ProcessName -match "python") {
                taskkill /PID $proc.Id /T /F 2>$null | Out-Null
                $rows += ,@("服务进程", "已停止（PID $($proc.Id)）", $C_Ok)
            } else {
                $name = if ($proc) { $proc.ProcessName } else { "未知进程" }
                $rows += ,@("服务进程", "端口被 $name 占用，未结束", $C_Warn)
            }
        } else {
            $rows += ,@("服务进程", "本来就没在运行", $C_Dim)
        }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue

    # 调试版 Edge（只认带 remote-debugging-port 的，普通 Edge 不动）
    $debugEdge = Get-DebugEdgeProcesses
    if ($debugEdge.Count -gt 0) {
        foreach ($p in $debugEdge) { taskkill /PID $p.ProcessId /T /F 2>$null | Out-Null }
        $rows += ,@("调试浏览器", "已关闭（$($debugEdge.Count) 个进程）", $C_Ok)
    } else {
        $rows += ,@("调试浏览器", "未运行", $C_Dim)
    }

    foreach ($r in $rows) { Write-Row $r[0] $r[1] $r[2] }
    Write-Host ""
    Write-Host ("  " + ("-" * 60)) -ForegroundColor $C_Dim
    Write-Footer "完成。本窗口 2 秒后自动关闭。" $C_Ok
    Start-Sleep -Seconds 2
}

# ---------- status ----------
function Show-Status {
    Write-Banner
    Write-Section "运行状态"
    if (Test-Health) {
        Write-Row "服务" "运行中" $C_Ok
        Write-Row "地址" $Url
        if (Test-Path $PidFile) {
            Write-Row "进程" "PID $(Get-Content $PidFile | Select-Object -First 1)"
        }
        try {
            $s = Invoke-Loopback -Uri "${Url}api/session/status" -TimeoutSec 3
            $state = if ($s.ready) { "就绪" } elseif ($s.running) { "启动中" } else { "未启动" }
            Write-Row "淘宝会话" $state
        } catch { }
    } else {
        Write-Row "服务" "未运行" $C_Dim
    }
    Write-Host ""
}

# ---------- start ----------
function Start-App {
    Write-Banner

    if (Test-Health) {
        Write-Section "已在运行"
        Write-Row "地址" $Url $C_Ok
        Write-Row "浏览器" (Open-Site)
        Write-Host ""
        Write-Footer "不用重复启动，直接使用即可。" $C_Dim
        Start-Sleep -Seconds 2
        return
    }

    $serverPy = Join-Path $AppDir "server.py"
    if (-not (Test-Path $serverPy)) { Fail "找不到服务程序" $serverPy }

    # --- 环境自检 ---
    Write-Section "环境"

    $py = Resolve-Python
    if (-not $py.Window) {
        Fail "没有找到 Python" "请安装 Python 3.10 或更高版本，并勾选 Add to PATH。下载：https://www.python.org/downloads/"
    }
    $pyVer = if ($py.Exe) { Get-PythonVersion $py.Exe } else { $null }
    if ($pyVer) {
        $majorMinor = [double]($pyVer -replace '^(\d+\.\d+).*', '$1')
        if ($majorMinor -lt 3.10) {
            Fail "Python 版本过低（$pyVer）" "需要 3.10 或更高版本。"
        }
        Write-Row "Python" $pyVer $C_Ok
    } else {
        Write-Row "Python" "已找到，版本未知" $C_Warn
    }

    $depsOk = if ($py.Exe) { Test-PythonDeps $py.Exe } else { $false }
    if ($depsOk) {
        Write-Row "依赖库" "playwright / openpyxl 已安装" $C_Ok
    } else {
        # 缺依赖还硬起，用户只会看到「上传 xlsx 报服务端异常」，比直接拦住更难查
        Write-Row "依赖库" "缺少依赖，服务无法正常启动" $C_Warn
        Fail "Python 缺依赖（playwright / openpyxl）" `
            "在这个 Python 下执行：`"$($py.Exe)`" -m pip install -r requirements.txt"
    }

    $conn = Get-Listener
    if ($conn) {
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        $name = if ($proc) { $proc.ProcessName } else { "未知进程" }
        Fail "端口 $Port 被占用（$name，PID $($conn.OwningProcess)）" "先跑 启动BOM选型台.bat stop，或结束该进程后重试。"
    }
    Write-Row "端口 $Port" "空闲" $C_Ok

    # --- 启动 ---
    Write-Section "启动"
    New-Item -ItemType Directory -Force -Path $RunsDir | Out-Null
    Remove-Item $OutLog, $ErrLog -Force -ErrorAction SilentlyContinue

    # 用 WMI 创建进程：不在本脚本的进程树里，关窗口不会牵连服务。
    # pythonw.exe 本身不建控制台，所以不需要 CREATE_NO_WINDOW，也没有残留黑框。
    $startedAt = Get-Date
    try {
        $res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
            CommandLine      = '"' + $py.Window + '" server.py'
            CurrentDirectory = $AppDir
        }
    } catch {
        Fail "无法创建服务进程" $_.Exception.Message
    }
    if ($res.ReturnValue -ne 0) {
        Fail "无法创建服务进程" "Win32 返回码 $($res.ReturnValue)"
    }
    $procId = [int]$res.ProcessId
    $procId | Set-Content -Path $PidFile -Encoding ascii
    Write-Row "服务进程" "PID $procId"

    $deadline = (Get-Date).AddSeconds(60)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 350
        if (Test-Health) { $ready = $true; break }
        if (-not (Get-Process -Id $procId -ErrorAction SilentlyContinue)) { break }
    }

    if (-not $ready) {
        Write-Row "健康检查" "60 秒内没有就绪" $C_Bad
        $reason = "服务启动失败"
        if (Test-Path $ErrLog) {
            $tail = Get-Content $ErrLog -Tail 40 -ErrorAction SilentlyContinue
            $hit = $tail | Where-Object { $_ -match "Error|Traceback|Exception" } | Select-Object -Last 1
            if ($hit) { $reason = $hit.Trim() }
        }
        Write-Host ""
        Write-Host ("  [失败] " + $reason) -ForegroundColor $C_Bad
        Write-Footer "        日志：$ErrLog" $C_Dim
        if (-not $NoPause) { try { Read-Host "  按回车退出" | Out-Null } catch { } }
        exit 1
    }

    $elapsed = [int]((Get-Date) - $startedAt).TotalMilliseconds
    Write-Row "健康检查" "200 OK（${elapsed} ms）" $C_Ok
    Write-Row "浏览器" (Open-Site) $C_Ok

    Write-Host ""
    Write-Host ("  " + ("-" * 60)) -ForegroundColor $C_Dim
    Write-Footer "就绪   $Url" $C_Ok
    Write-Footer "停止   页面右上角「退出服务」，或 启动BOM选型台.bat stop" $C_Dim
    Write-Footer "本窗口 3 秒后自动关闭" $C_Dim
    Start-Sleep -Seconds 3
}

# ---------- edge：打开调试版 Edge（独立 profile + CDP 9222）----------
function Start-DebugEdge {
    Write-Banner
    Write-Section "调试浏览器"

    $cdp = Test-Cdp
    if ($cdp) {
        Write-Row "CDP $CdpPort" "已经就绪，不用重复启动" $C_Ok
        Write-Row "浏览器" ([string]$cdp.Browser)
        Write-Footer "下一步：双击 启动BOM选型台.bat" $C_Dim
        Write-Host ""
        Start-Sleep -Seconds 2
        return
    }

    $conn = Get-PortListener $CdpPort
    if ($conn) {
        $p = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        $name = if ($p) { $p.ProcessName } else { "未知进程" }
        Fail "端口 $CdpPort 被 $name（PID $($conn.OwningProcess)）占用，但不是可用 CDP" `
            "通常是普通 Edge 抢先占了端口；关掉所有 Edge 窗口后重试。"
    }

    $edge = Resolve-Edge
    if (-not $edge) {
        Fail "没有找到 msedge.exe" "请安装 Microsoft Edge，或把它加入 PATH。"
    }
    Write-Row "Edge" $edge
    Write-Row "独立 profile" $ProfileDir
    if (-not (Test-Path $ProfileDir)) {
        New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null
    }

    # 单字符串传参，路径里的空格/中文由内层双引号兜住
    $argLine = '--remote-debugging-port={0} --user-data-dir="{1}" --no-first-run --no-default-browser-check --window-position=-2000,0 --window-size=1500,1000 --restore-last-session https://www.taobao.com' -f $CdpPort, $ProfileDir
    try {
        Start-Process -FilePath $edge -ArgumentList $argLine | Out-Null
    } catch {
        Fail "无法启动 Edge" $_.Exception.Message
    }
    Write-Row "启动" "已发出，等待 CDP 就绪" $C_Dim

    $cdp = $null
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        $cdp = Test-Cdp
        if ($cdp) { break }
    }
    if (-not $cdp) {
        Fail "Edge 已启动，但 $CdpPort 没有给出 CDP 响应" `
            "确认没有别的 Edge 实例占用该 profile；详细原因见淘宝会话状态。"
    }
    Write-Row "CDP $CdpPort" "就绪" $C_Ok
    Write-Row "浏览器" ([string]$cdp.Browser) $C_Ok

    Write-Host ""
    Write-Host ("  " + ("-" * 60)) -ForegroundColor $C_Dim
    Write-Footer "首次使用：在浏览器里手动登录淘宝，登录态会留在这个独立 profile 里。" $C_Warn
    Write-Footer "窗口默认停在屏幕外，不抢前台；网页里可点「显示淘宝窗口」。" $C_Dim
    Write-Footer "下一步：双击 启动BOM选型台.bat" $C_Ok
    Write-Host ""
    Write-Footer "本窗口 4 秒后自动关闭。" $C_Dim
    Start-Sleep -Seconds 4
}

# ---------- diag：只读体检，不启动也不关闭任何进程 ----------
function Show-Diagnose {
    Write-Banner
    Write-Section "环境"
    Write-Row "版本" $(if ($AppVersion) { "v$AppVersion" } else { "未找到 VERSION 文件" }) `
        $(if ($AppVersion) { $C_Ok } else { $C_Warn })
    $py = Resolve-Python
    if ($py.Window) {
        $pyVer = if ($py.Exe) { Get-PythonVersion $py.Exe } else { $null }
        Write-Row "Python" $(if ($pyVer) { $pyVer } else { $py.Window }) $C_Ok
        $depsOk = if ($py.Exe) { Test-PythonDeps $py.Exe } else { $false }
        Write-Row "依赖库" $(if ($depsOk) { "playwright / openpyxl 已安装" } else { "缺少依赖，需 pip install -r requirements.txt" }) `
            $(if ($depsOk) { $C_Ok } else { $C_Warn })
    } else {
        Write-Row "Python" "未找到，请先安装 Python 3.10+" $C_Bad
    }
    $edge = Resolve-Edge
    Write-Row "Edge" $(if ($edge) { $edge } else { "未找到" }) $(if ($edge) { $C_Ok } else { $C_Warn })

    Write-Section "服务"
    if (Test-Health) {
        Write-Row "健康检查" "200 OK" $C_Ok
        Write-Row "地址" $Url
        if (Test-Path $PidFile) {
            Write-Row "服务 PID" (Get-Content $PidFile -TotalCount 1 | Select-Object -First 1)
        }
    } else {
        Write-Row "健康检查" "未运行" $C_Dim
        $svcConn = Get-Listener
        if ($svcConn) {
            $p = Get-Process -Id $svcConn.OwningProcess -ErrorAction SilentlyContinue
            $name = if ($p) { $p.ProcessName } else { "未知进程" }
            Write-Row "端口 $Port" "被 $name（PID $($svcConn.OwningProcess)）占用" $C_Bad
        } else {
            Write-Row "端口 $Port" "空闲" $C_Ok
        }
    }

    Write-Section "调试浏览器"
    $cdp = Test-Cdp
    if ($cdp) {
        Write-Row "CDP $CdpPort" "就绪" $C_Ok
        Write-Row "浏览器" ([string]$cdp.Browser)
    } else {
        $cdpConn = Get-PortListener $CdpPort
        Write-Row "CDP $CdpPort" $(if ($cdpConn) { "端口被占用，但不是可用 CDP" } else { "未运行" }) `
            $(if ($cdpConn) { $C_Warn } else { $C_Dim })
    }
    $dbgCount = (Get-DebugEdgeProcesses).Count
    Write-Row "调试 Edge 进程" "$dbgCount 个"
    Write-Row "独立 profile" $(if (Test-Path $ProfileDir) { "已存在" } else { "尚未创建（首次启动时自动建）" })

    Write-Section "日志"
    if (Test-Path $ErrLog) {
        $f = Get-Item $ErrLog
        Write-Row "错误日志" ("{0}  {1} 字节" -f $f.LastWriteTime.ToString("MM-dd HH:mm:ss"), $f.Length)
    } else {
        Write-Row "错误日志" "无" $C_Ok
    }
    Write-Host ""
    Write-Host ("  " + ("-" * 60)) -ForegroundColor $C_Dim
    Write-Footer "只读体检，不会启动或关闭任何进程。" $C_Dim
    Write-Host ""
}

function Show-Usage {
    Write-Banner
    Write-Section "用法"
    Write-Footer "  打开调试版Edge.bat            开调试 Edge（独立 profile / 9222）"
    Write-Footer "  启动BOM选型台.bat            自检环境 → 后台起服务 → 打开界面"
    Write-Footer "  启动BOM选型台.bat status     只看运行状态"
    Write-Footer "  启动BOM选型台.bat diag       只读体检（环境 / 端口 / 日志）"
    Write-Footer "  启动BOM选型台.bat restart   重启服务"
    Write-Footer "  启动BOM选型台.bat stop       停服务 + 关调试 Edge"
    Write-Host ""
}

switch ($Action.ToLower()) {
    "start"             { Start-App }
    "stop"              { Stop-App }
    "restart"           { Stop-App | Out-Null; Start-Sleep -Seconds 1; Start-App }
    "status"            { Show-Status }
    "edge"              { Start-DebugEdge }
    "diag"              { Show-Diagnose }
    "diagnose"          { Show-Diagnose }
    "help"              { Show-Usage }
    default             { Show-Usage }
}
