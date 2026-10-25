@echo off
REM ===========================================================================
REM  Voltline Support Console - double-click this file to run the project.
REM
REM  Opens two windows: this one, and a server window. Close the server window
REM  when you are finished.
REM ===========================================================================

cd /d "%~dp0"
title Voltline - launcher
setlocal

if not exist ".venv\Scripts\python.exe" (
  echo.
  echo   The Python environment is missing. Run these two lines once:
  echo.
  echo     python -m venv .venv
  echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  exit /b 1
)

set "PY=.venv\Scripts\python.exe"
set "PYTHONIOENCODING=utf-8"

REM A server left over from an earlier run keeps port 8000 AND keeps serving
REM the .env as it was when that process started. That is how "I added my API
REM key but it still says offline" happens.
echo.
echo   Freeing port 8000 ...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:":8000 .*LISTENING"') do taskkill /f /pid %%P >nul 2>&1

echo   Resetting the demo data ...
%PY% -m alembic upgrade head >nul 2>&1
%PY% -m scripts.seed_data >nul 2>&1
%PY% -m scripts.ingest_policies >nul 2>&1

echo.
echo   Which agents will run:
%PY% -m scripts.launcher mode

echo.
echo   Starting the server ...
start "Voltline server - close this window to stop" %PY% -m uvicorn app.main:app --port 8000

echo   Waiting for it to come up ...
%PY% -m scripts.launcher wait

start "" http://localhost:8000

echo.
echo   The console is open at http://localhost:8000
echo   Stop it by closing the window titled "Voltline server".
echo.
pause
