@echo off
rem ---------------------------------------------------------------
rem  lsl_apdf - UNIFIED GUI launcher WITH console (troubleshooting)
rem
rem  Same as start_unified.bat but keeps the command window open so
rem  you can read any error message. Use this one when the no-console
rem  launcher appears to do nothing.
rem
rem  ASCII-only on purpose: cmd.exe parses a .bat with the console
rem  codepage active at startup, so non-ASCII here can break parsing.
rem ---------------------------------------------------------------
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   UNIFIED GUI - console mode (for troubleshooting)
echo ============================================================
echo.
echo   Config : session_config_real.json
echo   Driver : DRY-RUN by default; tick the real-driver box to write
echo   Close this window to quit the app.
echo.
echo   Starting...
echo.

python -m lsl_apdf gui --unified --session-config session_config_real.json

echo.
echo [exited with code %ERRORLEVEL%]
echo If it failed, the traceback is above and also in lsl_apdf\last_crash.log
pause
