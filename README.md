# MentoraAI — repaired build (v3) + study tools

## What it does
Answers **only from your own notes** (PDF / DOCX / TXT), remembers the conversation, adapts its teaching style to your mood, quizzes you, and tells you what to revise.

**New in this build** (on top of the repairs below): whole-notes **summary** (chapter by chapter + overview + key terms, downloadable) · **mind map** of chapters/sections · **flashcards** · **revision plan** with spaced repetition · **adaptive quiz difficulty** and "why each wrong option is wrong" · **click-to-open source citations** · **"Why this answer"** panel and visible **teaching style** · formatted answers (tables, lists) · **read aloud** · warm light theme (orange, red and gold) with a dark option · phone layout · upload progress bar · an **evaluation harness** (`backend/eval`) · `run_all.bat` one-click start. See `docs/` for architecture, before/after, limitations, a step-by-step test guide and a viva guide.

## Run it
```
install.bat          (first time: venv + requirements; creates .env from .env.example)
# open .env and paste your GROQ_API_KEY   <- the zip contains NO key
run_all.bat          (starts both and opens the app)      -- macOS/Linux: ./run_all.sh
# or separately:
start.bat            (backend  http://127.0.0.1:8000)
start_frontend.bat   (UI       http://localhost:3000)
```
Tests (no extra packages needed; `pip install fastapi httpx` additionally enables `tests/test_api.py`):
```
cd backend
python -m unittest discover -s tests -t .
```
If chat says "Groq model unavailable": `cd backend && python scripts/check_groq.py` lists the models your key can use (the backend also auto-discovers one).
Evaluation (builds a report from 41 questions about the notes, 4 follow-up sequences and 10 out-of-scope questions): `python eval/run_eval.py` (retrieval, no key) · `python eval/run_eval.py --mode answers` (asks the real model; needs the key) → `eval/REPORT.md`.
Optional: `python scripts/calibrate_threshold.py --owner <id> --relevant "..." --irrelevant "..."` tunes retrieval thresholds on your notes with the real embedder.
Coming from the old version? `python -m app.tools.migrate_legacy --owner <your mentora.clientId> --reindex` (backs up, never deletes).

## Root causes found and fixed (per the flaws PDF)
| # | Symptom | Confirmed root cause | Fix |
|---|---|---|---|
| 1,2 | Follow-ups unrelated / no continuity | Frontend created a **new session on every load**; retrieval used only the literal follow-up text ("What are its types?") so it fetched an unrelated section | Session id persisted; history reloaded from backend; follow-ups are resolved to a standalone question (LLM rewrite, heuristic fallback anchored to the active topic) before retrieval; explicit topic changes respected |
| 3 | "All types" explains only some | Top-5 chunks only; **DOCX tables ignored**; PDF pages cut at 5000 chars; chunk "chapter" picked from the page, not the chunk; **similarity formula `1 - d/2`** made the 0.30 gate pass for nearly everything | Heading-aware chunking (incl. tables), true cosine `1 - d`, per-embedder thresholds, broad-request detection + **section completion** (whole section retrieved), char budget with truncation notice, owner-scoped store, dedupe policy |
| 4 | Correct quiz answers shown as weak | MCQs graded by embedding similarity of "B) text" strings + LLM completeness; **any chat message containing "what is/how does" was scored 0.15 and flagged weak**; topic guessed from filenames / first words | Server-side answer key for MCQ; mastery from graded quiz answers only; confusion tracked as a separate signal; topic = the quiz topic |
| 5 | History/analytics vanish on refresh | New session each load; analytics keyed by that session; whole-JSON-file storage | SQLite persistence, `/sessions` APIs, restore on load, owner-wide mastery |
| 6 | Audio "Format not recognised" | Browser records WebM/Opus; soundfile cannot read WebM; ffmpeg silently missing → fallback to soundfile; temp files leaked; also the UI started a *second* speech recognition after recording | Browser converts to 16 kHz WAV; magic-byte sniffing; ffmpeg only for non-WAV with clear 503; size/duration limits; temp dirs always removed; real STT option |
| 7 | Repetitive / over-simplified answers | Prompt forced "which just means…" after every term, a mandatory fixed fallback opener, "surprising fact" openers, and "answer ONLY from notes" contradicting general-knowledge fallback | Rewritten prompt: fixed grounding/coverage rules + mood-only presentation layer |
| 8 | Intent misclassified | `topic_words = first 6 words`; topic from filename; "worst-case"/"move on" triggered frustrated/bored | Real topic extraction; whole-word mood keywords; no filename topics |
| – | Also found | `.env`/`start.bat` contained an API key; CORS `*`; `--host 0.0.0.0`; unsanitised upload filenames; no size limits; UI XSS via filename in `onclick`; dead models (mixtral/gemma2) in list; **styles.css contained a second, unrelated light theme that made text unreadable** | All fixed (see below) |

## Mastery policy (quiz evidence only)
`mastery = (Σ wᵢ·scoreᵢ + 0.5) / (Σ wᵢ + 1)` over the last 10 graded answers, wᵢ = 1..k (newer counts more). 0 answers → *not assessed*; 1 → *learning*; ≥2 and ≥0.75 → *mastered*; ≥0.40 → *needs revision*; <0.40 → *weak* (≥3 answers) or *needs revision* (2 answers). Chat questions, mood and "explain this" requests never change it. Explicit "I don't understand X" is stored separately; 3 in 14 days flags the topic "asked for help repeatedly" without touching mastery. One graded attempt per question; resubmits return the stored result.

## Persistence, isolation, security
* SQLite `data/mentora.db` (stdlib) + ChromaDB `data/chroma_db` + `uploads/<owner>/`. If the vector DB is lost or the embedder changes, documents are re-indexed from stored files at startup.
* No login exists, so each browser gets a random 128-bit id (`X-Client-Id`); sessions, documents, quizzes and mastery are bound to it and foreign ids return 404. **This separates browsers, it is not authentication** — add real auth before exposing the server publicly.
* Uploads: filename sanitised, content-sniffed (PDF/DOCX/TXT), 25 MB cap, streamed. CORS limited to `CORS_ORIGINS`. Rate limits on chat/upload/audio. Errors return `{"detail","code"}`. Secrets only from environment; logs never contain chat or audio content.
* Migration/backup: legacy JSON is copied, never deleted (`app/tools/migrate_legacy.py`). Old `gaps.json` is intentionally not imported (it was produced by the faulty logic).

## Voice
Feature extraction (pitch/pace/pauses) only feeds mood detection — it is **not** transcription. Speech-to-text: `STT_PROVIDER=browser` (default; Chrome/Edge Web Speech API, runs during recording, text lands in the box for review), `groq` (server-side Whisper through your existing Groq key — audio is sent to Groq; free-tier limits apply), or `none`.

## New API endpoints
`GET /outline/{doc_id}` (chapter/section tree) · `GET /passage?doc_id&heading` (open a cited section) · `POST /summary` / `GET /summary/{doc_id}` (whole-notes summary, cached) · `POST /flashcards` · `GET /revision-plan` · `/quiz/generate` now takes `difficulty` (auto/easy/medium/hard) · `/chat` returns `teaching_strategy`.

## Revision plan (spaced repetition)
Review interval after the last graded answer: weak 1 day · needs revision 2 · learning 3 · mastered 7 (14 after 4+ answers) · repeated confusion 1. A topic is *due* when that time has passed; the plan lists due topics first (lowest mastery first), then upcoming ones, each with an action and a link to the notes section. Quiz difficulty "auto": weak/needs-revision → easy, mastered → hard, otherwise medium.

## Test report (what was actually run)
Run in a sandbox without FastAPI/ChromaDB/sentence-transformers/Groq access (package index and Hugging Face are blocked there).
| Area | Result |
|---|---|
| 100 automated tests (RAG, conversation, quiz/mastery, audio, security, migration, study tools, revision plan, adaptive quiz, evaluation dataset) | **PASS** (95 run + 5 API tests skipped: FastAPI missing) |
| Evaluation harness, offline fallback embedder, real notes: 41 questions → expected section retrieved **40/41**, best source correct 34/41; follow-up turns 11/12; out-of-scope 10/10 not strongly grounded | **PASS as a harness run** — the two misses ("What is a process?", "How can it be prevented?") are limits of the lexical fallback; **re-run with MiniLM** (`eval/REPORT.md` is overwritten) |
| Whole-notes summary covers every section (every section heading is sent to the model); failure paths: model down at start → clear error and nothing cached; failure midway → extract fallback and not cached | **PASS** (scripted model) |
| Browser (Chromium) against the real service layer: formatted answers with table/list and no script injection, Why panel, source → passage popup, theme toggle (persists), upload progress, summary + download + cached after reload, flashcards flip, mind map (11 chapters / 41 sections) with popups, quiz difficulty + wrong-option notes, revision plan, phone layout without sideways scroll; hard refresh keeps the session | **PASS** |
| Earlier checks: six OS types from the real notes, real WebM/Opus via ffmpeg and audio edge cases, refresh/New Chat/foreign-client isolation | **PASS** |
| FastAPI endpoints (`main.py`), CORS preflight, upload endpoint — including the new routes | **NOT RUN** (compiled only; the browser check used a stdlib harness with the same routes; `tests/test_api.py` is ready) |
| Real Groq answers: gpt-oss behaviour, summary/flashcard quality, follow-up and "I don't understand" replies, mood tone | **NOT RUN** — only the prompts and context sent to the model were verified. Run `eval/run_eval.py --mode answers` |
| Real MiniLM retrieval and threshold calibration, ChromaDB persistence | **NOT RUN** — defaults (weak 0.25 / strong 0.40 cosine) are untuned |
| Microphone, MediaRecorder → WAV, Web Speech, Groq Whisper, read-aloud voices | **NOT RUN** (needs a real browser/mic/speakers) |
| PyMuPDF PDF path; deployment restart | **NOT RUN** |
