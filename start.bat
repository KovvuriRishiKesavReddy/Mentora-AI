@echo off
title MentoraAI Backend
color 0A
echo ============================================
echo   MentoraAI - Emotionally Intelligent Tutor
echo ============================================
cd /d "%~dp0"
if not exist .env (
    copy .env.example .env >nul
    echo Created .env from .env.example. Paste your free Groq key after GROQ_API_KEY= and save.
    notepad .env
    pause
)
call .venv\Scripts\activate
echo Starting backend on http://127.0.0.1:8000  (the key is read from .env)
cd backend
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
pause
