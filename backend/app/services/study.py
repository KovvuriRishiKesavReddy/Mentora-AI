"""Study tools built on the student's own notes.

  * outline()            deterministic chapter/section tree (drives the mind map) - no LLM, no cost
  * get_passage()        the full text of one section, so a citation can be opened and checked
  * summarize_document() whole-document summary: every chapter is summarised piece by piece (nothing skipped),
                         then one overview is written from the chapter summaries (map-reduce). Cached per
                         document content hash, so it is generated once.
  * generate_flashcards() question/answer cards from a topic or an evenly spread sample of the whole document

Every LLM prompt says "use only the text given". If the model fails part-way, the affected piece falls back to
the first sentences of the text (marked method="extractive") and the result is NOT cached, so a retry can do
better. If the very first call fails the error is returned as-is (nothing is invented or cached).
"""
import json
import math
import re

from app import config, db
from app.chat_service import llm_error_to_api
from app.errors import ApiError
from app.services import rag
from app.services.llm import LLMError, call_llm
from app.services.vectorstore import get_store


# ── helpers ──────────────────────────────────────────────────────────────────────────────────────────
def _doc_row(owner: str, doc_id: str):
    with db.connect() as c:
        row = c.execute("SELECT * FROM documents WHERE owner=? AND doc_id=?", (owner, doc_id)).fetchone()
    if not row:
        raise ApiError(404, "Document not found.", "document_not_found")
    return row


def _chunks(owner: str, doc_id: str) -> list:
    rows = get_store().fetch_doc(owner, doc_id)
    if not rows:
        raise ApiError(409, "This document has no indexed text yet. Re-upload it or restart the backend.", "not_indexed")
    return rows


def _sections(rows: list) -> list:
    """Consecutive chunks with the same heading path -> one section."""
    out = []
    for r in rows:
        m = r["meta"]
        path = m.get("heading") or "General"
        if out and out[-1]["path"] == path:
            out[-1]["text"] += "\n" + r["text"]
            continue
        parts = path.split(" > ")
        out.append({"path": path, "chapter": parts[0], "title": parts[-1], "is_chapter_intro": len(parts) == 1,
                    "page": int(m.get("page_number", 0) or 0), "text": r["text"]})
    return out


# ── outline / mind map ───────────────────────────────────────────────────────────────────────────────
def outline(owner: str, doc_id: str) -> dict:
    row = _doc_row(owner, doc_id)
    chapters = []
    for s in _sections(_chunks(owner, doc_id)):
        if not chapters or chapters[-1]["title"] != s["chapter"]:
            chapters.append({"title": s["chapter"], "page": s["page"], "chars": 0, "sections": []})
        ch = chapters[-1]
        ch["chars"] += len(s["text"])
        if not s["is_chapter_intro"]:
            ch["sections"].append({"title": s["title"], "path": s["path"], "page": s["page"], "chars": len(s["text"])})
    return {"doc_id": doc_id, "filename": row["filename"], "chapters": chapters}


# ── citations ────────────────────────────────────────────────────────────────────────────────────────
def get_passage(owner: str, doc_id: str, heading: str, max_chars: int = 6000) -> dict:
    row = _doc_row(owner, doc_id)
    rows = [r for r in get_store().fetch_doc(owner, doc_id) if (r["meta"].get("heading") or "General") == heading]
    if not rows:
        raise ApiError(404, "That section was not found in this document.", "passage_not_found")
    text = "\n".join(r["text"] for r in rows)
    page = next((int(r["meta"].get("page_number", 0)) for r in rows if r["meta"].get("page_number")), 0)
    return {"doc_id": doc_id, "filename": row["filename"], "heading": heading, "page": page,
            "text": text[:max_chars], "truncated": len(text) > max_chars}


# ── whole-document summary ───────────────────────────────────────────────────────────────────────────
_SUM_SYSTEM = "You are a careful study-notes summariser. Use ONLY the text you are given."


def _bullets_from(text: str) -> list:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        line = re.sub(r"^[-*•\d.)\s]+", "", line).strip()
        if line:
            out.append(line)
    return out


def _extractive(text: str, n: int = 3) -> list:
    flat = re.sub(r"\s+", " ", text)
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", flat) if len(s.strip()) > 25]
    return [s[:240] for s in sents[:n]] or [flat[:240]]


def _pack_pieces(sections: list, piece_chars: int) -> list:
    """Pack a chapter's sections into pieces of at most piece_chars (never cutting text away)."""
    units = []
    for s in sections:
        body = s["text"]
        if len(body) + len(s["title"]) + 4 > piece_chars:
            for part in rag._split_long(body, piece_chars - 200, 0):
                units.append(f"## {s['title']}\n{part}")
        else:
            units.append(f"## {s['title']}\n{body}")
    pieces, cur = [], ""
    for u in units:
        if cur and len(cur) + len(u) + 2 > piece_chars:
            pieces.append(cur)
            cur = u
        else:
            cur = f"{cur}\n\n{u}".strip()
    if cur:
        pieces.append(cur)
    return pieces


def _summarise_piece(chapter: str, piece: str) -> list:
    prompt = ("Summarise this part of the student's notes as 3-6 concise bullet points, each starting with '- '. Keep the "
              "key terms, definitions, numbers and algorithm names exactly as the text uses them. Do not add facts that are "
              "not in the text.\n\n"
              f"CHAPTER: {chapter}\n\n{piece}")
    raw = call_llm(_SUM_SYSTEM, [{"role": "user", "content": prompt}], 450)
    bullets = _bullets_from(raw)
    if not bullets:
        raise LLMError("upstream", "empty summary")
    return bullets[:8]


def _overview(chapters: list) -> tuple:
    digest = []
    for c in chapters:
        digest.append(f"{c['title']}:\n" + "\n".join(f"- {b}" for b in c["bullets"][:4]))
    prompt = ("Below are chapter summaries of a student's notes. Write (1) an OVERVIEW of 4-6 sentences saying what the notes "
              "are about, what each part covers and how the parts connect, then (2) a final line 'KEY TERMS:' with 10-15 "
              "important terms taken from the summaries, comma-separated. Use only what is below.\n\n"
              + "\n\n".join(digest)[:12000])
    raw = call_llm(_SUM_SYSTEM, [{"role": "user", "content": prompt}], 600)
    m = re.search(r"KEY TERMS\s*:\s*(.+)", raw, re.I | re.S)
    terms = [t.strip(" .*-") for t in re.split(r",|\n", m.group(1)) if t.strip(" .*-")] if m else []
    overview = raw[:m.start()].strip() if m else raw.strip()
    overview = re.sub(r"^\**\s*overview\s*:?\s*\**\s*", "", overview, flags=re.I).strip()
    return overview, terms[:15]


def _to_markdown(result: dict) -> str:
    lines = [f"# Summary of {result['filename']}", "", "## Overview", "", result["overview"], ""]
    if result["key_terms"]:
        lines += ["**Key terms:** " + ", ".join(result["key_terms"]), ""]
    for c in result["chapters"]:
        lines += [f"## {c['title']}", ""] + [f"- {b}" for b in c["bullets"]] + [""]
    return "\n".join(lines)


def get_cached_summary(owner: str, doc_id: str):
    row = _doc_row(owner, doc_id)
    with db.connect() as c:
        s = c.execute("SELECT * FROM summaries WHERE doc_id=? AND owner=?", (doc_id, owner)).fetchone()
    if s and s["content_hash"] == row["content_hash"]:
        data = db.unj(s["summary_json"], None)
        if data:
            data["cached"] = True
            return data
    return None


def summarize_document(owner: str, doc_id: str, refresh: bool = False) -> dict:
    row = _doc_row(owner, doc_id)
    if not refresh:
        cached = get_cached_summary(owner, doc_id)
        if cached:
            return cached
    sections = _sections(_chunks(owner, doc_id))
    total = sum(len(s["text"]) for s in sections)
    piece_chars = max(config.SUMMARY_PIECE_CHARS, math.ceil(total / max(1, config.SUMMARY_MAX_CALLS)))

    by_chapter = []
    for s in sections:
        if not by_chapter or by_chapter[-1]["title"] != s["chapter"]:
            by_chapter.append({"title": s["chapter"], "sections": []})
        by_chapter[-1]["sections"].append(s)

    complete, chapters, first_call = True, [], True
    for ch in by_chapter:
        bullets, method = [], "llm"
        for piece in _pack_pieces(ch["sections"], piece_chars):
            try:
                bullets += _summarise_piece(ch["title"], piece)
            except LLMError as e:
                if first_call:
                    raise llm_error_to_api(e)          # model unusable: fail clearly, cache nothing
                complete, method = False, "extractive"
                bullets += _extractive(piece)
            first_call = False
        chapters.append({"title": ch["title"], "bullets": bullets, "method": method,
                         "sections": [s["title"] for s in ch["sections"] if not s["is_chapter_intro"]]})

    try:
        overview, terms = _overview(chapters)
    except LLMError:
        complete = False
        overview = "Overview unavailable right now - the chapter summaries below are complete. Regenerate to try again."
        terms = []
    result = {"doc_id": doc_id, "filename": row["filename"], "overview": overview, "key_terms": terms,
              "chapters": chapters, "complete": complete, "generated_at": db.now(), "cached": False}
    result["markdown"] = _to_markdown(result)
    if complete:
        with db.connect() as c:
            c.execute("INSERT OR REPLACE INTO summaries(doc_id,owner,content_hash,summary_json,created_at) VALUES (?,?,?,?,?)",
                      (doc_id, owner, row["content_hash"], db.j(result), db.now()))
    return result


# ── flashcards ───────────────────────────────────────────────────────────────────────────────────────
def _parse_cards(raw: str) -> list:
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    cards, seen = [], set()
    for d in data if isinstance(data, list) else []:
        if not isinstance(d, dict):
            continue
        front, back = str(d.get("front", "")).strip(), str(d.get("back", "")).strip()
        if len(front) < 3 or len(back) < 3 or front.lower() in seen:
            continue
        seen.add(front.lower())
        cards.append({"front": front[:300], "back": back[:600]})
    return cards


def _document_sample(owner: str, doc_id: str, n_cards: int, budget: int = 9000) -> str:
    secs = _sections(_chunks(owner, doc_id))
    k = max(1, min(len(secs), n_cards))
    step = (len(secs) - 1) / (k - 1) if k > 1 else 0          # first and last section are always included
    picked = [secs[round(i * step)] for i in range(k)]
    per = max(300, budget // k)
    return "\n\n".join(f"## {s['path']}\n{s['text'][:per]}" for s in picked)


def generate_flashcards(owner: str, doc_id: str = None, topic: str = None, n: int = 10) -> dict:
    n = max(1, min(int(n), config.FLASHCARD_MAX))
    if topic and topic.strip():
        ev = rag.gather(owner, topic.strip(), broad=True, doc_id=doc_id or None)
        if ev.grounding == "none":
            raise ApiError(404, f"No content found for '{topic}' in your notes.", "no_content")
        text, label = ev.context, topic.strip()
    elif doc_id:
        row = _doc_row(owner, doc_id)
        text, label = _document_sample(owner, doc_id, n), row["filename"]
    else:
        raise ApiError(400, "Give a topic or choose a document.", "bad_request")
    prompt = (f"Create exactly {n} flashcards from the student's notes below. Use ONLY this text.\n"
              'Reply with ONLY a JSON array: [{"front": "question or term", "back": "answer in at most 2 sentences"}].\n'
              "Cover different ideas; no duplicate fronts.\n\n" + text)
    try:
        raw = call_llm("You make accurate flashcards. Reply with valid JSON only.", [{"role": "user", "content": prompt}], 1800)
    except LLMError as e:
        raise llm_error_to_api(e)
    cards = _parse_cards(raw)[:n]
    if not cards:
        raise ApiError(502, "Could not generate flashcards this time. Please try again.", "flashcards_failed")
    return {"topic": label, "cards": cards}
