@echo off
setlocal
cd /d "%~dp0"

echo === crunchybot launcher ===
echo cwd: %CD%

if not exist ".venv" (
    echo creating venv...
    python -m venv .venv
    if errorlevel 1 (
        echo FAILED: python -m venv
        pause
        exit /b 1
    )
)

call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo FAILED: activate venv
    pause
    exit /b 1
)

echo === installing requirements ===
python -m pip install --upgrade pip -q
pip install -q -r requirements.txt
if errorlevel 1 (
    echo FAILED: pip install
    pause
    exit /b 1
)

echo === starting bot ===
python bot.py
echo.
echo === bot exited with code %errorlevel% ===
pause
endlocal