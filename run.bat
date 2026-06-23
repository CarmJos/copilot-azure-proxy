@echo off
title copilot-azure-proxy — :4000
setlocal enabledelayedexpansion

:: Force Python UTF-8 mode (fixes YAML decode on Chinese Windows)
set PYTHONUTF8=1

cd /d "%~dp0"

:start

:: ── Free port 4000 ────────────────────────────────────────────────────────────
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":4000 "') do (
    if not "%%a"=="" (
        echo Killing existing process on port 4000 (PID %%a^)...
        taskkill /F /PID %%a >nul 2>&1
    )
)

:: ── Check .venv ───────────────────────────────────────────────────────────────
set "VENV_PYTHON=%~dp0.venv\Scripts\python.exe"
set "VENV_PIP=%~dp0.venv\Scripts\pip.exe"

if not exist "%VENV_PYTHON%" (
    echo ERROR: .venv not found.
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install aiohttp litellm pyyaml
    pause
    exit /b 1
)

:: ── Ensure dependencies ───────────────────────────────────────────────────────
for %%p in (aiohttp litellm yaml) do (
    "%VENV_PYTHON%" -c "import %%p" 2>nul
    if errorlevel 1 (
        echo Installing %%p...
        "%VENV_PIP%" install %%p
    )
)

echo.
echo ============================================
echo  copilot-azure-proxy
echo  Press Ctrl+C to stop.
echo ============================================
echo.

"%VENV_PYTHON%" copilot_azure_proxy.py --config config.yaml

pause

goto start