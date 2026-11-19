# 下载免安装运行时所需的原料：Python 嵌入式包 + 依赖 wheel。
# 只在打便携包时跑一次，产物缓存在 .cache\ 下（已被 .gitignore 忽略）。
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root = Split-Path -Parent $PSScriptRoot
$cache = Join-Path $root ".cache"
$pyDir = Join-Path $cache "pyembed"
$whDir = Join-Path $cache "wheelhouse"
New-Item -ItemType Directory -Force -Path $pyDir, $whDir | Out-Null

$ver = "3.14.4"
$zip = Join-Path $pyDir "python-$ver-embed-amd64.zip"
if (-not (Test-Path $zip)) {
    $url = "https://www.python.org/ftp/python/$ver/python-$ver-embed-amd64.zip"
    Write-Host "下载 $url"
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing -TimeoutSec 180
}
Write-Host ("Python 嵌入式包 {0:N1} MB" -f ((Get-Item $zip).Length / 1MB))

Write-Host "下载 wheel ..."
& python -m pip download -r (Join-Path $root "requirements.txt") -d $whDir --only-binary=:all: --disable-pip-version-check -q
if ($LASTEXITCODE -ne 0) { throw "pip download 失败" }
Get-ChildItem $whDir -Filter *.whl | ForEach-Object { "  {0}  {1:N1} MB" -f $_.Name, ($_.Length / 1MB) }
