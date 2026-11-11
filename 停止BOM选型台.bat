@echo off
chcp 65001 >nul
setlocal
set "PS=%~dp0bom-site\launcher.ps1"
if not exist "%PS%" (
  echo [ERROR] Cannot find "%PS%"
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS%" stop
ping -n 3 127.0.0.1 >nul
exit /b %errorlevel%
