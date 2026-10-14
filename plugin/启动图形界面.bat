@echo off
cd /d "%~dp0"
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw dpi_gui.py 2> gui_error.log
) else (
    python dpi_gui.py
    if errorlevel 1 (
        echo.
        echo [LAUNCH FAILED] Please install Python 3.9+ and check "Add python.exe to PATH".
        echo Download: https://www.python.org/downloads/
        pause
    )
)
