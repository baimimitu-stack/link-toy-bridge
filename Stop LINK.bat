@echo off
rem LINK one-click stop: closes bridge, mediator, relay, keep-awake and Intiface Central.
cd /d "%~dp0"
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "link-launcher.ps1" stop
