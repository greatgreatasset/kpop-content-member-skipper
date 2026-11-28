@echo off
rem Started by the browser for the add-on's "Filter this video" button (see native_host.py).
rem Must print nothing itself: stdout carries the messages to the browser.
"%~dp0.venv\Scripts\python.exe" "%~dp0native_host.py" %*
