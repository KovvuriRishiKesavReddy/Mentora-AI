"""Vector stores behind one small interface.

ChromaStore  — production (persistent ChromaDB, cosine space, same API surface the project already used).
MemoryStore  — numpy, in-process. Used by the automated tests and as a degraded fallback if chromadb
               cannot be imported (data is then NOT persistent across restarts; a warning is printed).

distance is always *cosine distance* (= 1 - cosine similarity) so callers compute similarity = 1 - distance.
(The previous code used 1 - distance/2, which maps cos=-0.4 to 0.30 and made the 0.30 gate pass for
almost everything.)
"""
import threading
import numpy as np

from app import config


def _owner_where(owner, doc_id=None):
    if doc_id:
        return {"$and": [{"owner": owner}, {"doc_id": doc_id}]}
    return {"owner": owner}


class MemoryStore:
    persistent = False

    def __init__(self):
        self._rows = {}
        self._lock = threading.Lock()

    def upsert(self, ids, embeddings, documents, metadatas):
        with self._lock:
            for i, e, d, m in zip(ids, embeddings, documents, metadatas):
                v = np.asarray(e, dtype=np.float32)
                v = v / (np.linalg.norm(v) + 1e-9)
                self._rows[i] = (v, d, dict(m))

    def query(self, embedding, n, owner, doc_id=None):
        q = np.asarray(embedding, dtype=np.float32)
        q = q / (np.linalg.norm(q) + 1e-9)
        with self._lock:
            rows = [(i, r) for i, r in self._rows.items()
                    if r[2]["owner"] == owner and (doc_id is None or r[2]["doc_id"] == doc_id)]
        scored = sorted(((1.0 - float(r[0] @ q), i, r) for i, r in rows), key=lambda t: t[0])[:n]
        return [{"id": i, "text": r[1], "meta": r[2], "distance": d} for d, i, r in scored]

    def fetch_sections(self, owner, section_ids):
        sids = set(section_ids)
        with self._lock:
            return [{"id": i, "text": r[1], "meta": r[2]} for i, r in self._rows.items()
                    if r[2]["owner"] == owner and r[2]["section_id"] in sids]

    def fetch_doc(self, owner, doc_id):
        with self._lock:
            rows = [{"id": i, "text": r[1], "meta": r[2]} for i, r in self._rows.items()
                    if r[2]["owner"] == owner and r[2]["doc_id"] == doc_id]
        return sorted(rows, key=lambda r: int(r["meta"].get("chunk_index", 0)))

    def delete_doc(self, owner, doc_id):
        with self._lock:
            for i in [i for i, r in self._rows.items()
                      if r[2]["owner"] == owner and r[2]["doc_id"] == doc_id]:
                del self._rows[i]

    def count_doc(self, owner, doc_id):
        with self._lock:
            return sum(1 for r in self._rows.values()
                       if r[2]["owner"] == owner and r[2]["doc_id"] == doc_id)

    def count(self):
        return len(self._rows)


class ChromaStore:
    persistent = True

    def __init__(self, path, name):
        import chromadb
        config.CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(path))
        self._col = self._client.get_or_create_collection(name=name, metadata={"hnsw:space": "cosine"})
        print(f"[rag] ChromaDB '{name}' ready — {self._col.count()} chunks")

    def upsert(self, ids, embeddings, documents, metadatas):
        for i in range(0, len(ids), 100):
            self._col.upsert(ids=ids[i:i + 100], embeddings=[list(map(float, e)) for e in embeddings[i:i + 100]],
                             documents=documents[i:i + 100], metadatas=metadatas[i:i + 100])

    def query(self, embedding, n, owner, doc_id=None):
        where = _owner_where(owner, doc_id)
        total = len(self._col.get(where=where, include=[])["ids"])
        if total == 0:
            return []
        r = self._col.query(query_embeddings=[list(map(float, embedding))], n_results=min(n, total),
                            where=where, include=["documents", "metadatas", "distances"])
        return [{"id": r["ids"][0][k], "text": r["documents"][0][k], "meta": r["metadatas"][0][k],
                 "distance": float(r["distances"][0][k])} for k in range(len(r["ids"][0]))]

    def fetch_sections(self, owner, section_ids):
        if not section_ids:
            return []
        r = self._col.get(where={"$and": [{"owner": owner}, {"section_id": {"$in": list(section_ids)}}]},
                          include=["documents", "metadatas"])
        return [{"id": r["ids"][k], "text": r["documents"][k], "meta": r["metadatas"][k]}
                for k in range(len(r["ids"]))]

    def fetch_doc(self, owner, doc_id):
        r = self._col.get(where=_owner_where(owner, doc_id), include=["documents", "metadatas"])
        rows = [{"id": r["ids"][k], "text": r["documents"][k], "meta": r["metadatas"][k]} for k in range(len(r["ids"]))]
        return sorted(rows, key=lambda x: int(x["meta"].get("chunk_index", 0)))

    def delete_doc(self, owner, doc_id):
        self._col.delete(where=_owner_where(owner, doc_id))

    def count_doc(self, owner, doc_id):
        return len(self._col.get(where=_owner_where(owner, doc_id), include=[])["ids"])

    def count(self):
        return self._col.count()


_store = None


def get_store():
    global _store
    if _store is None:
        from app.services.embedding import get_embedder
        name = f"student_notes_v2_{get_embedder().name}"
        try:
            _store = ChromaStore(config.CHROMA_DIR, name)
        except Exception as e:
            print(f"[rag] WARNING: ChromaDB unavailable ({type(e).__name__}: {e}). "
                  "Using in-memory store — notes will need re-indexing after restart.")
            _store = MemoryStore()
    return _store


def set_store(s) -> None:
    global _store
    _store = s
