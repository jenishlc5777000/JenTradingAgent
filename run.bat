@echo off
rem Start the TradingAgents web UI.
rem   run.bat            web UI on http://localhost:8501 (opens your browser)
rem   run.bat cli        the terminal version instead
rem   run.bat web 9000   web UI on another port
setlocal
cd /d "%~dp0"

python --version >nul 2>&1 || (echo Python 3.11+ was not found on PATH. & exit /b 1)

python -c "import tradingagents, webui" >nul 2>&1
if errorlevel 1 (
    echo Installing TradingAgents and its dependencies...
    python -m pip install -e . || exit /b 1
)

set "MODE=%~1"
if "%MODE%"=="" set "MODE=web"

set "PORT=8501"
if not "%~2"=="" set "PORT=%~2"

if /i "%MODE%"=="cli" goto cli
python -m cli.main web --port %PORT%
exit /b %errorlevel%

rem a label, because %PORT% inside a parenthesised block expands too early
:cli
python -m cli.main
