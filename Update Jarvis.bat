@echo off
rem Downloads and installs the newest Jarvis. Your settings and everything it has learned are kept.
title Update Jarvis
cd /d "%~dp0"
set "PY=py"
py -c "" >nul 2>&1 || set "PY=python"
"%PY%" updater.py --apply --restart
echo.
pause
