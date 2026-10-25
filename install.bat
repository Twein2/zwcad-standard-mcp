@echo off
rem Usage: install.bat [strict|off] [lisp|nolisp]
rem   strict|off   confirmation mode (default: keep the current setting, strict for a new install)
rem   lisp|nolisp  allow / forbid the execute_lisp tool (default: keep the current setting, off for a new install)
setlocal
cd /d "%~dp0"
set "CONFIGURE_ARGS="
if /i "%~1"=="strict" set "CONFIGURE_ARGS=--confirm-mode strict"
if /i "%~1"=="off" set "CONFIGURE_ARGS=--confirm-mode off"
if /i "%~1"=="lisp" set "CONFIGURE_ARGS=--enable-lisp"
if /i "%~1"=="nolisp" set "CONFIGURE_ARGS=--disable-lisp"
if /i "%~2"=="lisp" set "CONFIGURE_ARGS=%CONFIGURE_ARGS% --enable-lisp"
if /i "%~2"=="nolisp" set "CONFIGURE_ARGS=%CONFIGURE_ARGS% --disable-lisp"

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
.venv\Scripts\python.exe -m zwcad_standard_mcp.configure %CONFIGURE_ARGS%
if errorlevel 1 (
  echo [WARNING] Could not update the Claude Desktop config. See README, "Manual configuration".
)
echo.
echo Installation complete. Restart Claude Desktop, start ZWCAD, open a drawing and ask Claude to run diagnose_cad.
pause
endlocal
