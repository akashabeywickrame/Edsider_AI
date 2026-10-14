@echo off
rem Starts the EdsiderAI reader and opens it in the browser.
rem Keep this window open while reading; close it when finished.
cd /d "%~dp0"
if not exist venv\Scripts\python.exe (
  echo The reader is not set up on this computer yet - run setup.bat first.
  pause
  exit /b 1
)
venv\Scripts\python.exe reader.py
pause
