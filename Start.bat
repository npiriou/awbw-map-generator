@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please double-click Install.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m backend.server --open-browser %*
if errorlevel 1 (
  echo.
  echo The generator could not start. Check the error above.
  echo If software is missing, run Install.bat again.
  pause
  exit /b 1
)
