@echo off
cd /d "%~dp0"

if not exist venv (
    echo Creating virtual environment...
    py -m venv venv
)

call venv\Scripts\activate.bat

echo Installing dependencies...
py -m pip install --upgrade pip
py -m pip install -r requirements.txt
py -m pip install pyinstaller

echo Building AppLauncher...
py -m PyInstaller AppLauncher.spec --clean --noconfirm

if errorlevel 1 (
    echo.
    echo Build failed.
    pause
    exit /b 1
)

echo.
echo Build completed successfully.
echo Output: dist\AppLauncher.exe
pause
