@echo off
rem Stops the Jarvis running in the background.
cd /d "%~dp0"
set "PY=py"
py -c "" >nul 2>&1 || set "PY=python"
"%PY%" startup.py stop
timeout /t 3 >nul
