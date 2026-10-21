@echo off
REM Builds output\BPM_to_OSC_XL.exe
REM Needs Python 3.13.
REM The build environment lives outside this folder, so Dropbox doesn't sync it.
cd /d "%~dp0"
set BUILD_DIR=%LOCALAPPDATA%\BPM_to_OSC_XL-build
if not exist "%BUILD_DIR%\venv" (
    python -m venv "%BUILD_DIR%\venv" || goto :error
)
"%BUILD_DIR%\venv\Scripts\python" -m pip install -r requirements.txt || goto :error
"%BUILD_DIR%\venv\Scripts\pyinstaller" --noconfirm --onefile --windowed --name BPM_to_OSC_XL --icon "%~dp0BPM_to_OSC_XL.ico" ^
    --add-data "%~dp0BPM_to_OSC_XL.ico;." ^
    --distpath output --workpath "%BUILD_DIR%\build" --specpath "%BUILD_DIR%\build" main_gui.py || goto :error
echo.
echo Done: output\BPM_to_OSC_XL.exe
if not defined NOPAUSE pause
exit /b 0
:error
echo Build failed.
if not defined NOPAUSE pause
exit /b 1
