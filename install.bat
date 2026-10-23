@echo off
title MentoraAI - Install Dependencies
color 0E
echo Installing MentoraAI dependencies (5-10 minutes on first run)...
cd /d "%~dp0"
if not exist .venv ( python -m venv .venv )
call .venv\Scripts\activate
pip install -r requirements.txt
if not exist .env ( copy .env.example .env >nul & echo. & echo Created .env - open it and paste your GROQ_API_KEY. )
echo.
echo Optional: install ffmpeg only if you want to upload WebM/MP3 audio files directly (the web UI sends WAV, which needs no ffmpeg).
echo Installation complete. Run start.bat to launch the backend.
pause
