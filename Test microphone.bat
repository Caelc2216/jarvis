@echo off
rem Checks Jarvis can hear you, fixes the Windows setting if it's blocked, and lets you pick your mic.
title Jarvis - microphone test
cd /d "%~dp0"
set "PY=py"
py -c "" >nul 2>&1 || set "PY=python"
"%PY%" setup.py --mic
echo.
pause
