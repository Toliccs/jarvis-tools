@echo off
REM jarvis-onboard.bat
REM Launcher for jarvis-onboard.ps1
REM Double-click and select "Run as administrator" when prompted

setlocal enabledelayedexpansion

REM Clear screen
cls

echo.
echo ╔════════════════════════════════════════════════════════════════╗
echo ║       JARVIS REMOTE ACCESS ONBOARDING SCRIPT LAUNCHER          ║
echo ╚════════════════════════════════════════════════════════════════╝
echo.
echo This script will configure this machine for Jarvis remote access.
echo.
echo REQUIRED: This must run as Administrator
echo ACTION:   Click "Yes" when Windows asks for permission
echo.
echo After completion:
echo   - A new "jarvis" account will be created
echo   - Remote access will be enabled
echo   - Check C:\jarvis-setup-log.txt for details
echo.
pause

REM Check for admin rights
net session >nul 2>&1
if %errorlevel% neq 0 (
    cls
    echo.
    echo ╔════════════════════════════════════════════════════════════════╗
    echo ║                      ERROR                                     ║
    echo ╚════════════════════════════════════════════════════════════════╝
    echo.
    echo This script requires Administrator privileges.
    echo.
    echo TO FIX:
    echo   1. Right-click this batch file (jarvis-onboard.bat)
    echo   2. Select "Run as administrator"
    echo   3. Click "Yes" when prompted
    echo.
    pause
    exit /b 1
)

REM Get script directory (works whether run from USB or local drive)
set "SCRIPT_DIR=%~dp0"
set "PS_SCRIPT=%SCRIPT_DIR%jarvis-onboard.ps1"

REM Verify PowerShell script exists
if not exist "!PS_SCRIPT!" (
    cls
    echo.
    echo ╔════════════════════════════════════════════════════════════════╗
    echo ║                      ERROR                                     ║
    echo ╚════════════════════════════════════════════════════════════════╝
    echo.
    echo Could not find jarvis-onboard.ps1
    echo Expected location: !PS_SCRIPT!
    echo.
    echo Make sure jarvis-onboard.bat and jarvis-onboard.ps1 are in the 
    echo same folder.
    echo.
    pause
    exit /b 1
)

cls
echo.
echo Running: !PS_SCRIPT!
echo.
echo ════════════════════════════════════════════════════════════════
echo.

REM Run PowerShell script with bypass execution policy
powershell -NoProfile -ExecutionPolicy Bypass -File "!PS_SCRIPT!"
set PSExitCode=!errorlevel!

echo.
echo ════════════════════════════════════════════════════════════════
echo.

if !PSExitCode! equ 0 (
    echo ✓ Setup completed successfully
    echo.
    echo Next steps:
    echo   - Check C:\jarvis-setup-log.txt for full details
    echo   - Tell Nick: "Setup successful on !COMPUTERNAME!"
    echo.
) else (
    echo ✗ Setup encountered errors
    echo.
    echo Check C:\jarvis-setup-log.txt for details
    echo.
)

echo Log location: C:\jarvis-setup-log.txt
echo.
pause
exit /b !PSExitCode!
