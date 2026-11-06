@echo off
title Magical Portal System - Setup
cd /d "%~dp0"
if not exist "%~dp0app\server.py" (
    echo.
    echo  Please extract the whole zip first ^(right-click the zip ^> Extract All^),
    echo  then run Install.bat from the extracted folder.
    echo.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\install.ps1"
echo.
pause
