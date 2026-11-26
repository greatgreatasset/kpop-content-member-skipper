@echo off
rem Scans the videos friends ask for with the add-on's "Request filtering" button.
rem It listens hidden in the background: no window has to stay open.
rem   watch_requests.bat              set it up (double-click once): listens now and after every
rem                                   login, and shows the request code to give to friends
rem   watch_requests.bat /status      is it listening, the code, the last requests
rem   watch_requests.bat /stop        stop listening until the next login
rem   watch_requests.bat /uninstall   stop listening and don't start at login any more
rem   watch_requests.bat /window      listen in this window instead (to watch it work)
rem   watch_requests.bat --new-code   make a new request code (the old one stops working)
setlocal
cd /d "%~dp0"
title K-pop skipper scan requests
if not exist ".venv\Scripts\python.exe" (echo Run setup.bat first.& pause & exit /b 1)
set "ARG=%~1"
if "%ARG%"=="" set "ARG=--install"
if /i "%ARG%"=="/install" set "ARG=--install"
if /i "%ARG%"=="/uninstall" set "ARG=--uninstall"
if /i "%ARG%"=="/status" set "ARG=--status"
if /i "%ARG%"=="/stop" set "ARG=--stop"
if /i "%ARG%"=="/window" set "ARG="
".venv\Scripts\python.exe" request_watcher.py %ARG%
pause
