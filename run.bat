@echo off
rem ===========================================================================
rem  DrugScope launcher - double-click to start the app.
rem  First run creates the Python environment and installs dependencies;
rem  after that it starts straight away and opens your browser.
rem ===========================================================================
setlocal
cd /d "%~dp0"
title DrugScope

rem Already running? Open it instead of starting a second copy.
powershell -NoProfile -Command "try { if ((Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8501/_stcore/health -TimeoutSec 3).Content -eq 'ok') { exit 0 } } catch {}; exit 1" >nul 2>nul
if not errorlevel 1 (
    echo DrugScope is already running - opening it in your browser.
    start "" http://localhost:8501
    exit /b 0
)

set "PY=.venv\Scripts\python.exe"

if exist "%PY%" goto have_venv
echo Setting up the Python environment - first run only...
where py >nul 2>nul && py -3 -m venv .venv
if not exist "%PY%" python -m venv .venv
if not exist "%PY%" (
    echo.
    echo Python 3.10 or newer is required. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup, then run this file again.
    pause
    exit /b 1
)

:have_venv
"%PY%" -c "import streamlit, langgraph, anthropic, httpx, plotly, pandas, pypdf, dotenv, authlib, pptx, docx" >nul 2>nul
if not errorlevel 1 goto have_deps
echo Installing dependencies - this takes a minute or two the first time...
"%PY%" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo Dependency installation failed. Check your internet connection and try again.
    pause
    exit /b 1
)

:have_deps
if not exist ".env" if exist ".env.example" (
    copy /y ".env.example" ".env" >nul
    echo Created .env from .env.example - open it and add one API key, then restart.
)

echo.
echo Starting DrugScope - your browser will open at http://localhost:8501
echo Close this window to stop the app.
echo.
"%PY%" -m streamlit run app.py --server.headless false --server.address localhost --server.port 8501
if errorlevel 1 pause
endlocal
