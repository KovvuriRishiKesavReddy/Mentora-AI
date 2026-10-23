"""Central configuration. Every tunable is read from the environment (.env) with a safe default.
No secrets live in this file."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _list(name: str, default: str) -> list:
    return [x.strip() for x in os.environ.get(name, default).split(",") if x.strip()]


# ── Storage ───────────────────────────────────────────────────────────────────
DATA_DIR    = Path(os.environ.get("MENTORA_DATA_DIR", BASE_DIR / "data"))
UPLOAD_DIR  = Path(os.environ.get("MENTORA_UPLOAD_DIR", BASE_DIR / "uploads"))
DB_PATH     = DATA_DIR / "mentora.db"
CHROMA_DIR  = DATA_DIR / "chroma_db"

# ── LLM (Groq) ────────────────────────────────────────────────────────────────
# The key is read from the environment at call time (see services/llm.py); never hard-coded.
GROQ_MODEL           = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_FALLBACK_MODELS = _list("GROQ_FALLBACK_MODELS", "openai/gpt-oss-20b")
LLM_TIMEOUT_S        = _float("LLM_TIMEOUT_S", 45.0)
LLM_MAX_TOKENS       = _int("LLM_MAX_TOKENS", 1600)
REASONING_EFFORT     = os.environ.get("REASONING_EFFORT", "low")   # gpt-oss models only: low | medium | high
REASONING_HEADROOM   = _int("REASONING_HEADROOM", 1200)           # extra tokens so hidden reasoning cannot starve the answer

# ── RAG ───────────────────────────────────────────────────────────────────────
EMBED_MODEL          = os.environ.get("EMBED_MODEL", "all-MiniLM-L6-v2")
EMBEDDER_MODE        = os.environ.get("MENTORA_EMBEDDER", "auto")   # auto | minilm | hashing
CHUNK_SIZE_CHARS     = _int("CHUNK_SIZE_CHARS", 900)
CHUNK_OVERLAP_CHARS  = _int("CHUNK_OVERLAP_CHARS", 120)
RAG_TOP_K            = _int("RAG_TOP_K", 6)
RAG_BROAD_TOP_K      = _int("RAG_BROAD_TOP_K", 24)
CONTEXT_CHAR_BUDGET        = _int("CONTEXT_CHAR_BUDGET", 7000)
BROAD_CONTEXT_CHAR_BUDGET  = _int("BROAD_CONTEXT_CHAR_BUDGET", 14000)
# True cosine-similarity thresholds (similarity = 1 - cosine_distance). Optional overrides;
# when unset each embedder supplies its own defaults (see services/embedding.py).
SIM_WEAK_OVERRIDE    = os.environ.get("RAG_SIM_WEAK")
SIM_STRONG_OVERRIDE  = os.environ.get("RAG_SIM_STRONG")

# ── Conversation memory ───────────────────────────────────────────────────────
MEMORY_CONTEXT_WINDOW = _int("MEMORY_CONTEXT_WINDOW", 12)     # messages sent to the LLM
HISTORY_CHAR_BUDGET   = _int("HISTORY_CHAR_BUDGET", 7000)
HISTORY_MSG_CHAR_CAP  = _int("HISTORY_MSG_CHAR_CAP", 2500)
SUMMARIZE_TRIGGER     = _int("SUMMARIZE_TRIGGER", 20)         # unsummarised old messages before summarising

# ── Study tools ───────────────────────────────────────────────────────────────
SUMMARY_PIECE_CHARS = _int("SUMMARY_PIECE_CHARS", 6000)    # text sent to the model per summarisation call
SUMMARY_MAX_CALLS   = _int("SUMMARY_MAX_CALLS", 40)         # upper bound on LLM calls per document (pieces grow if needed)
FLASHCARD_MAX       = _int("FLASHCARD_MAX", 20)

# ── Uploads / audio limits ────────────────────────────────────────────────────
MAX_UPLOAD_BYTES  = _int("MAX_UPLOAD_MB", 25) * 1024 * 1024
MAX_AUDIO_BYTES   = _int("MAX_AUDIO_MB", 10) * 1024 * 1024
MAX_AUDIO_SECONDS = _int("MAX_AUDIO_SECONDS", 60)
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}
STT_PROVIDER      = os.environ.get("STT_PROVIDER", "browser").lower()   # browser | groq | none
WHISPER_MODEL     = os.environ.get("WHISPER_MODEL", "whisper-large-v3-turbo")

# ── API ───────────────────────────────────────────────────────────────────────
CORS_ORIGINS = _list("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
CHAT_RATE_PER_MIN   = _int("CHAT_RATE_PER_MIN", 30)
UPLOAD_RATE_PER_MIN = _int("UPLOAD_RATE_PER_MIN", 10)
AUDIO_RATE_PER_MIN  = _int("AUDIO_RATE_PER_MIN", 20)

# ── Mood ──────────────────────────────────────────────────────────────────────
MOOD_LABELS = ["frustrated", "confused", "confident", "bored", "neutral"]
MOOD_COLORS = {
    "frustrated": "#d62839", "confused": "#f08c00",
    "confident": "#2f9e44", "bored": "#868e96", "neutral": "#1f8a8a",
}
MOOD_EMOJI = {
    "frustrated": "😤", "confused": "🤔",
    "confident": "💪", "bored": "😐", "neutral": "🙂",
}
