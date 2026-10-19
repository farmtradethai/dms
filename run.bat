@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d %~dp0
title Die Management System

where python >nul 2>nul
if %errorlevel%==0 ( set PY=python ) else ( set PY=py -3 )

if not exist .venv (
  echo [1/3] Creating virtual environment ...
  %PY% -m venv .venv || ( echo Python 3.10+ not found. Install from python.org and tick "Add to PATH". & pause & exit /b 1 )
)
call .venv\Scripts\activate.bat

echo [2/3] Installing packages (first time only, 1-2 min) ...
python -m pip install --quiet --no-cache-dir --disable-pip-version-check -r requirements.txt
if errorlevel 1 ( echo pip install failed - check internet connection. & pause & exit /b 1 )

if not exist data\.seeded (
  echo [3/3] First run: creating AH / AHP / AHR, die register and importing Power Apps history ...
  echo       Please wait 1-3 minutes. Do NOT close this window.
  python scripts\seed_ah.py --with-history
  if errorlevel 1 ( echo Seed failed - see error above. & pause & exit /b 1 )
)

echo.
echo ================================================================
echo  Die Management System  ^-^>  http://localhost:8790
echo  Login:  ah_admin / 1234   (superadmin: admin / admin1234)
echo  Other roles (password 1234): ah_prod ah_foreman ah_leader ah_tech1 ah_tech2 ah_qc ah_manager
echo  Keep this window open. Press Ctrl+C to stop.
echo ================================================================
start "" http://localhost:8790
python -m uvicorn app.main:app --host 0.0.0.0 --port 8790
pause
