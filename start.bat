@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv (
    python -m venv .venv
    call .venv\Scripts\activate.bat
    python -m pip install --upgrade pip
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate.bat
)
if not exist .env copy .env.example .env >nul
python bot.py
pause
