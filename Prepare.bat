@echo off
rem Downloads the official UniFi / OpenWrt / mtk_uartboot files and builds the recovery images.
rem Needs Python 3.8+ (https://www.python.org/downloads/) and internet, once per PC.
setlocal
where py >nul 2>nul && (py -3 "%~dp0prepare\prepare.py" %* & goto :done)
where python >nul 2>nul && (python "%~dp0prepare\prepare.py" %* & goto :done)
echo Python 3 was not found. Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH") and run this again.
:done
echo.
pause
