"""Embedding back-ends.

Primary: sentence-transformers all-MiniLM-L6-v2.
Fallback (model cannot be loaded, e.g. offline): a deterministic hashing embedder. The previous
fallback re-fitted a TF-IDF vocabulary on every call, which silently changed the vector space and made
stored embeddings incomparable with new queries. The hashing embedder has a fixed space.
"""
import re
import zlib
import numpy as np

from app import config

_STOP = set("""a an the and or but if of to in on at by for with from as is are was were be been being it its this that
these those i you he she we they my your our their me us them do does did can could should would will what which who
whom whose how why when where about into over than then so such not no
explain describe define tell give show list summarize summarise summary notes note please understand mean means
what's whats let know like get make help need want also just very more most some any all""".split())
_TOKEN = re.compile(r"[a-z0-9]+")


_SUFFIXES = ("ations", "ation", "ments", "ment", "ions", "ion", "ings", "ing", "ies", "ed", "es", "ly", "s")


def stem(t: str) -> str:
    """Very light suffix stripping so 'prevented' / 'prevention' / 'prevents' meet. Used for lexical matching only."""
    if len(t) < 6:
        return t[:-1] if len(t) > 3 and t.endswith("s") and not t.endswith("ss") else t
    for suf in _SUFFIXES:
        if t.endswith(suf) and len(t) - len(suf) >= 4:
            return t[:-len(suf)]
    return t


def tokens(text: str, stemmed: bool = True) -> list:
    return [stem(t) if stemmed else t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1]


class HashingEmbedder:
    name = "hashing2048"
    semantic = False
    dim = 2048
    thresholds = (0.10, 0.25)      # (weak, strong) true-cosine defaults for this embedder

    def encode(self, texts, show_progress_bar=False, **_):
        if isinstance(texts, str):
            texts = [texts]
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for r, text in enumerate(texts):
            toks = tokens(text, stemmed=False)     # vectors stay as calibrated; stemming is for lexical coverage only
            feats = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]
            counts = {}
            for f in feats:
                counts[f] = counts.get(f, 0) + 1
            for f, c in counts.items():
                h = zlib.crc32(f.encode())
                sign = 1.0 if (h >> 31) & 1 else -1.0
                out[r, h % self.dim] += sign * (1.0 + np.log(c))
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / (norms + 1e-9)


class SentenceEmbedder:
    semantic = True
    thresholds = (0.25, 0.40)

    def __init__(self, model):
        self._m = model
        self.name = "minilm"

    def encode(self, texts, show_progress_bar=False, **_):
        if isinstance(texts, str):
            texts = [texts]
        e = self._m.encode(list(texts), show_progress_bar=show_progress_bar, normalize_embeddings=True)
        return np.asarray(e, dtype=np.float32)


_embedder = None


def get_embedder():
    global _embedder
    if _embedder is not None:
        return _embedder
    mode = config.EMBEDDER_MODE
    if mode != "hashing":
        try:
            from sentence_transformers import SentenceTransformer
            print("[embed] loading", config.EMBED_MODEL)
            _embedder = SentenceEmbedder(SentenceTransformer(config.EMBED_MODEL))
            _embedder.encode(["warmup"])
            print("[embed] sentence-transformers ready")
        except Exception as e:           # offline, missing package, etc.
            if mode == "minilm":
                raise
            print(f"[embed] WARNING: could not load {config.EMBED_MODEL} ({type(e).__name__}); "
                  "using the weaker hashing fallback. Retrieval quality will be lower.")
            _embedder = HashingEmbedder()
    else:
        _embedder = HashingEmbedder()
    return _embedder


def set_embedder(e) -> None:
    global _embedder
    _embedder = e


def thresholds():
    e = get_embedder()
    weak, strong = e.thresholds
    if config.SIM_WEAK_OVERRIDE:
        weak = float(config.SIM_WEAK_OVERRIDE)
    if config.SIM_STRONG_OVERRIDE:
        strong = float(config.SIM_STRONG_OVERRIDE)
    return weak, max(strong, weak)
