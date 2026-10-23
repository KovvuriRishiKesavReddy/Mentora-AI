"""Document ingestion + retrieval.

Pipeline: upload -> validate -> parse (blocks with headings/pages, DOCX tables included) -> section-aware
chunking -> embed (heading + text) -> store (owner-scoped) -> retrieve (similarity = 1 - cosine distance)
-> rerank -> *section completion* (so an enumerated section is never cut in half) -> budgeted context.
"""
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from app import config, db
from app.errors import ApiError
from app.services import embedding
from app.services.vectorstore import get_store


# ══ Parsing ═══════════════════════════════════════════════════════════════════
@dataclass
class Block:
    kind: str          # "heading" | "text"
    text: str
    page: int = 0      # 0 = unknown


def looks_like_heading(s: str) -> bool:
    s = s.strip()
    if not s or len(s) > 90 or "\n" in s or s[0] in "-•*·":
        return False
    if s[-1] in ".,;:!":
        return False
    words = s.split()
    if len(words) > 12:
        return False
    if re.match(r"^(chapter|unit|section|module|part|lesson)\s+\w+", s, re.I):
        return True
    if re.match(r"^\d+(\.\d+)*[.)]?\s+\S", s):
        return True
    if s.isupper() and len(s) > 3:
        return True
    caps = sum(1 for w in words if w[0].isupper() or w[0].isdigit())
    return len(words) <= 8 and caps >= max(1, int(0.6 * len(words)))


def _parse_txt(path) -> tuple:
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    blocks = []
    for para in re.split(r"\n\s*\n", text):
        para = " ".join(para.split())
        if para:
            blocks.append(Block("heading" if looks_like_heading(para) else "text", para))
    return blocks, 0


def _parse_pdf(path) -> tuple:
    blocks = []
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(path)
        try:
            for i, page in enumerate(doc, start=1):
                for b in page.get_text("blocks"):
                    if len(b) > 6 and b[6] != 0:      # skip image blocks
                        continue
                    text = " ".join(str(b[4]).split())
                    if text:
                        blocks.append(Block("heading" if looks_like_heading(text) else "text", text, i))
            pages = len(doc)
        finally:
            doc.close()
        return blocks, pages
    except ImportError:
        from pypdf import PdfReader          # pure-python fallback
        reader = PdfReader(str(path))
        for i, page in enumerate(reader.pages, start=1):
            para = []
            for line in (page.extract_text() or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                if looks_like_heading(line):
                    if para:
                        blocks.append(Block("text", " ".join(para), i)); para = []
                    blocks.append(Block("heading", line, i))
                else:
                    para.append(line)
            if para:
                blocks.append(Block("text", " ".join(para), i))
        return blocks, len(reader.pages)


def _para_page_breaks(el) -> int:
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    n = len(el.xpath(".//w:lastRenderedPageBreak"))
    n += len(el.xpath('.//w:br[@w:type="page"]'))
    return n


def _table_blocks(table, page) -> list:
    rows = []
    for row in table.rows:
        cells, seen = [], set()
        for c in row.cells:
            if id(c._tc) in seen:        # merged cell repeated by python-docx
                continue
            seen.add(id(c._tc))
            cells.append(" ".join(c.text.split()))
        if any(cells):
            rows.append(cells)
    if not rows:
        return []
    out = []
    if len(rows) >= 2:
        header = rows[0]
        for r in rows[1:]:
            parts = [f"{header[i] if i < len(header) and header[i] else 'Column ' + str(i + 1)}: {v}"
                     for i, v in enumerate(r) if v]
            if parts:
                out.append(Block("text", "; ".join(parts), page))
    else:
        out.append(Block("text", " | ".join(rows[0]), page))
    return out


def _parse_docx(path) -> tuple:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    doc = Document(path)
    raw = []            # (kind, text, page_marker_count_before)
    markers = 0
    signal = False      # did the file mark any heading explicitly (style / all-bold)?
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = Paragraph(child, doc)
            markers += _para_page_breaks(child)
            text = " ".join(para.text.split())
            if not text:
                continue
            style = (para.style.name or "") if para.style is not None else ""
            runs = [r for r in para.runs if r.text.strip()]
            bold_only = bool(runs) and all(bool(r.bold) for r in runs)
            is_head = (style.lower().startswith("heading") or style.lower() == "title"
                       or (bold_only and len(text) <= 100 and text[-1] not in ".,;:!"))
            signal = signal or is_head
            raw.append(["heading" if is_head else "text", text, 1 + markers])
        elif tag == "tbl":
            for b in _table_blocks(Table(child, doc), 1 + markers):
                raw.append(["text", b.text, b.page])
    pages_known = markers >= 2
    blocks = []
    for kind, text, pg in raw:
        if not signal and kind == "text" and looks_like_heading(text):
            kind = "heading"
        blocks.append(Block(kind, text, pg if pages_known else 0))
    return blocks, (1 + markers if pages_known else 0)


def parse_file(path, ext: str) -> tuple:
    if ext == ".pdf":
        return _parse_pdf(path)
    if ext == ".docx":
        return _parse_docx(path)
    if ext == ".txt":
        return _parse_txt(path)
    raise ApiError(400, f"Unsupported file type '{ext}'.", "bad_extension")


# ══ Chunking ══════════════════════════════════════════════════════════════════
@dataclass
class Chunk:
    text: str
    heading: str          # heading path, e.g. "1. Introduction > Types of Operating Systems"
    section_idx: int
    page: int
    index: int = 0


def _split_long(text: str, size: int, overlap: int) -> list:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    pieces, cur = [], ""
    for s in sentences:
        while len(s) > size:                 # a single enormous sentence
            if cur:
                pieces.append(cur); cur = ""
            pieces.append(s[:size]); s = s[size - overlap:]
        if cur and len(cur) + len(s) + 1 > size:
            pieces.append(cur)
            tail = cur[-overlap:] if overlap else ""
            cur = (tail.split(" ", 1)[-1] + " " + s).strip() if tail else s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        pieces.append(cur)
    return pieces


def build_chunks(blocks: list, size: int = None, overlap: int = None) -> list:
    size = size or config.CHUNK_SIZE_CHARS
    overlap = config.CHUNK_OVERLAP_CHARS if overlap is None else overlap
    sections, cur, major = [], {"heading": "", "path": "", "paras": []}, ""
    for b in blocks:
        if b.kind == "heading":
            if cur["paras"]:
                sections.append(cur)
            if re.match(r"^\d+[.)]\s", b.text):
                major = b.text
            path = b.text if (not major or b.text == major) else f"{major} > {b.text}"
            cur = {"heading": b.text, "path": path, "paras": []}
        else:
            cur["paras"].append((b.text, b.page))
    if cur["paras"]:
        sections.append(cur)

    chunks = []
    for si, sec in enumerate(sections):
        buf, buflen, bpage = [], 0, 0

        def flush():
            nonlocal buf, buflen, bpage
            if buf:
                chunks.append(Chunk(" ".join(buf).strip(), sec["path"], si, bpage))
            buf, buflen, bpage = [], 0, 0

        for text, pg in sec["paras"]:
            for piece in ([text] if len(text) <= size else _split_long(text, size, overlap)):
                if buf and buflen + len(piece) + 1 > size:
                    flush()
                if not buf:
                    bpage = pg
                buf.append(piece); buflen += len(piece) + 1
        flush()
    chunks = [c for c in chunks if len(c.text) > 15]
    for i, c in enumerate(chunks):
        c.index = i
    return chunks


# ══ Ingestion ═════════════════════════════════════════════════════════════════
def _sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def make_doc_id(owner: str, content_hash: str) -> str:
    return hashlib.sha256(f"{owner}:{content_hash}".encode()).hexdigest()[:20]


def owner_dir(owner: str) -> Path:
    return config.UPLOAD_DIR / hashlib.sha256(owner.encode()).hexdigest()[:12]


def _index_document(owner: str, path: Path, filename: str, doc_id: str) -> dict:
    ext = Path(filename).suffix.lower()
    blocks, pages = parse_file(path, ext)
    chunks = build_chunks(blocks)
    if not chunks:
        raise ApiError(422, "No readable text found in this file (is it a scanned/image-only PDF?).", "no_text")
    emb = embedding.get_embedder()
    texts = [f"{c.heading}\n{c.text}" if c.heading else c.text for c in chunks]
    vecs = []
    for i in range(0, len(texts), 16):
        vecs.extend(emb.encode(texts[i:i + 16], show_progress_bar=False))
    ids = [f"{doc_id}-{c.index:05d}" for c in chunks]
    metas = [{"owner": owner, "doc_id": doc_id, "filename": filename, "heading": c.heading,
              "section_id": f"{doc_id}:{c.section_idx}", "page_number": int(c.page),
              "chunk_index": int(c.index)} for c in chunks]
    store = get_store()
    store.delete_doc(owner, doc_id)                      # idempotent re-index of the same doc
    store.upsert(ids, vecs, [c.text for c in chunks], metas)
    headings = []
    for c in chunks:
        h = c.heading.split(" > ")[-1]
        if h and h not in headings:
            headings.append(h)
    return {"chunks": len(chunks), "pages": pages, "headings": headings[:60]}


def ingest_file(owner: str, path: Path, filename: str) -> dict:
    """Index a saved upload. Re-upload policy:
      * same filename            -> replace (old chunks removed after the new ones are stored)
      * same bytes, other name   -> 'duplicate': nothing added, existing document returned
      * identical re-upload      -> 'unchanged'
    Returns dict incl. status in {'added','replaced','unchanged','duplicate'} and the doc_id."""
    content_hash = _sha256_file(path)
    doc_id = make_doc_id(owner, content_hash)
    store = get_store()
    with db.connect() as c:
        same_name = c.execute("SELECT * FROM documents WHERE owner=? AND filename=?", (owner, filename)).fetchone()
        same_hash = c.execute("SELECT * FROM documents WHERE owner=? AND content_hash=?", (owner, content_hash)).fetchone()

    if same_hash and same_hash["filename"] != filename:
        return {"status": "duplicate", "doc_id": same_hash["doc_id"], "filename": same_hash["filename"],
                "chunks_created": 0, "chapters_detected": db.unj(same_hash["headings_json"], []),
                "pages": same_hash["pages"]}
    if same_name and same_name["content_hash"] == content_hash and store.count_doc(owner, doc_id) > 0:
        return {"status": "unchanged", "doc_id": doc_id, "filename": filename, "chunks_created": 0,
                "chapters_detected": db.unj(same_name["headings_json"], []), "pages": same_name["pages"]}

    info = _index_document(owner, path, filename, doc_id)
    status = "added"
    old_path = None
    with db.connect() as c:
        if same_name:
            status = "replaced"
            old_path = same_name["stored_path"]
            if same_name["doc_id"] != doc_id:
                store.delete_doc(owner, same_name["doc_id"])
            c.execute("DELETE FROM documents WHERE doc_id=?", (same_name["doc_id"],))
        c.execute("INSERT OR REPLACE INTO documents(doc_id,owner,filename,stored_path,content_hash,chunks,pages,"
                  "headings_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (doc_id, owner, filename, str(path), content_hash, info["chunks"], info["pages"],
                   db.j(info["headings"]), db.now()))
    if old_path and Path(old_path) != Path(path):
        _safe_unlink(old_path)
    return {"status": status, "doc_id": doc_id, "filename": filename, "chunks_created": info["chunks"],
            "chapters_detected": info["headings"], "pages": info["pages"]}


def _safe_unlink(p) -> None:
    try:
        rp = Path(p).resolve()
        if config.UPLOAD_DIR.resolve() in rp.parents and rp.exists():
            rp.unlink()
    except Exception:
        pass


def is_referenced(path) -> bool:
    with db.connect() as c:
        return c.execute("SELECT 1 FROM documents WHERE stored_path=?", (str(path),)).fetchone() is not None


def list_files(owner: str) -> list:
    with db.connect() as c:
        rows = c.execute("SELECT * FROM documents WHERE owner=? ORDER BY created_at", (owner,)).fetchall()
    return [{"doc_id": r["doc_id"], "filename": r["filename"], "chunks": r["chunks"], "pages": r["pages"],
             "chapters": db.unj(r["headings_json"], [])[:20]} for r in rows]


def delete_file(owner: str, doc_id: str) -> bool:
    with db.connect() as c:
        row = c.execute("SELECT * FROM documents WHERE owner=? AND doc_id=?", (owner, doc_id)).fetchone()
        if not row:
            return False
        c.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))
        c.execute("DELETE FROM summaries WHERE doc_id=?", (doc_id,))
    get_store().delete_doc(owner, doc_id)
    _safe_unlink(row["stored_path"])
    return True


def reindex_missing() -> int:
    """After a restart / embedder change / wiped vector DB, rebuild chunks for registered documents whose
    stored file still exists. Returns the number of documents re-indexed."""
    store = get_store()
    with db.connect() as c:
        rows = c.execute("SELECT * FROM documents").fetchall()
    n = 0
    for r in rows:
        try:
            if store.count_doc(r["owner"], r["doc_id"]) == 0 and Path(r["stored_path"]).exists():
                info = _index_document(r["owner"], Path(r["stored_path"]), r["filename"], r["doc_id"])
                with db.connect() as c:
                    c.execute("UPDATE documents SET chunks=?, pages=?, headings_json=? WHERE doc_id=?",
                              (info["chunks"], info["pages"], db.j(info["headings"]), r["doc_id"]))
                n += 1
        except Exception as e:
            print(f"[rag] re-index of {r['filename']} failed: {type(e).__name__}")
    return n


# ══ Retrieval ═════════════════════════════════════════════════════════════════
@dataclass
class Hit:
    text: str
    filename: str
    doc_id: str
    heading: str
    page: int
    chunk_index: int
    section_id: str
    similarity: float          # true cosine similarity
    score: float               # similarity + small lexical bonus (ranking only)
    chunk_id: str
    coverage: float = 0.0


def _hit(r, sim, cov):
    m = r["meta"]
    return Hit(text=r["text"], filename=m.get("filename", "unknown"), doc_id=m.get("doc_id", ""),
               heading=m.get("heading", ""), page=int(m.get("page_number", 0) or 0),
               chunk_index=int(m.get("chunk_index", 0)), section_id=m.get("section_id", ""),
               similarity=round(sim, 4), score=round(sim + 0.10 * cov, 4), chunk_id=r["id"], coverage=cov)


def retrieve(owner: str, query: str, top_k: int = None, doc_id: Optional[str] = None) -> list:
    top_k = top_k or config.RAG_TOP_K
    store = get_store()
    try:
        qv = embedding.get_embedder().encode([query], show_progress_bar=False)[0]
        rows = store.query(qv, top_k, owner, doc_id)
    except Exception as e:
        print(f"[rag] retrieval error: {type(e).__name__}")
        return []
    qtok = set(embedding.tokens(query))
    hits = []
    for r in rows:
        sim = 1.0 - float(r["distance"])
        ctok = set(embedding.tokens(r["meta"].get("heading", "") + " " + r["text"]))
        cov = (len(qtok & ctok) / len(qtok)) if qtok else 0.0
        hits.append(_hit(r, sim, cov))
    return sorted(hits, key=lambda h: h.score, reverse=True)


@dataclass
class Evidence:
    grounding: str = "none"                 # strong | weak | none
    context: str = ""
    sources: list = field(default_factory=list)
    top_similarity: float = 0.0
    truncated: bool = False
    sections_used: int = 0
    sections_omitted: int = 0
    hits: list = field(default_factory=list)


def gather(owner: str, query: str, broad: bool = False, doc_id: Optional[str] = None) -> Evidence:
    weak, strong = embedding.thresholds()
    hits = retrieve(owner, query, config.RAG_BROAD_TOP_K if broad else config.RAG_TOP_K, doc_id)
    if embedding.get_embedder().semantic:
        good = [h for h in hits if h.similarity >= weak or (h.coverage >= 0.6 and h.similarity >= weak * 0.6)]
    else:   # lexical fallback embedder: require real word overlap too (hash collisions on short chunks give false matches)
        good = [h for h in hits if h.similarity >= weak and h.coverage >= 0.4]
    if not good:
        return Evidence(top_similarity=max([h.similarity for h in hits], default=0.0), hits=hits)
    top = max(h.similarity for h in good)
    grounding = "strong" if top >= strong else "weak"
    budget = config.BROAD_CONTEXT_CHAR_BUDGET if broad else config.CONTEXT_CHAR_BUDGET

    by_section = {}
    for h in good:
        by_section.setdefault(h.section_id, []).append(h)
    floor = max(weak, 0.6 * top)            # drop sections that are only loosely related compared with the best match
    ranked = sorted(((sid, hs) for sid, hs in by_section.items() if max(h.similarity for h in hs) >= floor),
                    key=lambda kv: max(h.score for h in kv[1]), reverse=True)
    max_sections = 8 if broad else 3
    chosen, omitted = ranked[:max_sections], max(0, len(ranked) - max_sections)

    all_rows = get_store().fetch_sections(owner, [sid for sid, _ in chosen])
    rows_by_section = {}
    for r in all_rows:
        rows_by_section.setdefault(r["meta"]["section_id"], []).append(r)

    used, truncated, picked = 0, False, []      # 'truncated' = the char budget forced us to drop relevant text
    for sid, shits in chosen:
        sec_rows = sorted(rows_by_section.get(sid, []), key=lambda r: r["meta"]["chunk_index"])
        total = sum(len(r["text"]) for r in sec_rows)
        hit_idx = {h.chunk_index for h in shits}
        if sec_rows and (broad or total <= 3500):
            use = sec_rows                                          # complete section
        else:                                                        # hit chunks ± neighbour
            keep = {i + d for i in hit_idx for d in (-1, 0, 1)}
            use = [r for r in sec_rows if r["meta"]["chunk_index"] in keep]
        cost = sum(len(r["text"]) for r in use)
        if used + cost > budget:
            use = [r for r in sec_rows if r["meta"]["chunk_index"] in hit_idx]       # fall back to the hits only
            truncated = truncated or broad          # a focused question is *meant* to get focused context
            cost = sum(len(r["text"]) for r in use)
            if used + cost > budget:
                omitted += 1
                truncated = truncated or broad
                continue
        used += cost
        picked.append((sid, shits, use))

    if not broad:
        order = picked                                               # relevance order
    else:                                                            # document order, for lists / overviews
        order = sorted(picked, key=lambda p: (p[2][0]["meta"]["doc_id"], p[2][0]["meta"]["chunk_index"]) if p[2] else ("", 0))
    parts, sources = ["RETRIEVED CONTEXT FROM THE STUDENT'S NOTES (each source is a separate document section):\n"], []
    for n, (sid, shits, use) in enumerate(order, 1):
        if not use:
            continue
        m = use[0]["meta"]
        page = next((int(r["meta"].get("page_number", 0)) for r in use if r["meta"].get("page_number")), 0)
        label = f"[Source {n}: {m.get('filename')} | {m.get('heading') or 'General'}" + (f" | page {page}" if page else "") + "]"
        parts.append(f"{label}\n" + "\n".join(r["text"] for r in use) + "\n")
        sources.append({"filename": m.get("filename"), "doc_id": m.get("doc_id"), "chapter": m.get("heading") or "General",
                        "page": page, "score": round(max(h.similarity for h in shits), 3),
                        "preview": use[0]["text"][:200] + ("…" if len(use[0]["text"]) > 200 else "")})
    return Evidence(grounding=grounding, context="\n".join(parts), sources=sources, top_similarity=round(top, 4),
                    truncated=truncated, sections_used=len(sources), sections_omitted=omitted, hits=good)
