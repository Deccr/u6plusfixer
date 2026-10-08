@echo off
rem U6+ eMMC recovery - console version (the GUI is U6Plus-Fixer.bat)
rem Options: -Mode Fix|Reset  -ComPort COM5  -Adapter Ethernet  -FactoryReset  -SkipBackup  -Speed auto|fast|slow
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Fix-U6Plus.ps1" %*
echo.
pause
