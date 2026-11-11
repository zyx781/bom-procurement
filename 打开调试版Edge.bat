@echo off
rem BOM 采购台 · 打开调试版 Edge（独立 profile + CDP 9222）。UTF-8 编码，逻辑全在 bom-site\launcher.ps1。
chcp 65001 >nul
title BOM 采购台 · 调试版 Edge
mode con cols=78 lines=30 >nul 2>&1
setlocal
set "PS=%~dp0bom-site\launcher.ps1"

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

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS%" edge -NoPause
set "CODE=%errorlevel%"
if not "%CODE%"=="0" (
  echo.
  echo   启动器返回错误码 %CODE%，上方有具体原因。
  pause
)
exit /b %CODE%
