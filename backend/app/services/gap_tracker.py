"""Quiz-based mastery tracking (replaces the old chat-based "gap" tracker).

SIGNALS ARE KEPT SEPARATE
  * quiz correctness  -> quiz_attempts.score / correct       (the ONLY input to mastery)
  * explicit confusion -> confusion_signals                   (shown next to mastery, never changes it)
  * mood, "what is X" questions, asking for explanations      -> NOT recorded as evidence of a gap at all

MASTERY POLICY (per owner + topic, graded quiz attempts only)
  mastery = (sum(w_i * score_i) + 0.5) / (sum(w_i) + 1)  over the last 10 attempts, w_i = 1..k (newer counts more);
  the "+0.5 / +1" is a neutral prior, so a single answer can never decide the verdict.
      attempts == 0                     -> not_assessed
      attempts == 1                     -> learning            (not enough evidence either way)
      attempts >= 2 and mastery >= 0.75 -> mastered
      attempts >= 2 and mastery >= 0.40 -> needs_revision
      attempts >= 3 and mastery <  0.40 -> weak
      attempts == 2 and mastery <  0.40 -> needs_revision
  A correct answer always raises mastery; a wrong one lowers it.
  Confusion: 3+ explicit "I don't understand" statements on a topic in 14 days, with no mastered status, sets
  needs_attention=True (listed for revision with reason 'repeated confusion'); quiz mastery itself is untouched.
"""
import re
from datetime import datetime, timedelta, timezone

from app import db
from app.services.query import is_confusion_statement   # re-exported for callers

WINDOW = 10
CONFUSION_WINDOW_DAYS = 14
CONFUSION_FLAG_AT = 3
REVIEW_DAYS = {"weak": 1, "needs_revision": 2, "learning": 3, "mastered": 14}


def topic_key(topic: str) -> str:
    s = re.sub(r"[^\w\s]", " ", (topic or "").lower())
    return re.sub(r"\s+", " ", s).strip()[:80]


def topic_display(topic: str) -> str:
    t = re.sub(r"\s+", " ", (topic or "").strip())[:80]
    return t if any(ch.isupper() for ch in t) else t.title()


def compute_mastery(scores: list) -> dict:
    """scores: chronological list of floats in [0,1]."""
    s = scores[-WINDOW:]
    n_total = len(scores)
    if not s:
        return {"status": "not_assessed", "mastery": None, "attempts": 0}
    w = list(range(1, len(s) + 1))
    mastery = (sum(wi * si for wi, si in zip(w, s)) + 0.5) / (sum(w) + 1)
    if n_total == 1:
        status = "learning"
    elif mastery >= 0.75:
        status = "mastered"
    elif mastery >= 0.40:
        status = "needs_revision"
    else:
        status = "weak" if n_total >= 3 else "needs_revision"
    return {"status": status, "mastery": round(mastery, 3), "attempts": n_total}


def record_attempt(owner, session_id, quiz_id, idx, topic, question, student_answer, correct, score, graded_by, result) -> bool:
    """Insert one graded attempt. Returns False (and changes nothing) if this quiz question was already graded."""
    with db.connect() as c:
        cur = c.execute(
            "INSERT OR IGNORE INTO quiz_attempts(owner,session_id,quiz_id,question_idx,topic_key,topic_display,question,"
            "student_answer,correct,score,graded_by,result_json,ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (owner, session_id, quiz_id, idx, topic_key(topic), topic_display(topic), question[:500],
             student_answer[:4000], None if correct is None else int(correct), float(score), graded_by, db.j(result), db.now()))
        return cur.rowcount == 1


def get_attempt(quiz_id: str, idx: int):
    with db.connect() as c:
        return c.execute("SELECT * FROM quiz_attempts WHERE quiz_id=? AND question_idx=?", (quiz_id, idx)).fetchone()


def record_confusion(owner, session_id, topic) -> None:
    key = topic_key(topic)
    if not key:
        return
    with db.connect() as c:
        c.execute("INSERT INTO confusion_signals(owner,session_id,topic_key,topic_display,ts) VALUES (?,?,?,?,?)",
                  (owner, session_id, key, topic_display(topic), db.now()))


def get_topic_stats(owner: str) -> list:
    with db.connect() as c:
        att = c.execute("SELECT topic_key, topic_display, score, correct, ts FROM quiz_attempts WHERE owner=? ORDER BY id",
                        (owner,)).fetchall()
        since = (datetime.now(timezone.utc) - timedelta(days=CONFUSION_WINDOW_DAYS)).isoformat(timespec="seconds")
        conf = c.execute("SELECT topic_key, topic_display, COUNT(*) n FROM confusion_signals WHERE owner=? AND ts>=? "
                         "GROUP BY topic_key", (owner, since)).fetchall()
    topics = {}
    for r in att:
        t = topics.setdefault(r["topic_key"], {"topic": r["topic_display"], "key": r["topic_key"], "scores": [],
                                               "correct": 0, "incorrect": 0, "last": None})
        t["scores"].append(r["score"])
        if r["correct"] == 1 or (r["correct"] is None and r["score"] >= 0.6):
            t["correct"] += 1
        else:
            t["incorrect"] += 1
        t["last"] = r["ts"]
    conf_map = {r["topic_key"]: (r["topic_display"], r["n"]) for r in conf}
    for k, (disp, n) in conf_map.items():
        topics.setdefault(k, {"topic": disp, "key": k, "scores": [], "correct": 0, "incorrect": 0, "last": None})
    out = []
    for k, t in topics.items():
        m = compute_mastery(t["scores"])
        n_conf = conf_map.get(k, ("", 0))[1]
        out.append({"topic": t["topic"], "key": k, "status": m["status"], "mastery": m["mastery"],
                    "avg_score": m["mastery"] if m["mastery"] is not None else 0.0,
                    "attempts": m["attempts"], "correct": t["correct"], "incorrect": t["incorrect"],
                    "scores": t["scores"], "confusion_signals": n_conf,
                    "needs_attention": n_conf >= CONFUSION_FLAG_AT and m["status"] != "mastered",
                    "is_weak": m["status"] in ("weak", "needs_revision"), "last_attempt": t["last"]})
    return sorted(out, key=lambda x: (x["mastery"] if x["mastery"] is not None else 2))


def get_weak_topics(owner: str) -> list:
    """Topics needing work: weak / needs_revision from quiz evidence, plus repeated-confusion flags."""
    res = []
    for t in get_topic_stats(owner):
        if t["status"] in ("weak", "needs_revision"):
            res.append({**t, "reason": "quiz results"})
        elif t["needs_attention"]:
            res.append({**t, "reason": "repeated confusion"})
    return res


def get_next_revision_topic(owner: str):
    now = datetime.now(timezone.utc)
    due = []
    for t in get_weak_topics(owner):
        days = REVIEW_DAYS.get(t["status"], 1)
        try:
            last = datetime.fromisoformat(t["last_attempt"]) if t["last_attempt"] else now - timedelta(days=days + 1)
        except Exception:
            continue
        if last + timedelta(days=days) <= now:
            due.append({"topic": t["topic"], "avg_score": t["avg_score"], "status": t["status"],
                        "overdue_hours": round((now - (last + timedelta(days=days))).total_seconds() / 3600, 1)})
    return sorted(due, key=lambda x: x["overdue_hours"], reverse=True)[0] if due else None
