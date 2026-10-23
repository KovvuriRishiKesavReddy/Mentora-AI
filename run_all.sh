#!/usr/bin/env bash
# macOS / Linux: start backend (8000) and frontend (3000). Ctrl+C stops both.
set -e
cd "$(dirname "$0")"
[ -f .env ] || { cp .env.example .env; echo "Created .env - paste your GROQ_API_KEY into it, then run this again."; exit 1; }
[ -d .venv ] && source .venv/bin/activate
set -a; source .env; set +a
(cd frontend && python3 -m http.server 3000) &
FE=$!
trap "kill $FE 2>/dev/null" EXIT
cd backend && uvicorn app.main:app --host 127.0.0.1 --port 8000
