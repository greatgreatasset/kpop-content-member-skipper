@echo off
rem Lets the Firefox add-on start scans on this PC ("Filter this video" button).
rem   install_native_host.bat              install (per user, no admin needed)
rem   install_native_host.bat /uninstall   remove
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (echo Run setup.bat first.& pause & exit /b 1)
if /i "%~1"=="/uninstall" (".venv\Scripts\python.exe" native_host.py --uninstall) else (".venv\Scripts\python.exe" native_host.py --install)
if "%~1"=="" pause
