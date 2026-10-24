@echo off
rem Usage: install.bat [strict|off]   (confirmation mode, default strict)
setlocal
cd /d "%~dp0"
set "CONFIRM_MODE=%~1"
if "%CONFIRM_MODE%"=="" set "CONFIRM_MODE=strict"

if not exist .venv\Scripts\python.exe (
  py -3.11 -m venv .venv 2>nul || py -3 -m venv .venv 2>nul || python -m venv .venv
)
if not exist .venv\Scripts\python.exe (
  echo [ERROR] Python 3.10+ is required. Install it from https://www.python.org/downloads/windows/
  echo         and tick "Add python.exe to PATH".
  pause
  exit /b 1
)
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e .
if errorlevel 1 (
  echo [ERROR] Package installation failed. See the messages above.
  pause
  exit /b 1
)
echo.
echo Registering the server in Claude Desktop...
.venv\Scripts\python.exe -m zwcad_standard_mcp.configure --confirm-mode %CONFIRM_MODE%
if errorlevel 1 (
  echo [WARNING] Could not update the Claude Desktop config. See README, "Manual configuration".
)
echo.
echo Installation complete. Restart Claude Desktop, start ZWCAD, open a drawing and ask Claude to run diagnose_cad.
pause
endlocal
