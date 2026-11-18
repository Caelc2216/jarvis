@echo off
rem Let an Amazon Echo send messages to Jarvis: "Alexa, ask Jarvis Computer to say dinner is ready".
title Jarvis - Alexa setup
cd /d "%~dp0"
set "PY=py"
py -c "" >nul 2>&1 || set "PY=python"
"%PY%" jarvis.py alexa %*
echo.
pause
