@echo off
rem Copy a day's IREDES XML files to Data\XML\MWD and Data\XML\XML,
rem skipping any already in those folders' _archive.
rem Double-click to run. Asks for the date, then whether to do a dry run first.

cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python is not installed or not on PATH.
    pause
    exit /b 1
)

set RUNDATE=
set /p RUNDATE=Folder name (yyyymmdd), Enter = today: 
if "%RUNDATE%"=="" for /f %%d in ('python -c "import datetime;print(datetime.date.today().strftime('%%Y%%m%%d'))"') do set RUNDATE=%%d

choice /c YN /m "Dry run first (show what would be copied, copy nothing)"
if errorlevel 2 goto copy

python wick_copy_xml.py --date %RUNDATE% --dry-run %*
if errorlevel 1 goto failed
echo.
choice /c YN /m "Go ahead and copy"
if errorlevel 2 goto end

:copy
python wick_copy_xml.py --date %RUNDATE% %*
if errorlevel 1 goto failed
echo.
echo Finished.
goto end

:failed
echo.
echo Copy FAILED - see the message above.

:end
pause
