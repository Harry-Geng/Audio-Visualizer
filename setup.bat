@echo off
REM ===================================================================
REM  Music Microscope - one-time setup (Windows)
REM  Double-click this file. It creates a local Python environment and
REM  installs everything the app needs. Safe to re-run.
REM ===================================================================
cd /d "%~dp0"
echo(
echo === Music Microscope setup ===
echo(

REM --- 1. locate Python 3.12 and create the virtual environment ---
if exist ".venv\Scripts\python.exe" goto haveenv
echo Creating Python environment (.venv)...
py -3.12 -m venv .venv 2>nul
if exist ".venv\Scripts\python.exe" goto haveenv
py -m venv .venv 2>nul
if exist ".venv\Scripts\python.exe" goto haveenv
python -m venv .venv 2>nul
if exist ".venv\Scripts\python.exe" goto haveenv
echo(
echo ERROR: could not create a Python environment.
echo Install Python 3.12 from https://www.python.org/downloads/ (tick "Add to PATH"),
echo then double-click setup.bat again.
echo(
pause
exit /b 1
:haveenv

REM --- 2. pick GPU (NVIDIA) vs CPU requirements ---
set "REQ=requirements.txt"
where nvidia-smi >nul 2>nul
if %errorlevel%==0 (
  set "REQ=requirements-cuda.txt"
  echo NVIDIA GPU detected - installing the CUDA build ^(fast^).
) else (
  echo No NVIDIA GPU detected - installing the CPU build ^(works, but slow^).
)
echo(

REM --- 3. install dependencies ---
echo Upgrading pip...
call ".venv\Scripts\python.exe" -m pip install --upgrade pip
echo(
echo Installing packages from %REQ% ^(this can take several minutes^)...
call ".venv\Scripts\python.exe" -m pip install -r %REQ%
if not %errorlevel%==0 (
  echo(
  echo ERROR: package install failed. Scroll up for the reason, then re-run setup.bat.
  pause
  exit /b 1
)
echo(

REM --- 4. ffmpeg (needed to decode audio + YouTube/SoundCloud links) ---
where ffmpeg >nul 2>nul
if %errorlevel%==0 goto haveff
echo ffmpeg not found - trying to install it with winget...
winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
where ffmpeg >nul 2>nul
if %errorlevel%==0 goto haveff
echo(
echo NOTE: ffmpeg still isn't on PATH. Adding music from files/links needs it.
echo Install it from https://www.gyan.dev/ffmpeg/builds/ or run:  winget install Gyan.FFmpeg
echo Then open a NEW terminal window so PATH refreshes.
:haveff

echo(
echo === Setup complete! ===
echo Double-click  run.bat  to start Music Microscope.
echo(
pause
