@echo off
rem Runs Jarvis in this window instead of hidden, so any error shows up on screen.
title Jarvis - troubleshooting
cd /d "%~dp0"
set "PY=py"
py -c "" >nul 2>&1 || set "PY=python"
echo Stopping the background Jarvis (if it's running)...
"%PY%" startup.py stop
echo.
echo Checking Jarvis's parts are installed...
"%PY%" -m pip install -r requirements.txt --quiet --disable-pip-version-check
echo.
echo Starting Jarvis here. Say "Hey Jarvis". If you see an error, take a screenshot of this window.
echo (Close this window when you're done, then double-click Start Jarvis.bat.)
echo.
"%PY%" jarvis.py
echo.
echo ---- Jarvis stopped. If there's an error above, screenshot it. ----
pause
