@echo off
rem Update the scripts in this folder from GitHub (MGN-Civil/093_B2_Wicking).
rem Double-click to run. Works from wherever this folder lives.

cd /d "%~dp0"

where git >nul 2>nul
if errorlevel 1 (
    echo Git is not installed. Install it with:  winget install Git.Git
    pause
    exit /b 1
)

if not exist ".git" (
    echo This folder is not a git clone yet. See the README or ask for the setup steps.
    pause
    exit /b 1
)

echo Updating %CD% ...
git pull
if errorlevel 1 (
    echo.
    echo Update FAILED - see the message above.
) else (
    echo.
    echo Up to date.
)
pause
