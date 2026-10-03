@echo off
rem ASCII only: cmd misreads UTF-8 letters in .bat files and runs garbage commands.
cd /d "%~dp0"
set "NODE=node"
where node >nul 2>nul
if not errorlevel 1 goto run
set "NODE=%ProgramFiles%\nodejs\node.exe"
if exist "%NODE%" goto run
echo Node.js not found. Install it from https://nodejs.org and run this file again.
pause
exit /b 1
:run
"%NODE%" presence.js
pause
