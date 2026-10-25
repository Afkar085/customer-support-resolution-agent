@echo off
REM ===========================================================================
REM  Voltline Support Console - double-click this file to run the project.
REM
REM  It resets the demo data to a known state, starts the server, and opens
REM  the browser. Close the black window when you are done.
REM ===========================================================================

cd /d "%~dp0"
title Voltline Support Console

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   The Python environment is missing. Run this once, then try again:
  echo.
  echo     python -m venv .venv
  echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

set PY=.venv\Scripts\python.exe
set PYTHONIOENCODING=utf-8

REM A server left running from a previous session would keep port 8000 and,
REM worse, would still be using the .env as it was when IT started -- which is
REM how "I added my API key but it still says offline" happens.
echo.
echo   Freeing port 8000...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:":8000 .*LISTENING"') do (
  taskkill /f /pid %%P >nul 2>&1
)

echo   Preparing the demo...
%PY% -m alembic upgrade head >nul 2>&1
%PY% -m scripts.seed_data >nul 2>&1
%PY% -m scripts.ingest_policies >nul 2>&1

for /f "delims=" %%M in ('%PY% -c "from app.api.routes import _model_mode; from app.config import get_settings; s=get_settings(); m=_model_mode(s); print(m if m=='offline' else s.model_name)" 2^>nul') do set AGENTS=%%M
echo   Agents: %AGENTS%
echo   Starting the console at http://localhost:8000
echo.
echo   Leave this window open. Close it to stop the server.
echo.

start "" http://localhost:8000
%PY% -m uvicorn app.main:app --port 8000
