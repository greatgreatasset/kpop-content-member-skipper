@echo off
rem Sign the extension with Mozilla so it installs permanently in normal Firefox,
rem then publish it as a GitHub release (installed copies auto-update).
rem
rem "Unlisted" signing: the add-on is NOT shown in Mozilla's add-on store.
rem You need an addons.mozilla.org API key: https://addons.mozilla.org/developers/
rem -> Tools -> Manage API Keys. The key and secret are passed to Mozilla's
rem signing tool for this run only; they are not saved anywhere.
rem
rem Mozilla signs each version once: raise "version" in extension\manifest.json
rem before signing a changed extension again.
setlocal
cd /d "%~dp0"
for /f "usebackq delims=" %%v in (`powershell -NoProfile -Command "(Get-Content extension\manifest.json -Raw | ConvertFrom-Json).version"`) do set "VER=%%v"
echo Signing extension version %VER%.
echo.
set "KEY="
set "SECRET="
set /p "KEY=API key (JWT issuer, looks like user:12345:678): "
for /f "usebackq delims=" %%s in (`powershell -NoProfile -Command "$s = Read-Host 'API secret (input hidden)' -AsSecureString; [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))"`) do set "SECRET=%%s"
if not defined KEY goto :missing
if not defined SECRET goto :missing

echo.
echo Asking Mozilla to sign it (this can take a few minutes)...
rem passed via environment (not the command line, which other programs can see)
set "WEB_EXT_API_KEY=%KEY%"
set "WEB_EXT_API_SECRET=%SECRET%"
set "KEY="
set "SECRET="
call npx --yes web-ext sign --channel=unlisted --source-dir extension --artifacts-dir dist
set "RC=%errorlevel%"
set "WEB_EXT_API_KEY="
set "WEB_EXT_API_SECRET="
if not "%RC%"=="0" (
  echo.
  echo !! Signing failed. If it says this version already exists, raise "version"
  echo    in extension\manifest.json and run sign.bat again.
  goto :end
)

echo.
echo Publishing it on GitHub...
".venv\Scripts\python.exe" release.py
if errorlevel 1 (
  echo !! The add-on is signed but publishing failed - see above. The .xpi is in dist\.
)
echo.
echo Signed add-on:
dir /b dist\kpop-content-member-skipper-%VER%.xpi 2>nul || dir /b dist\*.xpi
echo.
echo To install: drag the .xpi file onto a Firefox window (or File ^> Open File) and
echo click Add. Then remove the temporary copy in about:debugging.
start "" "%~dp0dist"
goto :end

:missing
echo The key and the secret are both needed.

:end
pause
