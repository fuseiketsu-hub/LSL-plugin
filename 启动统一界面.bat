@echo off
rem ---------------------------------------------------------------
rem  lsl_apdf - UNIFIED GUI launcher (NO console window)
rem
rem  Launches with pythonw.exe (no console) and immediately exits,
rem  so no command window stays open. Expect at most a brief flash.
rem
rem  If nothing appears when you double-click this:
rem    -> run  start_unified_console.bat  to see the real error
rem    -> or read  lsl_apdf\last_crash.log
rem
rem  ASCII-only on purpose: cmd.exe parses a .bat with the console
rem  codepage active at startup, so non-ASCII here can break parsing.
rem ---------------------------------------------------------------
cd /d "%~dp0"

start "" pythonw.exe -m lsl_apdf gui --unified --session-config session_config_real.json

exit
