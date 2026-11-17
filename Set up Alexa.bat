@echo off
rem Let an Amazon Echo send messages to Jarvis: "Alexa, tell Jarvis that dinner is ready".
title Jarvis - Alexa setup
cd /d "%~dp0"
set "PY=py"
py -c "" >nul 2>&1 || set "PY=python"
"%PY%" alexa_setup.py
echo.
pause
