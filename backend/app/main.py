# backend/app/main.py — thin HTTP layer. Business logic lives in chat_service.py and services/*.
import os
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
except Exception:
    pass

from fastapi import Depends, FastAPI, File, Header, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import chat_service, config, db
from app.errors import ApiError
from app.schemas import (AudioResponse, ChatRequest, ChatResponse, FilesResponse, FlashcardRequest, MoodHistoryResponse,
                         QuizGenerateRequest, QuizGenerateResponse, QuizGradeRequest, QuizGradeResponse,
                         SessionResponse, SummaryRequest, UploadResponse)
from app.security import limiter, sanitize_filename, validate_owner, validate_upload_content
from app.services import gap_tracker, memory, quiz, rag, revision, study, voice
from app.services.llm import LLMError


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[startup] GROQ key: {'SET' if os.environ.get('GROQ_API_KEY') else 'MISSING (chat disabled until set in .env)'}")
    print(f"[startup] model={config.GROQ_MODEL} fallbacks={config.GROQ_FALLBACK_MODELS} stt={config.STT_PROVIDER}")
    from app.services import embedding
    embedding.get_embedder()
    from app.services.vectorstore import get_store
    get_store()
    n = rag.reindex_missing()
    if n:
        print(f"[startup] re-indexed {n} document(s) from stored files")
    from app.services.mood import _load_embedder, _load_refs
    _load_embedder(); _load_refs()
    print("[startup] ready")
    yield


app = FastAPI(title="MentoraAI", version="3.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_credentials=False,
                   allow_methods=["GET", "POST", "DELETE", "OPTIONS"], allow_headers=["Content-Type", "X-Client-Id"])


# ── consistent error bodies: {"detail": "...", "code": "..."} ─────────────────────────────────────────
@app.exception_handler(ApiError)
async def _api_error(_: Request, e: ApiError):
    return JSONResponse({"detail": e.detail, "code": e.code}, status_code=e.status)


@app.exception_handler(LLMError)
async def _llm_error(_: Request, e: LLMError):
    ae = chat_service.llm_error_to_api(e)
    return JSONResponse({"detail": ae.detail, "code": ae.code}, status_code=ae.status)


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, e: RequestValidationError):
    first = e.errors()[0] if e.errors() else {}
    loc = ".".join(str(x) for x in first.get("loc", []) if x != "body")
    return JSONResponse({"detail": f"Invalid request: {loc} {first.get('msg', '')}".strip(), "code": "invalid_request"},
                        status_code=422)


@app.exception_handler(Exception)
async def _unhandled(_: Request, e: Exception):
    print(f"[error] unhandled {type(e).__name__}")        # type only: never log request content or secrets
    return JSONResponse({"detail": "Internal server error.", "code": "internal_error"}, status_code=500)


def owner_dep(x_client_id: str = Header(default=None)) -> str:
    return validate_owner(x_client_id)


# ── health ───────────────────────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "groq_key_set": bool(os.environ.get("GROQ_API_KEY")),
            "stt_provider": config.STT_PROVIDER, "max_audio_seconds": config.MAX_AUDIO_SECONDS}


# ── sessions ─────────────────────────────────────────────────────────────────────────────────────────
@app.post("/sessions", response_model=SessionResponse)
@app.get("/session/new", response_model=SessionResponse)         # legacy path kept
def new_session(owner: str = Depends(owner_dep)):
    return {"session_id": memory.create_session(owner)}


@app.get("/sessions")
def list_sessions(owner: str = Depends(owner_dep)):
    return {"sessions": memory.list_sessions(owner)}


@app.get("/sessions/{session_id}")
def get_session(session_id: str, owner: str = Depends(owner_dep)):
    s = memory.get_session(owner, session_id)
    return {"session": {k: s[k] for k in ("id", "title", "created_at", "updated_at", "active_topic")},
            "messages": memory.get_messages(owner, session_id),
            "mood_history": memory.get_mood_history(owner, session_id)}


@app.delete("/sessions/{session_id}")
@app.delete("/session/{session_id}")                              # legacy path kept
def delete_session(session_id: str, owner: str = Depends(owner_dep)):
    if not memory.delete_session(owner, session_id):
        raise ApiError(404, "Session not found.", "session_not_found")
    return {"deleted": True}


# ── documents ────────────────────────────────────────────────────────────────────────────────────────
@app.post("/upload", response_model=UploadResponse)
def upload_file(file: UploadFile = File(...), owner: str = Depends(owner_dep)):
    limiter.check(owner, "upload", config.UPLOAD_RATE_PER_MIN)
    filename = sanitize_filename(file.filename or "", config.ALLOWED_EXTENSIONS)
    ext = Path(filename).suffix.lower()
    odir = rag.owner_dir(owner)
    odir.mkdir(parents=True, exist_ok=True)
    tmp_path = odir / f".upload_{uuid.uuid4().hex}.part"
    final_path, registered = None, False
    try:
        size = 0
        with open(tmp_path, "wb") as out:                          # stream with a hard size cap
            while True:
                chunk = file.file.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > config.MAX_UPLOAD_BYTES:
                    raise ApiError(413, f"File is larger than {config.MAX_UPLOAD_BYTES // (1024 * 1024)} MB.", "file_too_large")
                out.write(chunk)
        validate_upload_content(tmp_path, ext)
        final_path = odir / f"{rag._sha256_file(tmp_path)[:12]}_{filename}"     # content-addressed: no accidental overwrite
        os.replace(tmp_path, final_path)
        result = rag.ingest_file(owner, final_path, filename)
        registered = result["status"] != "duplicate"
        if not registered and final_path.exists():
            final_path.unlink()
        return {"success": True, **{k: result[k] for k in ("status", "doc_id", "filename", "chunks_created", "chapters_detected")}}
    except ApiError:
        raise
    except Exception as e:
        print(f"[upload] failed: {type(e).__name__}")
        raise ApiError(500, "Could not process this file.", "ingest_failed")
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
        if final_path is not None and not registered and final_path.exists() and not rag.is_referenced(final_path):
            final_path.unlink()                                   # failed ingest: leave no orphan file behind


@app.get("/files", response_model=FilesResponse)
def list_files(owner: str = Depends(owner_dep)):
    return {"files": rag.list_files(owner)}


@app.delete("/files/{doc_id}")
def delete_file(doc_id: str, owner: str = Depends(owner_dep)):
    if not rag.delete_file(owner, doc_id):
        raise ApiError(404, "Document not found.", "document_not_found")
    return {"deleted": True, "doc_id": doc_id}


# ── study tools ──────────────────────────────────────────────────────────────────────────────────────
@app.get("/outline/{doc_id}")
def doc_outline(doc_id: str, owner: str = Depends(owner_dep)):
    return study.outline(owner, doc_id)


@app.get("/passage")
def doc_passage(doc_id: str, heading: str, owner: str = Depends(owner_dep)):
    if len(heading) > 300 or len(doc_id) > 64:
        raise ApiError(400, "Invalid request.", "invalid_request")
    return study.get_passage(owner, doc_id, heading)


@app.get("/summary/{doc_id}")
def get_summary(doc_id: str, owner: str = Depends(owner_dep)):
    cached = study.get_cached_summary(owner, doc_id)
    if not cached:
        raise ApiError(404, "No summary has been generated for this document yet.", "summary_not_found")
    return cached


@app.post("/summary")
def make_summary(req: SummaryRequest, owner: str = Depends(owner_dep)):
    limiter.check(owner, "summary", 3)                            # expensive: many model calls
    return study.summarize_document(owner, req.doc_id, req.refresh)


@app.post("/flashcards")
def make_flashcards(req: FlashcardRequest, owner: str = Depends(owner_dep)):
    limiter.check(owner, "chat", config.CHAT_RATE_PER_MIN)
    return study.generate_flashcards(owner, req.doc_id, req.topic, req.count)


@app.get("/revision-plan")
def revision_plan(owner: str = Depends(owner_dep)):
    return revision.build_plan(owner)


# ── audio ────────────────────────────────────────────────────────────────────────────────────────────
@app.post("/audio", response_model=AudioResponse)
def process_audio(file: UploadFile = File(...), owner: str = Depends(owner_dep)):
    limiter.check(owner, "audio", config.AUDIO_RATE_PER_MIN)
    data = file.file.read(config.MAX_AUDIO_BYTES + 1)             # bounded read
    return voice.process_audio(data, file.content_type or "")


# ── chat ─────────────────────────────────────────────────────────────────────────────────────────────
@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, owner: str = Depends(owner_dep)):
    limiter.check(owner, "chat", config.CHAT_RATE_PER_MIN)
    return chat_service.handle_chat(
        owner, req.session_id, req.message, req.response_latency_ms, req.backspace_count, req.attempt_number,
        req.audio_features.model_dump() if req.audio_features else None)


# ── quiz ─────────────────────────────────────────────────────────────────────────────────────────────
@app.post("/quiz/generate", response_model=QuizGenerateResponse)
def generate_quiz(req: QuizGenerateRequest, owner: str = Depends(owner_dep)):
    limiter.check(owner, "chat", config.CHAT_RATE_PER_MIN)
    return quiz.generate_quiz(owner, req.session_id, req.topic, req.question_type, req.num_questions, req.difficulty)


@app.post("/quiz/grade", response_model=QuizGradeResponse)
def grade_quiz(req: QuizGradeRequest, owner: str = Depends(owner_dep)):
    limiter.check(owner, "chat", config.CHAT_RATE_PER_MIN)
    return quiz.grade_answer(owner, req.quiz_id, req.question_index, req.student_answer)


# ── analytics (owner-wide mastery; per-session mood) ─────────────────────────────────────────────────
@app.get("/mood/history/{session_id}", response_model=MoodHistoryResponse)
def mood_history(session_id: str, owner: str = Depends(owner_dep)):
    return memory.get_mood_history(owner, session_id)


@app.get("/gaps")
def get_gaps(owner: str = Depends(owner_dep)):
    return {"weak_topics": gap_tracker.get_weak_topics(owner), "all_topics": gap_tracker.get_topic_stats(owner)}


@app.get("/gaps/next/{session_id}")
def next_revision(session_id: str, owner: str = Depends(owner_dep)):
    return {"next_topic": gap_tracker.get_next_revision_topic(owner)}


@app.get("/gaps/{session_id}")
def get_gaps_legacy(session_id: str, owner: str = Depends(owner_dep)):
    return {"session_id": session_id, "weak_topics": gap_tracker.get_weak_topics(owner),
            "all_topics": gap_tracker.get_topic_stats(owner)}


@app.get("/analytics/{session_id}")
def analytics(session_id: str, owner: str = Depends(owner_dep)):
    stats = gap_tracker.get_topic_stats(owner)
    return {"session_id": session_id,
            "mood_summary": memory.get_mood_history(owner, session_id),
            "topic_stats": stats,
            "weak_topics": gap_tracker.get_weak_topics(owner),
            "mastered_topics": [t for t in stats if t["status"] == "mastered"],
            "files_indexed": len(rag.list_files(owner)),
            "total_chunks": sum(f["chunks"] for f in rag.list_files(owner))}
