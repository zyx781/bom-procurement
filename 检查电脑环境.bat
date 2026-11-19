@echo off
rem BOM 采购台 · 环境自检：淘宝打不开时先双击这里。
rem 逐项检查运行时 / Edge / 组策略 / 调试端口 / 网络，报告存到 bom-site\runs\。
chcp 65001 >nul
title BOM 采购台 · 环境自检
mode con cols=88 lines=42 >nul 2>&1
setlocal

set "PS=%~dp0bom-site\doctor.ps1"
if not exist "%PS%" (
  echo.
  echo   [失败] 找不到自检脚本
  echo          %PS%
  echo.
  echo   请确认 bom-site 目录与本文件在同一层，且压缩包已完整解压。
  echo.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS%"
set "CODE=%errorlevel%"
if not "%CODE%"=="0" (
  echo.
  echo   自检脚本以错误码 %CODE% 结束，上方有具体结论。
  pause
)
exit /b %CODE%
