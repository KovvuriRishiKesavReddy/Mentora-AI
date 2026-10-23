"""Owner identification, rate limiting, filename and upload-content validation."""
import re
import threading
import time
import unicodedata
import zipfile
from collections import defaultdict, deque
from pathlib import Path

from app.errors import ApiError

# ── Owner (anonymous per-browser identity) ────────────────────────────────────
# The app has no login. Each browser generates a random 128-bit id, keeps it in localStorage and
# sends it as X-Client-Id. Every session/document/quiz row is bound to that id, so a guessed or
# edited session id from another browser is rejected (reported as "not found").
_OWNER_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


def validate_owner(value) -> str:
    if not value or not _OWNER_RE.match(value):
        raise ApiError(401, "Missing or invalid X-Client-Id header.", "bad_client_id")
    return value


# ── Rate limiting (in-memory, per owner+bucket) ───────────────────────────────
class RateLimiter:
    def __init__(self):
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, owner: str, bucket: str, limit: int, window_s: float = 60.0, now=None):
        now = time.monotonic() if now is None else now
        key = (owner, bucket)
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window_s:
                q.popleft()
            if len(q) >= limit:
                retry = max(1, int(window_s - (now - q[0])))
                raise ApiError(429, f"Too many requests. Try again in {retry}s.", "rate_limited")
            q.append(now)

    def reset(self):
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


# ── Filenames ─────────────────────────────────────────────────────────────────
def sanitize_filename(name: str, allowed_ext: set) -> str:
    """Return a safe basename. Never trusts any directory component supplied by the client."""
    name = unicodedata.normalize("NFKC", name or "")
    name = name.replace("\\", "/").split("/")[-1]            # drop any path
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().strip(".")
    ext = Path(name).suffix.lower()
    if ext not in allowed_ext:
        raise ApiError(400, f"Unsupported file type '{ext or 'none'}'. Use PDF, DOCX or TXT.", "bad_extension")
    stem = re.sub(r"[^\w .()\-]", "_", Path(name).stem, flags=re.UNICODE).strip(" ._")
    stem = re.sub(r"\s+", " ", stem)[:100] or "notes"
    return f"{stem}{ext}"


# ── Upload content validation (never trust extension / MIME alone) ────────────
def validate_upload_content(path: Path, ext: str) -> None:
    with open(path, "rb") as f:
        head = f.read(4096)
    if not head:
        raise ApiError(400, "The uploaded file is empty.", "empty_file")
    if ext == ".pdf":
        if not head.lstrip().startswith(b"%PDF"):
            raise ApiError(400, "File content is not a valid PDF.", "bad_content")
    elif ext == ".docx":
        if not head.startswith(b"PK"):
            raise ApiError(400, "File content is not a valid DOCX.", "bad_content")
        try:
            with zipfile.ZipFile(path) as z:
                if "word/document.xml" not in z.namelist():
                    raise ApiError(400, "File content is not a valid DOCX.", "bad_content")
        except zipfile.BadZipFile:
            raise ApiError(400, "File content is not a valid DOCX.", "bad_content")
    elif ext == ".txt":
        if b"\x00" in head:
            raise ApiError(400, "File content is not plain text.", "bad_content")
        try:
            head.decode("utf-8")
        except UnicodeDecodeError as e:
            # a multi-byte char cut at the 4 KiB boundary is fine; anything earlier is not UTF-8
            if e.start < len(head) - 4:
                try:
                    head.decode("latin-1")
                except Exception:
                    raise ApiError(400, "File content is not plain text.", "bad_content")
