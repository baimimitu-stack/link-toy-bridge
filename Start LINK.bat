@echo off
rem LINK one-click start: everything runs hidden in the background, then the handheld window opens.
cd /d "%~dp0"
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "link-launcher.ps1" start
