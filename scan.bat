@echo off
rem Scan a YouTube video and upload its skip list. Double-click it, or:
rem   scan.bat <youtube link> [--profile hearts2hearts] [--no-push] [scan.py options]
rem Kept tiny on purpose: all logic is in scan_cli.py (cmd.exe re-reads a running
rem .bat file from disk, so a long one breaks if it is edited mid-scan).
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Run setup.bat first.& pause & exit /b 1)
".venv\Scripts\python.exe" scan_cli.py %*