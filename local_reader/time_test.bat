@echo off
rem The laptop test: how long ONE page takes on this computer.
rem Drag a scanned paper (PDF or picture) onto this file, or run:
rem   time_test.bat "C:\path\to\paper.pdf"
rem Send the numbers it prints back to whoever is setting this up.
cd /d "%~dp0"
if "%~1"=="" (
  echo Drag a scanned paper onto time_test.bat to time it.
  pause
  exit /b 1
)
if not exist venv\Scripts\python.exe (
  echo The reader is not set up on this computer yet - run setup.bat first.
  pause
  exit /b 1
)
venv\Scripts\python.exe reader.py --time "%~1"
pause
