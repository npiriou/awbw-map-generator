@echo off
setlocal
cd /d "%~dp0"
echo AWBW Map Generator - Installation
echo.
if exist ".venv\Scripts\python.exe" goto dependencies
set "TASK_PY=py -3.12"
%TASK_PY% --version >nul 2>&1
if not errorlevel 1 goto createenv
set "TASK_PY=py -3"
%TASK_PY% --version >nul 2>&1
if not errorlevel 1 goto createenv
set "TASK_PY=python"
%TASK_PY% --version >nul 2>&1
if not errorlevel 1 goto createenv
echo Python was not found. Install Python 3.12 from python.org first.
echo Select "Add python.exe to PATH" in the installer, then run this file again.
pause
exit /b 1
:createenv
%TASK_PY% -c "import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 14) else 1)"
if errorlevel 1 (
  echo Please install Python 3.12 and run this file again.
  pause
  exit /b 1
)
echo Preparing a private Python environment...
%TASK_PY% -m venv ".venv"
if errorlevel 1 goto failed
:dependencies
echo Downloading the required software. This may take a few minutes...
".venv\Scripts\python.exe" -m pip install -r requirements.txt --index-url https://download.pytorch.org/whl/cpu
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -c "import torch; from backend.server import MODEL_PATH; assert MODEL_PATH.is_file(), 'Missing models/ppo-48000.pt'; torch.load(MODEL_PATH, map_location='cpu', weights_only=True)"
if errorlevel 1 goto failed
echo.
echo Installation complete. Double-click Start.bat to open the generator.
pause
exit /b 0
:failed
echo.
echo Installation could not finish. Check the error above and try again.
echo An internet connection is needed for the first installation.
pause
exit /b 1
