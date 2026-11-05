@echo off
rem Usage: scan.bat <youtube url> [--profile hearts2hearts] [scan.py options]
rem Scans the video, writes skiplists\<profile>\<id>.json, refreshes the extension's bundled copy.
setlocal
cd /d "%~dp0"
if "%~1"=="" (
  echo Usage: scan.bat ^<youtube url^> [options]
  exit /b 1
)
".venv\Scripts\python.exe" scan.py %* || exit /b 1
".venv\Scripts\python.exe" sync_skiplists.py
