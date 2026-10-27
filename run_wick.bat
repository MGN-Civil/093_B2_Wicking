@echo off
rem Daily wick processing: asks for the date (yyyymmdd), reads that day's raw data from
rem _from_Machine and writes CSV, GeoPackage and IREDES to _from_Machine\<yyyymmdd>\outputs\
rem Double-click to run. Extra options can be passed, e.g.  run_wick.bat --all-designs

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python is not installed or not on PATH.
    pause
    exit /b 1
)

python wick_process.py %*
if errorlevel 1 (
    echo.
    echo Processing FAILED - see the message above.
) else (
    echo.
    echo Finished.
)
pause
