param(
    [string]$Action = "start",
    [switch]$NoBrowser,
    [switch]$NoPause
)

$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Url = "http://127.0.0.1:8765/"
$Port = 8765
$RunsDir = Join-Path $AppDir "runs"
$PidFile = Join-Path $RunsDir "server.pid"
$OutLog = Join-Path $RunsDir "server.out.log"
$ErrLog = Join-Path $RunsDir "server.err.log"

function Fail([string]$Message) {
    Write-Host "[错误] $Message"
    if (-not $NoPause) { try { Read-Host "回车退出" | Out-Null } catch { } }
    exit 1
}

function Test-Health {
    try {
        $r = Invoke-WebRequest -Uri "$Url/api/health" -UseBasicParsing -TimeoutSec 2
        return $r.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Get-Listener {
    return Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

function Open-Site {
    if ($NoBrowser) { return }
    $edgePaths = @()
    foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        if ($base) { $edgePaths += (Join-Path $base "Microsoft\Edge\Application\msedge.exe") }
    }
    $edge = $edgePaths | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($edge) {
        # 新开一个窗口，避免只在自己已经开着的 Edge 里多一个后台标签页（看起来像“没启动”）
        Start-Process -FilePath $edge -ArgumentList @("--new-window", $Url)
        Write-Host "[打开] 已用 Edge 新窗口打开 $Url"
        return
    }
    Start-Process $Url
    Write-Host "[打开] 已交给默认浏览器打开 $Url"
}

function Stop-App {
    $stopped = $false
    if (Test-Path $PidFile) {
        $savedPid = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
        if ($savedPid -and (Get-Process -Id $savedPid -ErrorAction SilentlyContinue)) {
            try {
                Invoke-RestMethod -Uri "$Url/api/session/stop" -Method Post -Body "{}" `
                    -ContentType "application/json" -TimeoutSec 10 | Out-Null
            } catch { }
            Start-Sleep -Milliseconds 800
            taskkill /PID $savedPid /T /F 2>$null | Out-Null
            $stopped = $true
        }
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }
    if (-not $stopped) {
        $conn = Get-Listener
        if ($conn) {
            $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
            if ($proc -and $proc.ProcessName -match "python") {
                taskkill /PID $proc.Id /T /F 2>$null | Out-Null
                $stopped = $true
            } else {
                Write-Host "[提示] 端口 $Port 被 $($proc.ProcessName) 占用，不是本站进程，未结束。"
            }
        }
    }
    if ($stopped) {
        Write-Host "[完成] BOM 选型台已停止。"
    } else {
        Write-Host "[提示] BOM 选型台本来就没有在运行。"
    }
}

function Show-Status {
    if (Test-Health) {
        Write-Host "[运行中] $Url"
        if (Test-Path $PidFile) {
            Write-Host "          PID = $(Get-Content $PidFile | Select-Object -First 1)"
        }
    } else {
        Write-Host "[未运行] $Url"
    }
}

function Find-Python {
    $candidates = @()
    # pythonw 优先：没有控制台子系统，启动后不会留下任何黑框
    foreach ($name in @("pythonw.exe", "python.exe")) {
        $c = Get-Command $name -ErrorAction SilentlyContinue
        if ($c) { $candidates += $c.Source }
    }
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    return $null
}

function Start-App {
    if (Test-Health) {
        Write-Host "[已在运行] $Url"
        Open-Site
        return
    }

    $conn = Get-Listener
    if ($conn) {
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        $name = if ($proc) { $proc.ProcessName } else { "未知进程" }
        Fail "端口 $Port 已被 $name（PID $($conn.OwningProcess)）占用。先运行 停止BOM选型台.bat，或手动结束该进程。"
    }

    $serverPy = Join-Path $AppDir "server.py"
    if (-not (Test-Path $serverPy)) {
        Fail "找不到 $serverPy"
    }

    $python = Find-Python
    if (-not $python) {
        Fail "找不到 python，请先安装 Python 3.10+ 并加入 PATH。"
    }

    New-Item -ItemType Directory -Force -Path $RunsDir | Out-Null
    Remove-Item $OutLog, $ErrLog -Force -ErrorAction SilentlyContinue

    Write-Host "[启动] 正在启动本地服务 ..."
    # 完全无窗口地起服务：
    # 旧写法是 `cmd.exe /c python server.py > log`，cmd 会一直挂着等 python ——
    # 那个黑框就是用户看到的「后台命令窗口」。现在用 pythonw.exe + CREATE_NO_WINDOW，
    # 日志由 server.py 自己写进 runs/server.{out,err}.log，不再需要 cmd 做重定向。
    try {
        # 用 WMI 起进程：它由 WMI 服务创建，不在启动脚本的进程树里 ——
        # 脚本窗口关掉也不会牵连服务（Start-Process 做不到这点）。
        # pythonw.exe 是 GUI 子系统解释器，本来就不建控制台，所以不用 CREATE_NO_WINDOW；
        # 日志由 server.py 自己写进 runs/server.{out,err}.log。
        $res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
            CommandLine      = '"' + $python + '" server.py'
            CurrentDirectory = $AppDir
        }
    } catch {
        Fail "无法创建服务进程：$($_.Exception.Message)"
    }
    if ($res.ReturnValue -ne 0) {
        Fail "无法创建服务进程（Win32 返回码 $($res.ReturnValue)）。"
    }
    $procId = [int]$res.ProcessId
    $procId | Set-Content -Path $PidFile -Encoding ascii
    Write-Host "       PID = $procId（首次启动可能要几秒）"

    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 400
        if (Test-Health) {
            Write-Host "[完成] $Url"
            Write-Host "       关闭：网页里点「退出服务」就行（这个窗口 2 秒后自动关闭）"
            Open-Site
            Start-Sleep -Seconds 2  # 给用户看一眼启动结果，然后连这个控制台一起收掉
            return
        }
        if (-not (Get-Process -Id $procId -ErrorAction SilentlyContinue)) {
            break
        }
    }

    Write-Host "[失败] 服务没有在 60 秒内就绪。"
    if (Test-Path $ErrLog) {
        Write-Host "---- stderr（最后 30 行）----"
        Get-Content $ErrLog -Tail 30 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
    }
    if (Test-Path $OutLog) {
        Write-Host "---- stdout（最后 10 行）----"
        Get-Content $OutLog -Tail 10 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host $_ }
    }
    Fail "服务没有在 60 秒内就绪，日志在 $RunsDir"
}

switch ($Action.ToLower()) {
    "start" { Start-App }
    "stop" { Stop-App }
    "restart" { Stop-App; Start-Sleep -Seconds 1; Start-App }
    "status" { Show-Status }
    default {
        Write-Host "用法：启动BOM选型台.bat [start|stop|restart|status]"
    }
}
