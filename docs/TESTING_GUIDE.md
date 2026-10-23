# Test it step by step (about 15 minutes)

Stop at the first failure and note the exact message.

1. **Automated tests** — from `backend`: `python -m unittest discover -s tests -t .` → OK. (`pip install fastapi httpx` also enables the API tests.)
2. **Model check** — `python scripts/check_groq.py` → both gpt-oss models work.
3. **Start** — `run_all.bat` (or `start.bat` + `start_frontend.bat`), open http://localhost:3000. No "Backend not reachable" banner.
4. **Upload** your notes → chapters listed. Upload the same file again → "already indexed".
5. **Notes understanding** — "What are the types of operating systems?" lists all six; ask about a late detail; ask something not in the notes → it says so.
6. **Continuity** — "Explain process scheduling" → "What are its advantages?" → "Give an example" → "I don't understand it." (short, simple, about *process scheduling*) → "Now explain deadlock" (switches topic).
7. **Persistence** — F5 keeps the chat; New Chat then reopen the old one under Previous Chats; restart the backend and check again.
8. **Mood / why panel** — "I give up, this is terrible" changes tone and shows a Teaching style; open **Why this answer**; click **open section** on a source.
9. **Quiz** — generate (difficulty Auto), answer right and wrong, read why each wrong option is wrong; answering the same question twice does not count twice.
10. **Plan** — open Plan: topics you got wrong appear with an action and a button to open the notes section.
11. **Study** — Summarise the whole notes (once; then reload and it is still there), download the .md, make flashcards, open the mind map and click a section.
12. **Extras** — 🔊 Read aloud, ☀️/🌙 theme, resize the window to phone width (☰ menu).
13. **Evaluation** — `python eval/run_eval.py` (retrieval, no key) and `python eval/run_eval.py --mode answers` (real model). Open `eval/REPORT.md`.
14. **Voice** — mic button; Chrome/Edge; allow the microphone.
