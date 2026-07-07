@echo off
REM ===================================================================
REM  Music Microscope - start the app (Windows)
REM  Double-click this file. It opens the app in your browser.
REM  Close this window (or press Ctrl+C) to stop the app.
REM ===================================================================
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo No environment found yet. Double-click  setup.bat  first.
  echo(
  pause
  exit /b 1
)

echo Starting Music Microscope...
echo Opening http://127.0.0.1:8000  (leave this window open while you use it)
start "" http://127.0.0.1:8000
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" microscope.py
