@echo off
rem Scan a YouTube video and publish its skip list.
rem
rem   Double-click scan.bat     -> asks for the link
rem   scan.bat "<youtube link>" [--profile hearts2hearts] [scan.py options]
rem
rem Accepts youtube.com/watch, youtu.be and shorts links. Writes
rem skiplists\<profile>\<id>.json, refreshes the extension's bundled copy, then
rem commits ONLY those skip-list files and pushes them to GitHub, where the
rem extension looks first.
setlocal
cd /d "%~dp0"
set "INTERACTIVE="
set "URL="
if "%~1"=="" (
  set "INTERACTIVE=1"
  set /p "URL=Paste the YouTube link: "
) else (
  set "URL=%~1"
  shift
)
if not defined URL (
  echo No link given.
  goto :end
)
set "REST="
:collect
if "%~1"=="" goto :run
set REST=%REST% %1
shift
goto :collect

:run
if exist "cache\last_scan.json" del "cache\last_scan.json"
".venv\Scripts\python.exe" scan.py "%URL%" %REST%
if errorlevel 1 (
  echo.
  echo !! Scan failed - see the message above. Nothing was uploaded.
  goto :end
)
".venv\Scripts\python.exe" sync_skiplists.py >nul || (
  echo !! Could not refresh the bundled skip lists.
  goto :end
)
".venv\Scripts\python.exe" publish_skiplists.py

:end
if defined INTERACTIVE pause
