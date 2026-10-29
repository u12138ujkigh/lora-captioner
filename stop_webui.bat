@echo off
REM ============================================
REM  LoRA Captioner WebUI stopper
REM  PORT lives in stop_webui.ps1, keep it in
REM  sync with run_webui.bat
REM ============================================
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_webui.ps1"

echo   Press any key to close...
pause >nul
