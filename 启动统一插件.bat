@echo off
rem ---------------------------------------------------------------
rem  lsl_apdf - UNIFIED plugin launcher (single process)
rem  Acquisition -> %MVC -> APDF -> balance decision -> DPI write
rem  No TCP, no port, one config, one session folder.
rem  Default is DRY-RUN (nothing is written to Raw Accel).
rem
rem  ASCII-only on purpose: cmd.exe parses a .bat with the console
rem  codepage active at startup, so non-ASCII here can break parsing.
rem ---------------------------------------------------------------
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo   UNIFIED PLUGIN  (single process, no TCP)
echo ============================================================
echo.
echo   signal side  : LSL / synthetic -^> filter -^> RMS -^> %MVC -^> APDF
echo   decision     : high-exposure area of ECU vs FDS -^> crossings
echo   execution    : 800 ^<-^> 1200 DPI ramp, written in-process
echo   driver       : DRY-RUN (nothing written to Raw Accel)
echo   source       : synthetic 4-muscle (no hardware needed)
echo   takes        : about 80 seconds
echo.
echo   Output: sessions\session_*\  with
echo     pctmvc.csv, apdf_summary.csv, sens_log_*.csv,
echo     balance_events_*.csv, session_summary.json
echo.
echo   REAL experiment (writes Raw Accel, changes mouse DPI):
echo     python -m lsl_apdf unified --session-config session_config_example.json ^
echo         --real-driver --mac 00:07:80:AA:BB:CC
echo   (set the MVC values you measured on site first)
echo.
echo   Starting in 3 seconds (Ctrl+C to abort)...
timeout /t 3 /nobreak >nul

python -m lsl_apdf unified ^
    --session-config session_config_linktest.json ^
    --synthetic --duration 80
set RC=%ERRORLEVEL%

echo.
if not "%RC%"=="0" (
  echo [WARNING] exit code = %RC%
  echo.
)
echo Done. Result folder: sessions\
echo.
pause
