@echo off
rem LINK installer: double-click this. It runs install.ps1 and keeps the window open at the end.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "install.ps1"
echo.
pause
