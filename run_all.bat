@echo off
rem Starts the backend and the frontend in two windows and opens the app.
cd /d "%~dp0"
start "MentoraAI Backend" cmd /k call start.bat
timeout /t 5 >nul
start "MentoraAI Frontend" cmd /k call start_frontend.bat
timeout /t 2 >nul
start http://localhost:3000
