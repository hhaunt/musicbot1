@echo off
chcp 65001 >nul
cd /d "%~dp0"
where node >nul 2>nul
if errorlevel 1 (
    echo Нужен Node.js: скачайте его с https://nodejs.org и запустите этот файл снова.
    pause
    exit /b 1
)
node presence.js
pause
