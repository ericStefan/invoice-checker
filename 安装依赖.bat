@echo off
rem Install dependencies for invoice checker (ASCII only to avoid GBK issues)
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto deps

echo [1/3] Creating local venv .venv ...
where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
if not exist ".venv\Scripts\python.exe" (
    echo ERROR: no usable Python found. Install Python 3.10+ first.
    pause
    exit /b 1
)

:deps
echo [2/3] Installing packages from requirements.txt ...
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 (
    echo ERROR: dependency installation failed. See messages above.
    pause
    exit /b 1
)

echo [3/3] Installing Chromium for Playwright ...
".venv\Scripts\python.exe" -m playwright install chromium

echo.
echo All dependencies ready. Double-click the start bat to run the tool.
pause
