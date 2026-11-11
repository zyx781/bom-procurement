@echo off
chcp 936 >nul
set EDGE="C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if not exist %EDGE% set EDGE="C:\Program Files\Microsoft\Edge\Application\msedge.exe"
if not exist %EDGE% (
  echo [错误] 没找到 msedge.exe，请确认 Edge 安装位置
  pause
  exit /b 1
)
echo 正在启动带调试端口的 Edge（profile: D:\测试\bom-site\.cdp-profile）...
echo 这个窗口里登录一次淘宝，之后都复用它；不要关它。
start "" %EDGE% --remote-debugging-port=9222 --user-data-dir="D:\测试\bom-site\.cdp-profile" --no-first-run --no-default-browser-check --window-position=-2000,0 --window-size=1500,1000 --restore-last-session https://www.taobao.com
timeout /t 3 >nul
powershell -NoProfile -Command "try { $v = Invoke-RestMethod http://127.0.0.1:9222/json/version -TimeoutSec 3; Write-Host ('[OK] CDP 已就绪: ' + $v.Browser) } catch { Write-Host '[失败] 9222 没起来，可能 Edge 已被别的实例占用（把普通 Edge 窗口全关掉再试）' }"
pause
