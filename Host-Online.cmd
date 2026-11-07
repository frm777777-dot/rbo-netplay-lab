@echo off
cd /d "%~dp0"
"%~dp0runtime\python\python.exe" -B "%~dp0tools\netplay_launcher.py" host
pause
