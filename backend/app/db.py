"""SQLite persistence (stdlib only — no new dependency).

Why SQLite instead of the previous memory.json / gaps.json files: those were re-read and fully
rewritten on every message, were not safe under concurrent requests, had no owner binding and had
no way to enforce "one graded answer per question". SQLite gives atomic writes, WAL concurrency and
UNIQUE constraints. Vector data stays in ChromaDB exactly as before.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app import config

SCHEMA_VERSION = 1
_db_path = None


def configure(path) -> None:
    """Point the module at a database file (tests use a temp file)."""
    global _db_path
    _db_path = Path(path)
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    init()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect():
    path = _db_path or config.DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    active_topic TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
    summarized_upto INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(owner, updated_at DESC);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role TEXT NOT NULL, content TEXT NOT NULL, ts TEXT NOT NULL,
    mood_label TEXT, tone_json TEXT, sources_json TEXT, grounding TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY, owner TEXT NOT NULL, filename TEXT NOT NULL,
    stored_path TEXT NOT NULL, content_hash TEXT NOT NULL,
    chunks INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0,
    headings_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL,
    UNIQUE(owner, filename)
);
CREATE TABLE IF NOT EXISTS quizzes (
    id TEXT PRIMARY KEY, owner TEXT NOT NULL, session_id TEXT NOT NULL,
    topic TEXT NOT NULL, topic_key TEXT NOT NULL, question_type TEXT NOT NULL,
    questions_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS quiz_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL, session_id TEXT NOT NULL, quiz_id TEXT NOT NULL,
    question_idx INTEGER NOT NULL, topic_key TEXT NOT NULL, topic_display TEXT NOT NULL,
    question TEXT NOT NULL, student_answer TEXT NOT NULL,
    correct INTEGER, score REAL NOT NULL, graded_by TEXT NOT NULL,
    result_json TEXT NOT NULL, ts TEXT NOT NULL,
    UNIQUE(quiz_id, question_idx)
);
CREATE INDEX IF NOT EXISTS idx_attempts_owner_topic ON quiz_attempts(owner, topic_key, id);
CREATE TABLE IF NOT EXISTS confusion_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner TEXT NOT NULL, session_id TEXT NOT NULL,
    topic_key TEXT NOT NULL, topic_display TEXT NOT NULL, ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conf_owner_topic ON confusion_signals(owner, topic_key, id);
CREATE TABLE IF NOT EXISTS summaries (
    doc_id TEXT PRIMARY KEY, owner TEXT NOT NULL, content_hash TEXT NOT NULL,
    summary_json TEXT NOT NULL, created_at TEXT NOT NULL
);
"""


def init() -> None:
    with connect() as c:
        c.executescript(SCHEMA)
        row = c.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            c.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))


def j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def unj(text, default=None):
    try:
        return json.loads(text) if text else default
    except Exception:
        return default
