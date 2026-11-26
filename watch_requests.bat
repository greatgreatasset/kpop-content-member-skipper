@echo off
rem Scans the videos friends ask for with the add-on's "Request filtering" button.
rem Leave the window open; requests made while it is closed wait up to 12 hours.
rem   watch_requests.bat              listen and scan (shows the request code for friends)
rem   watch_requests.bat /install     also start it, minimized, when you log in to Windows
rem   watch_requests.bat /uninstall   stop starting it at login
rem   watch_requests.bat --new-code   make a new request code (the old one stops working)
setlocal
cd /d "%~dp0"
title K-pop skipper scan requests
if not exist ".venv\Scripts\python.exe" (echo Run setup.bat first.& pause & exit /b 1)
if /i "%~1"=="/install" (".venv\Scripts\python.exe" request_watcher.py --install & pause & exit /b)
if /i "%~1"=="/uninstall" (".venv\Scripts\python.exe" request_watcher.py --uninstall & pause & exit /b)
".venv\Scripts\python.exe" request_watcher.py %*
pause
