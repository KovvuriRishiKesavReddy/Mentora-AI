# Before and after

"Before" is what code review of the original project showed; "after" is what the automated tests and the browser check now demonstrate. Run `python eval/run_eval.py` to reproduce the retrieval numbers yourself.

| Situation | Before | After |
|---|---|---|
| Refresh the page | New session; chat, mood and analytics gone | Same session restored from the server; Previous Chats list; New Chat keeps the old one |
| "What are its types?" | Searched the literal words, returned an unrelated chapter | Rewritten to "What are the types of process scheduling?" and answered from the right chapter |
| "I don't understand it." | Treated as a new question about "it" | Re-explains the active topic simply (short, plain words, one analogy) |
| "Explain all six types of OS" | Top-5 chunks, tables ignored, some types missing | Whole section retrieved; all six present (tested on the real notes) |
| Correct quiz answer | Could be scored weak (embedding similarity of "B) text") | MCQ graded against the answer key; mastery only from graded answers |
| A plain question like "what is X" | Counted as a knowledge gap | Never affects mastery |
| "worst case time complexity" | Tagged Frustrated | Neutral (whole-word keywords; near-tie → neutral) |
| Answers | Repetitive, "which just means…" after every term | Layered prompt, no filler, no invented numbers |
| Voice | "Format not recognised" for WebM | Browser converts to WAV; clear errors |
| Secrets | API key in `.env`/`start.bat` | Key only from your own `.env`; none in the zip |
| Whole-notes view | None | Chapter-by-chapter summary + overview, mind map, flashcards |
| What to study next | None | Spaced-repetition revision plan |
