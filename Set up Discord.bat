@echo off
rem Connect Jarvis to a private Discord bot so you can text it from your phone.
title Jarvis - Discord setup
cd /d "%~dp0"
set "PY=py"
py -c "" >nul 2>&1 || set "PY=python"
"%PY%" -m pip install -q --disable-pip-version-check discord.py
"%PY%" setup.py --discord
echo.
pause
