@echo off
setlocal
title Jarvis setup
cd /d "%~dp0"
echo.
echo   Setting up Jarvis. This window will ask you a few questions.
echo   (The first time can take a few minutes while things download.)
echo.

rem --- 1. Make sure Python is installed (the real one, not the Microsoft Store placeholder) ---
set "PY="
for %%P in (py python) do (
  if not defined PY (
    %%P -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=%%P"
  )
)
if not defined PY (
  echo Installing Python...
  winget install -e --id Python.Python.3.13 --scope user --accept-source-agreements --accept-package-agreements --silent
  if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
)
if not defined PY (
  echo.
  echo Couldn't install Python automatically.
  echo Please install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^),
  echo then double-click setup.bat again.
  pause
  exit /b 1
)

rem --- 2. Install what Jarvis needs ---
echo Installing Jarvis's parts...
"%PY%" -m pip install --upgrade pip --quiet --disable-pip-version-check
"%PY%" -m pip install -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 (
  echo.
  echo Something went wrong installing Jarvis's parts. Check your internet connection and try again.
  pause
  exit /b 1
)

rem --- 3. The questions ---
"%PY%" setup.py
echo.
pause
