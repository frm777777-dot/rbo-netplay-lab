@echo off
cd /d "%~dp0"
"%~dp0runtime\python\python.exe" -B "%~dp0tools\singleplayer.py"
if errorlevel 1 pause
