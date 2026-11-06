@echo off
title Magical Portal System
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (
    python server.py %*
    goto :end
)
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 server.py %*
    goto :end
)

echo.
echo  Python was not found.
echo  Install it with:  winget install Python.Python.3.12
echo  (or from python.org - tick "Add python.exe to PATH"), then run this again.
echo.

:end
pause
