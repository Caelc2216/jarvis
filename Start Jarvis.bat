@echo off
rem Starts Jarvis in the background (it waits for "Hey Jarvis" or a double clap).
cd /d "%~dp0"
set "PY=py"
py -c "" >nul 2>&1 || set "PY=python"
"%PY%" startup.py start
timeout /t 3 >nul
