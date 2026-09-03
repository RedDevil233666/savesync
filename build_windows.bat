@echo off
rem Build SaveSync.exe (Windows GUI app) - run this ONCE on a Windows machine
rem Requires: Python 3.8+ from python.org (check "Add python.exe to PATH" during install)
rem Put this file together with savesync.py and savesync_gui.py, then double-click.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo Install from https://www.python.org/downloads/ and check "Add python.exe to PATH".
    pause
    exit /b 1
)

echo [1/2] Installing PyInstaller ...
python -m pip install --upgrade pyinstaller

echo [2/2] Building SaveSync.exe ...
python -m PyInstaller --noconfirm --onefile --windowed --name SaveSync --add-data "savesync.py;." savesync_gui.py

if errorlevel 1 (
    echo [FAILED] Build error - check messages above.
    pause
    exit /b 1
)

echo.
echo ============================================
echo  Done! Single file app: dist\SaveSync.exe
echo  You can copy it anywhere, e.g. D:\tools\
echo ============================================
pause
