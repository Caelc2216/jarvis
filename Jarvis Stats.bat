@echo off
rem How fast Jarvis has been answering, and how much CPU and memory it uses (last 24 hours).
title Jarvis stats
cd /d "%~dp0"
set "PY=py"
py -c "" >nul 2>&1 || set "PY=python"
set JARVIS_PAUSE=1
"%PY%" jarvis.py stats 24
