# BOM 采购台 · 环境自检（淘宝打不开时先跑它）
#
# 为什么单独做一份：用户在别人的电脑上点「启动淘宝会话」失败时，
# 我们拿不到那台机器的任何信息，只能靠来回问。这个脚本把「能不能开淘宝」
# 拆成可以逐项验证的几件事——运行时、Edge、组策略、调试端口、网络——
# 每项都给出结论，并把完整报告写进 bom-site\runs\doctor-*.txt，
# 用户把那个文件发过来，就不用猜了。
#
# 只读优先：已经能连上的就不动；需要试验时用独立临时 profile + 9333 端口，
# 不碰用户自己的 .cdp-profile，也不动正在跑的服务。

param(
    [switch]$NoPause
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ProgressPreference = "SilentlyContinue"

$AppDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $AppDir
$RunsDir = Join-Path $AppDir "runs"
$Lines   = New-Object System.Collections.Generic.List[string]
$script:Fails = 0
$script:Warns = 0
$script:Hints = New-Object System.Collections.Generic.List[string]

function Say([string]$Text, [string]$Color = "Gray") {
    Write-Host $Text -ForegroundColor $Color
    $script:Lines.Add($Text)
}
function Head([string]$Text) {
    Say ""
    Say "== $Text ==" "Cyan"
}
function Ok([string]$Text) {
    Say "  [OK]   $Text" "Green"
}
function Warn([string]$Text) {
    $script:Warns++
    Say "  [注意] $Text" "Yellow"
}
function Bad([string]$Text, [string]$Hint = "") {
    $script:Fails++
    Say "  [失败] $Text" "Red"
    if ($Hint) { $script:Hints.Add($Hint) }
}

function Get-VersionText {
    Say ""
    Say "  BOM 采购台 · 环境自检" "Cyan"
    Say "  ------------------------------------------------------------"
}

function Get-PortFromSettings {
    $settings = Join-Path $AppDir "settings.json"
    if (-not (Test-Path $settings)) { $settings = Join-Path $AppDir "settings.example.json" }
    try {
        $obj = Get-Content $settings -Raw -Encoding UTF8 | ConvertFrom-Json
        $m = [regex]::Match([string]$obj.cdp_url, ":(\d{2,5})")
        if ($m.Success) { return [int]$m.Groups[1].Value }
    } catch { }
    return 9222
}

function Test-CdpEndpoint([int]$PortNumber, [int]$TimeoutSec = 3) {
    try {
        $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$PortNumber/json/version")
        $req.Proxy = $null
        $req.Timeout = $TimeoutSec * 1000
        $resp = $req.GetResponse()
        try {
            $reader = New-Object System.IO.StreamReader($resp.GetResponseStream(), [Text.Encoding]::UTF8)
            $text = $reader.ReadToEnd()
            $reader.Close()
            return ($text | ConvertFrom-Json)
        } finally { $resp.Close() }
    } catch { return $null }
}

function Get-EdgeExe {
    $candidates = @()
    foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles, $env:LOCALAPPDATA)) {
        if ($base) { $candidates += (Join-Path $base "Microsoft\Edge\Application\msedge.exe") }
    }
    $c = Get-Command "msedge.exe" -ErrorAction SilentlyContinue
    if ($c) { $candidates += $c.Source }
    return $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}

# ---------------------------------------------------------------- 1. 系统
Get-VersionText
Head "系统"
$os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
if ($os) { Say ("  " + $os.Caption + "  " + $os.Version) }
$arch = $env:PROCESSOR_ARCHITECTURE
if ($arch -eq "AMD64") {
    Ok "64 位 Windows（$arch）"
} else {
    Bad "这不是 64 位 Windows（$arch）；免安装包里的运行时不支持 32 位" `
        "换一台 64 位 Windows，或用源码版自己装 32 位 Python"
}
Say ("  PowerShell " + $PSVersionTable.PSVersion.ToString())

# ---------------------------------------------------------------- 2. 目录
Head "目录与权限"
Say ("  解压位置  " + $RootDir)
$probe = Join-Path $RunsDir ".doctor-write-test"
try {
    New-Item -ItemType Directory -Force -Path $RunsDir | Out-Null
    Set-Content -Path $probe -Value "ok" -Encoding ascii -ErrorAction Stop
    Remove-Item $probe -Force -ErrorAction SilentlyContinue
    Ok "目录可写（服务能保存分析结果和日志）"
} catch {
    Bad "目录不可写：$RunsDir" "把整个文件夹解压到桌面或 D 盘再试；Program Files 下没有写权限"
}

# ---------------------------------------------------------------- 3. 运行时
Head "Python 运行环境"
$py = $null
$pyKind = ""
$runtimeExe = Join-Path $RootDir "runtime\python\python.exe"
$venvExe = Join-Path $RootDir ".venv\Scripts\python.exe"
if (Test-Path $runtimeExe) {
    $py = $runtimeExe
    $pyKind = "包内自带运行时（免安装）"
} elseif (Test-Path $venvExe) {
    & $venvExe -c "import sys" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $py = $venvExe
        $pyKind = ".venv 独立环境"
    } else {
        Bad ".venv 在这台电脑上跑不起来（多半是从别的电脑拷来的）" `
            "删掉 .venv 文件夹，重新双击「安装环境.bat」；或直接用免安装版"
    }
}
if (-not $py) {
    $sys = Get-Command "python.exe" -ErrorAction SilentlyContinue | Where-Object { $_.Source -notmatch "WindowsApps" }
    if ($sys) { $py = $sys.Source; $pyKind = "系统 Python" }
}
if ($py) {
    $ver = (& $py -c "import sys;print(sys.version.split()[0])" 2>$null | Select-Object -First 1)
    Say ("  " + $pyKind + "  " + ([string]$ver).Trim())
    Say ("  " + $py)
    & $py -c "import playwright, openpyxl" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Ok "playwright / openpyxl 可导入"
    } else {
        Bad "依赖不全：$pyKind 缺 playwright 或 openpyxl" "双击「安装环境.bat」补齐依赖"
    }
} else {
    Bad "这台电脑上没找到可用的 Python" "用免安装版（自带运行时），或双击「安装环境.bat」"
}

# ---------------------------------------------------------------- 4. Edge 与策略
Head "浏览器（Edge）"
$edge = Get-EdgeExe
if ($edge) {
    $edgeVer = ""
    try { $edgeVer = (Get-Item $edge).VersionInfo.ProductVersion } catch { }
    Ok ("找到 Edge " + $edgeVer)
    Say ("  " + $edge)
} else {
    Bad "没找到 msedge.exe" "Windows 10/11 自带 Edge；精简系统/服务器版可能没有，装一个 Edge 即可"
}
$policyBlocked = $false
foreach ($key in @(
    "HKLM:\SOFTWARE\Policies\Microsoft\Edge",
    "HKCU:\SOFTWARE\Policies\Microsoft\Edge",
    "HKLM:\SOFTWARE\Policies\Google\Chrome",
    "HKCU:\SOFTWARE\Policies\Google\Chrome"
)) {
    if (-not (Test-Path $key)) { continue }
    try { $v = (Get-ItemProperty $key -ErrorAction Stop).RemoteDebuggingAllowed } catch { continue }
    if ($null -eq $v) { continue }
    if ([int]$v -eq 0) {
        $policyBlocked = $true
        Bad "组策略禁止了远程调试（$key → RemoteDebuggingAllowed=0）" `
            "让管理员放开 RemoteDebuggingAllowed（公司电脑常见）；否则这台机器无法用 CDP 驱动浏览器"
    } else {
        Ok "组策略允许远程调试（$key）"
    }
}

# ---------------------------------------------------------------- 5. 调试端口
Head "调试端口（CDP）"
$cdpPort = Get-PortFromSettings
Say ("  端口        " + $cdpPort + "（来自 settings.json，缺省 9222）")
$live = Test-CdpEndpoint $cdpPort
if ($live) {
    Ok ("已经能连上：" + [string]$live.Browser)
} else {
    Say "  现在没起来（正常：启动会话时会自动拉起浏览器）"
    if ($edge -and -not $policyBlocked) {
        # 用独立临时 profile + 另一个端口试一次：只验证「这台机器允许开调试端口」，
        # 不碰用户的登录态，也不和正在跑的服务抢 9222。
        $testPort = 9333
        $testProfile = Join-Path $env:TEMP ("bom-doctor-" + [guid]::NewGuid().ToString("N").Substring(0, 6))
        Say ("  试验：用临时 profile 在端口 $testPort 启动一次 ...")
        $proc = $null
        try {
            $proc = Start-Process -FilePath $edge -PassThru -ArgumentList @(
                "--remote-debugging-port=$testPort",
                "--user-data-dir=$testProfile",
                "--no-first-run",
                "--no-default-browser-check",
                "--headless=new",
                "about:blank"
            )
            $probeCdp = $null
            $deadline = (Get-Date).AddSeconds(20)
            while ((Get-Date) -lt $deadline) {
                $probeCdp = Test-CdpEndpoint $testPort 1
                if ($probeCdp) { break }
                Start-Sleep -Milliseconds 500
            }
            if ($probeCdp) {
                Ok ("这台机器允许远程调试（试验端口 $testPort 有响应：" + [string]$probeCdp.Browser + "）")
            } else {
                Bad "Edge 能启动，但调试端口没有任何响应" `
                    "多半是安全软件/组策略拦了 --remote-debugging-port；把本目录加入杀毒白名单后重试"
            }
        } catch {
            Bad ("试验启动 Edge 失败：" + $_.Exception.Message) "手动双击「打开调试版Edge.bat」看能不能开出窗口"
        } finally {
            if ($proc) { taskkill /PID $proc.Id /T /F 2>$null | Out-Null }
            Start-Sleep -Milliseconds 300
            Remove-Item $testProfile -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

# ---------------------------------------------------------------- 6. 淘宝能不能打开
Head "网络与淘宝"
try {
    $inet = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" -ErrorAction SilentlyContinue
    if ($inet -and $inet.ProxyEnable -eq 1) {
        Warn ("系统代理已启用：" + [string]$inet.ProxyServer + "（淘宝打不开时先看代理）")
    } else {
        Ok "系统代理未启用"
    }
} catch { }
foreach ($name in @("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")) {
    if (Test-Path "Env:$name") { Warn ("环境变量 $name=" + (Get-Item "Env:$name").Value) }
}

$tcp = New-Object System.Net.Sockets.TcpClient
try {
    $iar = $tcp.BeginConnect("www.taobao.com", 443, $null, $null)
    if ($iar.AsyncWaitHandle.WaitOne(8000) -and $tcp.Connected) {
        $tcp.EndConnect($iar)
        Ok "能连上 www.taobao.com:443"
    } else {
        Bad "连不上 www.taobao.com:443" "这台机器网络到淘宝不通：检查 DNS/防火墙/公司代理，或换网络再试"
    }
} catch {
    Bad ("连接 www.taobao.com 失败：" + $_.Exception.Message) "检查网络、DNS 或代理设置"
} finally {
    $tcp.Close()
}

try {
    $resp = Invoke-WebRequest -Uri "https://www.taobao.com" -UseBasicParsing -TimeoutSec 15
    $kb = [math]::Round(($resp.RawContentLength / 1024), 0)
    if ($resp.StatusCode -eq 200 -and $resp.RawContentLength -gt 20000) {
        Ok ("淘宝首页 HTTP 200（约 $kb KB）")
    } else {
        Warn ("淘宝首页返回 HTTP " + $resp.StatusCode + "（约 $kb KB），可能是验证页/拦截页")
    }
} catch {
    Bad ("打不开 www.taobao.com：" + $_.Exception.Message) "浏览器里能不能打开淘宝？打不开就是网络问题，跟本工具无关"
}

# ---------------------------------------------------------------- 7. 结论
Head "结论"
if ($script:Fails -eq 0) {
    Say "  没有发现阻塞项。点「启动淘宝会话」应该能开出浏览器。" "Green"
    if ($script:Warns -gt 0) {
        Say ("  有 $($script:Warns) 项提醒，见上面的 [注意]。") "Yellow"
    }
} else {
    Say ("  发现 $($script:Fails) 项问题、$($script:Warns) 项提醒，按下面的顺序处理：") "Red"
    $i = 1
    foreach ($h in $script:Hints) {
        Say ("   $i. " + $h)
        $i++
    }
}

$report = Join-Path $RunsDir ("doctor-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".txt")
try {
    $Lines | Set-Content -Path $report -Encoding UTF8
    Say ""
    Say ("  报告已保存  " + $report) "Cyan"
    Say "  还搞不定就把它发给维护者（里面没有登录态和商品数据）。" "DarkGray"
} catch {
    Say ("  报告没保存成功：" + $_.Exception.Message) "Yellow"
}

if (-not $NoPause) {
    Say ""
    try { Read-Host "  按回车退出" | Out-Null } catch { }
}
