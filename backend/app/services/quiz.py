"""Quiz generation and grading.

* Questions are generated from the student's notes and stored SERVER-SIDE (answers never travel to the browser
  before grading); the topic of every question is the topic the quiz was generated for.
* MCQs are graded objectively (chosen letter == stored correct letter). They are NOT run through embedding
  similarity — that was why correct MCQ answers could be scored < 0.6 and flagged weak.
* Short / Feynman answers: LLM grader (+ small similarity component). If the grader is unavailable the answer
  is NOT recorded, so an outage can never create a false "weak" verdict.
* One graded attempt per (quiz, question). Re-submitting returns the stored result instead of counting again.
"""
import json
import re
import uuid

import numpy as np

from app import db
from app.errors import ApiError
from app.services import embedding, gap_tracker, memory, rag
from app.services.llm import LLMError, call_llm

TYPE_INSTRUCTIONS = {
    "mcq": ("Generate {n} multiple-choice questions with 4 options (A/B/C/D). Format EACH as:\n"
            "Q: [question]\nA) [option]\nB) [option]\nC) [option]\nD) [option]\nANSWER: [letter]\nEXPLANATION: [why correct]\n---"),
    "short": ("Generate {n} short-answer questions. Format EACH as:\nQ: [question]\nMODEL ANSWER: [2-3 sentence ideal answer]\n---"),
    "feynman": ("Generate {n} Feynman-mode questions asking the student to explain a concept in their own words as if "
                "teaching a 12-year-old. Format EACH as:\nQ: [explain X as if...]\nKEY CONCEPTS: [comma-separated]\n---"),
}
PASS_SCORE = 0.6


def _parse_questions(raw: str, qtype: str) -> list:
    out = []
    for block in [b.strip() for b in raw.split("---") if b.strip()]:
        q = {}
        for line in [l.strip() for l in block.split("\n") if l.strip()]:
            if line.startswith("Q:"):
                q["question"] = line[2:].strip()
            elif line.startswith("MODEL ANSWER:"):
                q["model_answer"] = line[13:].strip()
            elif line.startswith("ANSWER:"):
                m = re.match(r"\(?([A-D])\b", line[7:].strip().upper())
                if m:
                    q["correct_answer"] = m.group(1)
            elif line.startswith("WRONG OPTIONS:"):
                notes = {}
                for m in re.finditer(r"\b([A-D])\s*[:\-)]\s*([^;]+)", line[14:]):
                    notes[m.group(1)] = m.group(2).strip()
                if notes:
                    q["option_notes"] = notes
            elif line.startswith("EXPLANATION:"):
                q["explanation"] = line[12:].strip()
            elif line.startswith("KEY CONCEPTS:"):
                q["key_concepts"] = [c.strip() for c in line[13:].split(",") if c.strip()]
            elif len(line) > 2 and line[0] in "ABCD" and line[1] == ")":
                q.setdefault("options", {})[line[0]] = line[2:].strip()
        if not q.get("question"):
            continue
        if qtype == "mcq":
            if len(q.get("options", {})) < 2 or q.get("correct_answer") not in q.get("options", {}):
                continue                                  # malformed / un-gradable: drop rather than mis-grade
        elif qtype == "short" and not q.get("model_answer"):
            continue
        elif qtype == "feynman" and not q.get("key_concepts"):
            continue
        out.append(q)
    return out


DIFFICULTY_GUIDE = {
    "easy": "direct recall of definitions and basic facts",
    "medium": "understanding, comparison and short reasoning",
    "hard": "application to scenarios, edge cases and subtle distinctions between similar ideas",
}


def pick_difficulty(owner: str, topic: str, requested: str = "auto") -> tuple:
    """auto: adapt to what the student has already shown on this topic (graded quiz answers only)."""
    if requested in DIFFICULTY_GUIDE:
        return requested, "chosen by you"
    snap = _topic_snapshot(owner, topic)
    if snap["status"] in ("weak", "needs_revision"):
        return "easy", "you are still building this topic, so we start with the basics"
    if snap["status"] == "mastered":
        return "hard", "you have mastered this topic, so the questions are harder"
    return "medium", "a standard level (not enough quiz history on this topic yet)"


def generate_quiz(owner: str, session_id: str, topic: str, question_type: str = "mcq", num_questions: int = 3,
                  difficulty: str = "auto") -> dict:
    memory.get_session(owner, session_id)                      # ownership check
    difficulty, difficulty_reason = pick_difficulty(owner, topic, difficulty)
    qtype = question_type if question_type in TYPE_INSTRUCTIONS else "short"
    ev = rag.gather(owner, topic, broad=True)
    if ev.grounding == "none":
        raise ApiError(404, f"No content found for topic '{topic}' in your uploaded notes. Upload relevant notes first.", "no_content")
    instructions = TYPE_INSTRUCTIONS[qtype].replace("{n}", str(num_questions))
    prompt = ("You are a quiz generator. Use ONLY the content below.\n\n" f"{ev.context}\n\nTOPIC: {topic}\nDIFFICULTY: {difficulty} ({DIFFICULTY_GUIDE[difficulty]})\n\n{instructions}\n\n"
              "Generate exactly the requested number of questions separated by ---")
    try:
        raw = call_llm("You are a quiz generator. Follow the format instructions exactly.",
                       [{"role": "user", "content": prompt}], 1800)
    except LLMError as e:
        raise ApiError(502 if e.kind != "no_key" else 503, e.message, f"llm_{e.kind}")
    questions = _parse_questions(raw, qtype)[:num_questions]
    if not questions:
        raise ApiError(502, "Could not generate valid questions this time. Please try again.", "quiz_generation_failed")
    quiz_id = str(uuid.uuid4())
    with db.connect() as c:
        c.execute("INSERT INTO quizzes(id,owner,session_id,topic,topic_key,question_type,questions_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
                  (quiz_id, owner, session_id, topic, gap_tracker.topic_key(topic), qtype, db.j(questions), db.now()))
    public = []
    for i, q in enumerate(questions):
        item = {"index": i, "question": q["question"]}
        if qtype == "mcq":
            item["options"] = q["options"]
        public.append(item)
    return {"quiz_id": quiz_id, "topic": topic, "question_type": qtype, "questions": public,
            "difficulty": difficulty, "difficulty_reason": difficulty_reason,
            "sources_used": sorted({s["filename"] for s in ev.sources})}


def _load_quiz(owner: str, quiz_id: str) -> dict:
    with db.connect() as c:
        r = c.execute("SELECT * FROM quizzes WHERE id=? AND owner=?", (quiz_id, owner)).fetchone()
    if not r:
        raise ApiError(404, "Quiz not found.", "quiz_not_found")
    return {"id": r["id"], "session_id": r["session_id"], "topic": r["topic"], "type": r["question_type"],
            "questions": db.unj(r["questions_json"], [])}


def _topic_snapshot(owner: str, topic: str) -> dict:
    key = gap_tracker.topic_key(topic)
    for t in gap_tracker.get_topic_stats(owner):
        if t["key"] == key:
            return {"status": t["status"], "mastery": t["mastery"], "attempts": t["attempts"]}
    return {"status": "not_assessed", "mastery": None, "attempts": 0}


def _shape(stored: dict, owner: str, topic: str, already: bool) -> dict:
    r = dict(stored)
    r["already_graded"] = already
    r["topic_status"] = _topic_snapshot(owner, topic)
    return r


def grade_answer(owner: str, quiz_id: str, question_idx: int, student_answer: str) -> dict:
    quiz = _load_quiz(owner, quiz_id)
    if not 0 <= question_idx < len(quiz["questions"]):
        raise ApiError(400, "Invalid question index.", "bad_question")
    prior = gap_tracker.get_attempt(quiz_id, question_idx)
    if prior:                                                  # repeated submission: do not count twice
        return _shape(db.unj(prior["result_json"], {}), owner, quiz["topic"], True)

    q, qtype, topic = quiz["questions"][question_idx], quiz["type"], quiz["topic"]
    answer = student_answer.strip()
    if qtype == "mcq":
        letter = answer[:1].upper()
        if letter not in q.get("options", {}):
            raise ApiError(400, "Choose one of the listed options.", "bad_option")
        correct = letter == q["correct_answer"]
        score = 1.0 if correct else 0.0
        result = {"score": score, "score_percent": f"{score * 100:.1f}%", "correct": correct,
                  "correct_answer": q["correct_answer"], "explanation": q.get("explanation", ""),
                  "option_notes": {k: v for k, v in q.get("option_notes", {}).items() if k != q["correct_answer"]},
                  "feedback": q.get("explanation", ""), "similarity_score": score, "completeness_score": score,
                  "clarity_score": score, "concepts_present": [], "concepts_missing": [], "misconceptions": [],
                  "gap_analysis": {"has_gaps": not correct, "has_misconceptions": False}, "graded_by": "answer_key"}
    else:
        model_answer = q.get("model_answer") or ", ".join(q.get("key_concepts", []))
        result = _grade_open_answer(q["question"], model_answer, answer)
    stored = gap_tracker.record_attempt(owner, quiz["session_id"], quiz_id, question_idx, topic, q["question"], answer,
                                        result["correct"], result["score"], result["graded_by"], result)
    if not stored:                                             # lost a race with an identical concurrent request
        prior = gap_tracker.get_attempt(quiz_id, question_idx)
        return _shape(db.unj(prior["result_json"], {}), owner, topic, True)
    return _shape(result, owner, topic, False)


def _grade_open_answer(question: str, model_answer: str, student_answer: str) -> dict:
    prompt = (f"You are a fair AI grader.\n\nQUESTION: {question}\nMODEL ANSWER / KEY CONCEPTS: {model_answer}\n"
              f"STUDENT ANSWER: {student_answer}\n\nRespond ONLY with JSON (no markdown):\n"
              '{"concepts_present": ["..."], "concepts_missing": ["..."], "misconceptions": [], '
              '"clarity_score": 0.8, "completeness_score": 0.7, "feedback": "2-3 sentence specific feedback"}')
    try:
        raw = call_llm("You are a precise grader. Reply with valid JSON only.", [{"role": "user", "content": prompt}], 500)
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0))
        completeness = float(min(max(data["completeness_score"], 0.0), 1.0))
        clarity = float(min(max(data["clarity_score"], 0.0), 1.0))
    except Exception as e:
        # Do not record anything: a grader outage must not look like a wrong answer.
        raise ApiError(503, "The grader is unavailable right now, so this answer was not scored. Please resubmit.", "grader_unavailable")
    emb = embedding.get_embedder()
    a, b = emb.encode([model_answer])[0], emb.encode([student_answer])[0]
    cos = float(max(0.0, np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9)))
    score = round(min(max(0.6 * completeness + 0.2 * clarity + 0.2 * cos, 0.0), 1.0), 3)
    missing = [str(x) for x in data.get("concepts_missing", [])]
    wrong = [str(x) for x in data.get("misconceptions", [])]
    return {"score": score, "score_percent": f"{score * 100:.1f}%", "correct": score >= PASS_SCORE,
            "correct_answer": None, "explanation": "", "similarity_score": round(cos, 3),
            "completeness_score": round(completeness, 3), "clarity_score": round(clarity, 3),
            "concepts_present": [str(x) for x in data.get("concepts_present", [])], "concepts_missing": missing,
            "misconceptions": wrong, "feedback": str(data.get("feedback", "")),
            "gap_analysis": {"has_gaps": bool(missing), "has_misconceptions": bool(wrong)}, "graded_by": "llm"}
