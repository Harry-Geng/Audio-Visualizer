@echo off
REM ===================================================================
REM  Music Microscope - share your library with friends (temporary link)
REM  Double-click: starts the app READ-ONLY on your full library and
REM  opens a Cloudflare quick tunnel. Send friends the printed
REM  https://xxxx.trycloudflare.com link. Close this window to stop
REM  sharing (also close the minimized "microscope-share" window).
REM  Anyone with the link can listen while the tunnel is up - only run
REM  it while you're actually showing people.
REM ===================================================================
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo No environment found - run setup.bat first.
  pause
  exit /b 1
)

set "CLOUDFLARED=cloudflared"
if exist "cloudflared.exe" set "CLOUDFLARED=%~dp0cloudflared.exe"
where %CLOUDFLARED% >nul 2>nul
if not %errorlevel%==0 if not exist "cloudflared.exe" (
  echo cloudflared not found. Get it with:
  echo    winget install Cloudflare.cloudflared
  echo or drop cloudflared.exe next to this script:
  echo    https://github.com/cloudflare/cloudflared/releases
  pause
  exit /b 1
)

set PYTHONIOENCODING=utf-8
set AV_DEMO=1
set SHARE_PORT=8010

echo Starting Music Microscope (read-only guest mode) on port %SHARE_PORT%...
start "microscope-share" /min ".venv\Scripts\python.exe" microscope.py --host 127.0.0.1 --port %SHARE_PORT%

echo(
echo ============================================================
echo  Your share link appears below as  https://....trycloudflare.com
echo  Send that to your friends. CTRL+C here ends the sharing.
echo ============================================================
echo(
"%CLOUDFLARED%" tunnel --url http://127.0.0.1:%SHARE_PORT%

REM tunnel ended - stop the share server too
taskkill /fi "WINDOWTITLE eq microscope-share*" /f >nul 2>nul
