@echo off
rem BOM 采购台 · 主启动器。UTF-8 编码，逻辑全在 bom-site\launcher.ps1。
rem 可带参数：status（看状态）/ diag（只读体检）/ restart（重启）/ stop（停止）/ help（用法）
chcp 65001 >nul
title BOM 采购台 · 启动器
mode con cols=78 lines=34 >nul 2>&1
setlocal
set "PS=%~dp0bom-site\launcher.ps1"
set "ACTION=start"
if /i "%~1"=="status"  set "ACTION=status"
if /i "%~1"=="diag"    set "ACTION=diag"
if /i "%~1"=="restart" set "ACTION=restart"
if /i "%~1"=="stop"    set "ACTION=stop"
if /i "%~1"=="help"    set "ACTION=help"

if not exist "%PS%" (
  echo.
  echo   [失败] 找不到启动器
  echo          %PS%
  echo.
  echo   请确认 bom-site 目录与启动脚本在同一层，且仓库已完整解压。
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS%" %ACTION% -NoPause
set "CODE=%errorlevel%"
if not "%CODE%"=="0" (
  echo.
  echo   启动器返回错误码 %CODE%，上方有具体原因。
  pause
)
exit /b %CODE%
