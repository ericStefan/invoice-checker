@echo off
rem Start invoice checker GUI (ASCII only to avoid GBK issues)
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto run
echo Dependencies not installed. Run the install bat in this folder first.
pause
exit /b 1

:run
".venv\Scripts\python.exe" main.py
if errorlevel 1 pause
