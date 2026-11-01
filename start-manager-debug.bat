@echo off
REM  dsh manager DEBUG launcher
REM  NOTE: this file is intentionally PURE ASCII. cmd.exe decodes .bat using the
REM  system ANSI codepage (GBK on Chinese Windows), so UTF-8 Chinese here turns
REM  into garbage commands. Keep ASCII only.
REM  Machine-independent: uses %~dp0 (script dir), no hardcoded user paths.
setlocal
cd /d "%~dp0"

echo === dsh manager DEBUG launcher ===

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY where python >nul 2>nul && set "PY=python"

if not defined PY (
  echo [ERROR] No python found in PATH. Install Python 3.x first.
  pause
  exit /b 1
)

echo Running: %PY% "%~dp0dsh_manager.pyw"
echo Output is also saved to "%~dp0debug-run.log"
echo If the manager window opens normally, just close it.
echo.

%PY% "%~dp0dsh_manager.pyw" > "%~dp0debug-run.log" 2>&1
echo.
echo python exit code: %ERRORLEVEL%
echo.
echo Logs to check:
echo   %~dp0debug-run.log
echo   %~dp0dsh-manager.log
echo.
pause
