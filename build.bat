@echo off
REM Playlist-Downloader Release Build Script (py2exe)
REM Requires: Python, py2exe, ffmpeg in PATH

echo Playlist-Downloader Release Builder
echo. 

cd /d "%~dp0"
pip install --upgrade pip setuptools py2exe

if not exist "build" mkdir build
if not exist "dist" mkdir dist

python setup.py py2exe

if errorlevel 1 (
    echo.
    echo Build FAILED!
    pause
    exit /b 1
)

echo.
echo Release built in ./dist/
pause
