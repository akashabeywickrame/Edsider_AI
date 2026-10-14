@echo off
rem One-time setup for the EdsiderAI reader on this computer.
rem Needs Python 3.11 (python.org, or: winget install Python.Python.3.11)
rem and an internet connection. Takes 10-20 minutes; about 4 GB of disk.
setlocal
cd /d "%~dp0"

rem A deep folder breaks the install: one of torch's files has a very long
rem name, and Windows refuses paths over 260 characters (measured - it
rem failed from inside a Temp folder). Keep this folder near the drive root.
set "HERE=%~dp0"
if not "%HERE:~40,1%"=="" (
  echo.
  echo This folder is too deep for the install to work:
  echo   %HERE%
  echo Move the whole folder somewhere short, such as C:\EdsiderReader, then run setup.bat again.
  echo.
  pause
  exit /b 1
)

py -3.11 --version >nul 2>nul
if errorlevel 1 (
  echo.
  echo Python 3.11 is not installed. Install it from python.org, or run:
  echo   winget install Python.Python.3.11
  echo then run setup.bat again.
  echo.
  pause
  exit /b 1
)

if not exist venv\Scripts\python.exe (
  echo Creating a private Python for the reader...
  py -3.11 -m venv venv || goto :failed
)

echo Installing the reader - this is the slow part...
venv\Scripts\python.exe -m pip install --upgrade pip --quiet || goto :failed
venv\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cpu --quiet || goto :failed
venv\Scripts\python.exe -m pip install -r requirements.txt --quiet || goto :failed

rem The program that runs the reading model. Pinned to the build that was
rem tested; the CPU build, since these computers have no NVIDIA card.
if not exist llamacpp\llama-server.exe (
  echo Downloading the model runner...
  powershell -NoProfile -Command ^
    "$ErrorActionPreference='Stop';" ^
    "Invoke-WebRequest 'https://github.com/ggml-org/llama.cpp/releases/download/b11577/llama-b11577-bin-win-cpu-x64.zip' -OutFile 'llamacpp.zip';" ^
    "Expand-Archive -Force 'llamacpp.zip' 'llamacpp';" ^
    "Remove-Item 'llamacpp.zip'" || goto :failed
)

echo.
echo Done. Double-click start.bat to use the reader.
echo (The first paper also downloads the reading model, about 1.5 GB.)
echo.
pause
exit /b 0

:failed
echo.
echo Setup did not finish - see the message above. Run setup.bat again once it is fixed.
echo.
pause
exit /b 1
