@echo off
REM  ===================================================================
REM   Open dsh Manager (start the GUI)
REM   This launcher only OPENS the manager. It does NOT install anything.
REM
REM   Why this file exists: people expected one double-click to both set up
REM   and launch dsh, and got confused about which file to run. This file
REM   has an unambiguous name: it only starts the GUI. Installing a dsh
REM   version is done from the window itself (Install official dsh).
REM
REM   NOTE: pure ASCII on purpose. cmd.exe decodes .bat with the system
REM   ANSI codepage (GBK on Chinese Windows), so UTF-8 Chinese here would
REM   turn into garbage commands. This also avoids the ")" inside
REM   parenthesised blocks problem: never put parentheses in echo text.
REM   Machine-independent: uses %~dp0, no hardcoded user paths.
REM  ===================================================================
setlocal
cd /d "%~dp0"

title dsh Manager

echo ============================================================
echo   dsh Manager - starting the GUI...
echo ============================================================
echo.

REM  1) Prefer the bundled portable Python - it always has tkinter.
set "PYW=%~dp0runtime\python\pythonw.exe"
if exist "%PYW%" goto bundled

REM  2) Fall back to pythonw on PATH.
where pythonw >nul 2>nul
if not errorlevel 1 goto path_pw

REM  3) Fall back to the py launcher's pythonw.
where py >nul 2>nul
if not errorlevel 1 goto path_py

echo [ERROR] No usable Python was found.
echo.
echo   The manager needs Python with tkinter. Easiest fix: install Python
echo   from https://www.python.org/downloads/ and tick the tcl-tk option.
echo   Or copy the runtime\python folder next to this file and retry.
echo.
echo   You can also run the environment check first:
echo     python scripts\check-env.py
echo.
goto :done

:bundled
start "" "%PYW%" "%~dp0dsh_manager.pyw"
echo Started with the bundled portable Python.
goto :note

:path_pw
start "" pythonw "%~dp0dsh_manager.pyw"
echo Started with pythonw from PATH.
goto :note

:path_py
start "" pyw "%~dp0dsh_manager.pyw"
echo Started with the py launcher.
goto :note

:note
echo.
echo   If no window appears, diagnostics are written to dsh-manager.log
echo   in the manager folder.
echo   To see errors on screen instead, double-click start-manager-debug.bat
echo   - it uses a console Python and prints tracebacks.
echo.

:done
endlocal