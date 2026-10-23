"""One-off import of data written by the previous version (data/memory.json, uploads/).

    cd backend
    python -m app.tools.migrate_legacy --owner <your client id> [--reindex]

* Nothing is deleted: legacy JSON files are COPIED to data/legacy_backup/ first.
* Chat sessions/messages from memory.json are imported for the given owner (copy your id from the browser:
  DevTools > Application > Local Storage > mentora.clientId, or print it from the console with
  localStorage.getItem('mentora.clientId')).
* data/gaps.json (old "weak topic" data) is backed up but NOT imported on purpose: it was derived from chat
  messages and from MCQ grading that could mark correct answers as wrong, so it cannot be trusted as mastery evidence.
  Mastery now starts fresh from graded quizzes.
* --reindex indexes the files found directly in uploads/ for that owner (the old vector DB is not reused).
"""
import argparse
import json
import shutil
import uuid
from pathlib import Path

from app import config, db
from app.security import validate_owner


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", required=True)
    ap.add_argument("--reindex", action="store_true")
    a = ap.parse_args()
    owner = validate_owner(a.owner)
    db.init()
    backup = config.DATA_DIR / "legacy_backup"
    backup.mkdir(parents=True, exist_ok=True)
    for name in ("memory.json", "gaps.json"):
        f = config.DATA_DIR / name
        if f.exists():
            shutil.copy2(f, backup / name)
            print(f"backed up {name}")
    mem = config.DATA_DIR / "memory.json"
    if mem.exists():
        data = json.loads(mem.read_text(encoding="utf-8"))
        n_s = n_m = 0
        with db.connect() as c:
            for old_id, sess in data.items():
                msgs = [m for m in sess.get("messages", []) if not m.get("is_summary") and m.get("role") in ("user", "assistant")]
                if not msgs:
                    continue
                sid = str(uuid.uuid4())
                first = next((m["content"] for m in msgs if m["role"] == "user"), "")
                c.execute("INSERT INTO sessions(id,owner,title,created_at,updated_at) VALUES (?,?,?,?,?)",
                          (sid, owner, " ".join(first.split())[:60], sess.get("created_at", db.now()), msgs[-1].get("timestamp", db.now())))
                for m in msgs:
                    c.execute("INSERT INTO messages(session_id,role,content,ts,mood_label,tone_json,sources_json) VALUES (?,?,?,?,?,?,?)",
                              (sid, m["role"], m["content"], m.get("timestamp", db.now()), m.get("mood_label"),
                               db.j(m.get("tone_profile")), db.j(m.get("retrieved_chunks", []))))
                    n_m += 1
                n_s += 1
        print(f"imported {n_s} session(s), {n_m} message(s)")
    if a.reindex:
        from app.services import rag
        for f in sorted(config.UPLOAD_DIR.glob("*")):
            if f.is_file() and f.suffix.lower() in config.ALLOWED_EXTENSIONS:
                dst = rag.owner_dir(owner)
                dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst / f.name)
                r = rag.ingest_file(owner, dst / f.name, f.name)
                print(f"indexed {f.name}: {r['status']} ({r['chunks_created']} chunks)")


if __name__ == "__main__":
    main()
