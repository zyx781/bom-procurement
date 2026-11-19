@echo off
rem BOM 采购台 · 一次性安装运行环境（建 .venv + 装依赖）。UTF-8，双击运行即可。
chcp 65001 >nul
title BOM 采购台 · 安装环境
mode con cols=86 lines=36 >nul 2>&1
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "VENV=%~dp0.venv"
set "VPY=%VENV%\Scripts\python.exe"
set "VWIN=%VENV%\Scripts\pythonw.exe"
set "MIRROR=https://pypi.tuna.tsinghua.edu.cn/simple"

echo.
echo   BOM 采购台 · 安装运行环境
echo   ------------------------------------------------------------------
echo   这一步只做一次：在本目录建独立环境 .venv，装好 playwright / openpyxl。
echo   装完之后双击「启动BOM选型台.bat」即可，日常使用不再需要联网。
echo.

if exist "%VPY%" (
    echo   [1-2/4] 检测到已有 .venv，跳过创建。
    goto :deps
)

rem ---------------- 找 Python ----------------
echo   [1/4] 查找 Python 3.10+ ...
set "PY="

rem 优先 py 启动器（官方安装包会带），它不受 PATH 顺序影响
for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do (
    if not defined PY set "PY=%%i"
)

rem 退而求其次：PATH 里的 python，但跳过应用商店的占位别名（WindowsApps）
set "STORE="
if not defined PY (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        if not defined PY (
            echo %%i | find /i "WindowsApps" >nul
            if errorlevel 1 (
                set "PY=%%i"
            ) else (
                set "STORE=1"
            )
        )
    )
)

if not defined PY (
    echo.
    if defined STORE (
        echo   [失败] 只找到 Windows 应用商店的占位 Python，它还没真正安装。
        echo.
        echo          系统里的 python.exe 只是个跳转链接，执行它会打开 Microsoft Store。
    ) else (
        echo   [失败] 没找到可用的 Python。
    )
    echo.
    echo          装一个 3.10 或更高版本，装官方版最稳，安装第一屏务必勾选
    echo          "Add python.exe to PATH"：
    echo.
    echo            https://www.python.org/downloads/
    echo.
    echo          从 Microsoft Store 装也可以，但要确认装完（不只是打开商店页面）。
    echo          装完关掉这个窗口，重新双击本文件。
    echo.
    pause
    exit /b 1
)

"%PY%" -c "import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if errorlevel 1 (
    "%PY%" -c "import sys;print('  [失败] 这个 Python 太旧：'+sys.version.split()[0]+'  '+sys.executable)"
    echo.
    echo          需要 3.10 或更高版本。换一个 Python 再试。
    echo.
    pause
    exit /b 1
)
for /f "delims=" %%v in ('"%PY%" -c "import sys;print(sys.version.split()[0])" 2^>nul') do echo          找到 Python %%v
echo          路径 %PY%

rem ---------------- 建 venv ----------------
echo   [2/4] 创建独立环境 .venv ...
"%PY%" -m venv "%VENV%"
if not exist "%VPY%" (
    echo.
    echo   [失败] 创建 .venv 失败。
    echo.
    echo          常见原因：
    echo            1. 这个 Python 是精简版 / 应用商店版，缺 venv 组件；
    echo            2. 当前目录没有写权限（不要放在 Program Files 里）；
    echo            3. 杀毒软件拦截了 python.exe 的复制。
    echo.
    echo          建议装官方 python.org 的完整版再试。
    echo.
    pause
    exit /b 1
)

:deps
echo   [3/4] 安装依赖（首次约 1-3 分钟）...
"%VPY%" -m pip install --upgrade pip --disable-pip-version-check -q >nul 2>&1
echo          正在从 PyPI 安装 ...
"%VPY%" -m pip install -r "%~dp0requirements.txt" --disable-pip-version-check
if errorlevel 1 (
    echo.
    echo          直连失败，改用国内镜像重试 ...
    echo.
    "%VPY%" -m pip install -r "%~dp0requirements.txt" -i "%MIRROR%" --disable-pip-version-check
    if errorlevel 1 (
        echo.
        echo   [失败] 依赖没装上。
        echo.
        echo          检查网络 / 代理后重试；公司网络需要代理时，
        echo          先在命令行设置 HTTPS_PROXY 再运行本文件。
        echo          日志里往上翻，能看到具体是哪个包失败。
        echo.
        pause
        exit /b 1
    )
)

rem ---------------- 自检 ----------------
echo   [4/4] 自检 ...
"%VPY%" -c "import playwright, openpyxl, sys; print('          Python ' + sys.version.split()[0] + ' · playwright / openpyxl 就绪')"
if errorlevel 1 (
    echo.
    echo   [失败] 依赖装了但导入失败，环境可能不完整。
    echo          删掉本目录下的 .venv 文件夹，重新运行本文件。
    echo.
    pause
    exit /b 1
)

rem ---------------- 可选组件 ----------------
echo.
echo   ------------------------------------------------------------------
echo   可选：自动过滑块（撞验证时先自己拖，拖不过才叫你）。
echo         需要额外下载约 100MB（ddddocr + opencv），不装也能跑完整流程。
echo.
set "OPT="
set /p "OPT=  现在安装吗？输入 y 安装，直接回车跳过："
if /i "!OPT!"=="y" (
    echo.
    echo   [可选] 安装 ddddocr / opencv ...
    "%VPY%" -m pip install -r "%~dp0requirements-optional.txt" --disable-pip-version-check
    if errorlevel 1 (
        echo          直连失败，改用国内镜像重试 ...
        "%VPY%" -m pip install -r "%~dp0requirements-optional.txt" -i "%MIRROR%" --disable-pip-version-check
    )
    "%VPY%" -c "import ddddocr, cv2; print('          自动过滑块组件就绪')" 2>nul || echo          [提示] 可选组件没装上，不影响主流程，之后可再运行本文件补装。
)

echo.
echo   ------------------------------------------------------------------
echo   环境就绪。
echo.
echo   下一步：双击「启动BOM选型台.bat」，它会自动起服务并打开界面。
echo.
pause
exit /b 0
