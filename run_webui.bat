@echo off
REM ============================================
REM  LoRA Captioner WebUI launcher
REM  Change PORT below to listen on another port
REM ============================================
setlocal
cd /d "%~dp0"

REM ---- listen port (edit me) ----
set PORT=7860

if not exist ".venv\Scripts\python.exe" (
    echo [!] venv not found, creating it now...
    "C:\Users\ximu\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" -m venv .venv
    if exist ".venv\Scripts\python.exe" (
        ".venv\Scripts\python.exe" -m pip install -i https://mirrors.aliyun.com/pypi/simple/ fastapi "uvicorn[standard]" pillow pyyaml httpx
    ) else (
        echo [X] failed to create venv
        pause
        exit /b 1
    )
)

set CAPTIONER_PORT=%PORT%
echo.
echo   ================================
echo    LoRA Captioner WebUI
echo    http://127.0.0.1:%PORT%
echo    Press Ctrl+C to stop
echo   ================================
echo.
start "" http://127.0.0.1:%PORT%
".venv\Scripts\python.exe" -m app.main
pause
