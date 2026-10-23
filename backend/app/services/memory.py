"""Sessions and chat history, persisted in SQLite and bound to an owner.

Raw messages are never rewritten or deleted by summarisation: older turns are summarised into
sessions.summary and simply left out of the live context window."""
import uuid
from typing import Optional

from app import config, db
from app.errors import ApiError

MOOD_KEYS = ["frustrated", "confused", "confident", "bored", "neutral"]


def _row_to_session(r) -> dict:
    return {"id": r["id"], "title": r["title"], "created_at": r["created_at"], "updated_at": r["updated_at"],
            "active_topic": r["active_topic"], "summary": r["summary"], "summarized_upto": r["summarized_upto"]}


def create_session(owner: str) -> str:
    sid = str(uuid.uuid4())                       # 122 random bits: not guessable
    t = db.now()
    with db.connect() as c:
        c.execute("INSERT INTO sessions(id,owner,title,created_at,updated_at) VALUES (?,?,?,?,?)", (sid, owner, "", t, t))
    return sid


def get_session(owner: str, session_id: str) -> dict:
    """Owner-checked lookup. A session belonging to someone else is reported exactly like a missing one."""
    with db.connect() as c:
        r = c.execute("SELECT * FROM sessions WHERE id=? AND owner=?", (session_id, owner)).fetchone()
    if not r:
        raise ApiError(404, "Session not found.", "session_not_found")
    return _row_to_session(r)


def list_sessions(owner: str, limit: int = 50) -> list:
    with db.connect() as c:
        rows = c.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) AS n FROM sessions s "
            "WHERE s.owner=? ORDER BY s.updated_at DESC LIMIT ?", (owner, limit)).fetchall()
    return [{"id": r["id"], "title": r["title"] or "New chat", "updated_at": r["updated_at"],
             "created_at": r["created_at"], "messages": r["n"]} for r in rows if r["n"] > 0]


def delete_session(owner: str, session_id: str) -> bool:
    with db.connect() as c:
        # Deleting a chat removes its messages only; quiz attempts / mastery / confusion signals belong to the
        # learner's progress and are kept.
        cur = c.execute("DELETE FROM sessions WHERE id=? AND owner=?", (session_id, owner))
    return bool(cur.rowcount)


def get_messages(owner: str, session_id: str) -> list:
    get_session(owner, session_id)
    with db.connect() as c:
        rows = c.execute("SELECT * FROM messages WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
    return [{"id": r["id"], "role": r["role"], "content": r["content"], "timestamp": r["ts"],
             "mood_label": r["mood_label"], "tone_profile": db.unj(r["tone_json"]),
             "sources": db.unj(r["sources_json"], []), "grounding": r["grounding"]} for r in rows]


def add_exchange(session_id: str, user_text: str, assistant_text: str, mood_label: str, tone: dict,
                 sources: list, grounding: str, active_topic: Optional[str] = None) -> None:
    """Persist the user message and the assistant answer in ONE transaction (so a failed turn leaves no
    half-written history), and bump the session."""
    t = db.now()
    with db.connect() as c:
        c.execute("INSERT INTO messages(session_id,role,content,ts,mood_label,tone_json) VALUES (?,?,?,?,?,?)",
                  (session_id, "user", user_text, t, mood_label, db.j(tone)))
        c.execute("INSERT INTO messages(session_id,role,content,ts,mood_label,sources_json,grounding) VALUES (?,?,?,?,?,?,?)",
                  (session_id, "assistant", assistant_text, db.now(), mood_label, db.j(sources), grounding))
        row = c.execute("SELECT title FROM sessions WHERE id=?", (session_id,)).fetchone()
        title = row["title"] or " ".join(user_text.split())[:60]
        if active_topic is None:
            c.execute("UPDATE sessions SET title=?, updated_at=? WHERE id=?", (title, db.now(), session_id))
        else:
            c.execute("UPDATE sessions SET title=?, updated_at=?, active_topic=? WHERE id=?",
                      (title, db.now(), active_topic, session_id))


def get_context(session: dict) -> tuple:
    """-> (history_messages, summary). History = the most recent turns, chronological, bounded by count and
    characters, starting with a user turn and strictly alternating. Called BEFORE the current message is saved,
    so the current message can never appear twice."""
    with db.connect() as c:
        rows = c.execute("SELECT id, role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT ?",
                         (session["id"], config.MEMORY_CONTEXT_WINDOW)).fetchall()
    rows = list(reversed(rows))
    msgs, total = [], 0
    for r in reversed(rows):                      # newest first so the char budget keeps the most recent turns
        content = r["content"]
        if len(content) > config.HISTORY_MSG_CHAR_CAP:
            content = content[:config.HISTORY_MSG_CHAR_CAP] + " …"
        if total + len(content) > config.HISTORY_CHAR_BUDGET and msgs:
            break
        total += len(content)
        msgs.append({"role": r["role"], "content": content})
    msgs.reverse()
    fixed = []
    for m in msgs:                                # enforce alternation (merge same-role neighbours)
        if fixed and fixed[-1]["role"] == m["role"]:
            fixed[-1]["content"] += "\n" + m["content"]
        else:
            fixed.append(m)
    while fixed and fixed[0]["role"] != "user":
        fixed.pop(0)
    return fixed, session.get("summary", "")


def summarize_if_long(session_id: str) -> bool:
    """Best-effort: summarise turns that fell out of the live window. Never raises."""
    try:
        from app.services.llm import summarise
        with db.connect() as c:
            s = c.execute("SELECT summary, summarized_upto FROM sessions WHERE id=?", (session_id,)).fetchone()
            rows = c.execute("SELECT id, role, content FROM messages WHERE session_id=? ORDER BY id", (session_id,)).fetchall()
        if not s or len(rows) <= config.MEMORY_CONTEXT_WINDOW:
            return False
        old = [r for r in rows[:-config.MEMORY_CONTEXT_WINDOW] if r["id"] > s["summarized_upto"]]
        if len(old) < config.SUMMARIZE_TRIGGER:
            return False
        transcript = "\n".join(f"{r['role'].upper()}: {r['content'][:600]}" for r in old)
        if s["summary"]:
            transcript = f"PREVIOUS SUMMARY: {s['summary']}\n\n{transcript}"
        text = summarise(transcript)
        with db.connect() as c:
            c.execute("UPDATE sessions SET summary=?, summarized_upto=? WHERE id=?", (text, old[-1]["id"], session_id))
        return True
    except Exception as e:
        print(f"[memory] summarisation skipped: {type(e).__name__}")
        return False


def get_mood_history(owner: str, session_id: str) -> dict:
    get_session(owner, session_id)
    counts = {k: 0 for k in MOOD_KEYS}
    timeline = []
    with db.connect() as c:
        for r in c.execute("SELECT mood_label FROM messages WHERE session_id=? AND role='user' ORDER BY id", (session_id,)):
            label = r["mood_label"] or "neutral"
            if label in counts:
                counts[label] += 1
                timeline.append(label)
    counts["timeline"] = timeline
    return counts
