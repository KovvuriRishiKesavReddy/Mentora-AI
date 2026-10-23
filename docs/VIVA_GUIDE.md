# Viva / presentation guide

## 60-second story
MentoraAI is an emotionally adaptive tutor that answers **only from the student's own notes**. The original version forgot the conversation and could not reliably read notes. I found the root causes, fixed them, and added study tools — and I measure it with a question set built from the notes.

## Slide plan
1. Problem — a tutor that forgets and ignores your notes
2. Architecture (docs/ARCHITECTURE.md diagram)
3. The flaws and root causes (README table)
4. Fix 1 — conversation continuity: session persistence + follow-up resolution (demo: the 5-step sequence)
5. Fix 2 — reading the notes: heading-aware chunks, tables, section completion (demo: "all six types")
6. Fix 3 — fair assessment: answer-key MCQ, mastery formula, confusion as a separate signal
7. What is new: whole-notes summary, mind map, flashcards, revision plan with spaced repetition, adaptive quiz
8. Trust: cited sources you can open, "Why this answer", honest "not in your notes"
9. Results — `eval/REPORT.md` numbers (state the embedder and whether the model was used)
10. Limitations and next steps (docs/LIMITATIONS.md)

## Likely questions
* *How do you stop hallucination?* Retrieval with a similarity gate, "use only the text" prompts, "not in your notes" behaviour, no invented numbers, citations you can open.
* *How is mastery computed?* Weighted average of the last 10 graded answers with a neutral prior (README). Chat and mood never change it.
* *How is mood used?* Presentation only: tone, pace, challenge level. Never facts.
* *What would you do next?* Real login, OCR for scanned PDFs, calibrating thresholds with MiniLM, a larger evaluation set.
