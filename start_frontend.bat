@echo off
title MentoraAI Frontend
color 0B
echo ============================================
echo   MentoraAI Frontend
echo   Open browser at: http://localhost:3000
echo ============================================
echo.
cd /d "%~dp0\frontend"
python -m http.server 3000
pause
