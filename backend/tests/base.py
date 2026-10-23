"""Shared test fixture: isolated temp data dir, in-memory vector store, offline hashing embedder, scripted LLM."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app import config, db
from app.security import limiter
from app.services import embedding, llm, rag, vectorstore
from app.services.vectorstore import MemoryStore

HERE = Path(__file__).parent
NOTES = HERE / "os_notes.docx"
OWNER_A = "a" * 32
OWNER_B = "b" * 32


class FakeLLM:
    """Scripted stand-in for Groq. Records every call so tests can inspect exactly what the model was sent."""
    def __init__(self):
        self.calls = []
        self.fail = False
        self.rewrite = None         # callable(message, active_topic) -> standalone ; None => invalid JSON (forces heuristic)
        self.chat_reply = "Here is the explanation."

    def __call__(self, system, messages, max_tokens=None):
        self.calls.append({"system": system, "messages": messages})
        if self.fail:
            raise llm.LLMError("upstream", "boom")
        if system.startswith("You rewrite questions"):
            text = messages[0]["content"]
            msg = text.split("LAST MESSAGE:", 1)[1].strip()
            topic = text.split("CURRENT TOPIC:", 1)[1].split("\n", 1)[0].strip()
            if self.rewrite is None:
                return "not json"
            return json.dumps({"standalone": self.rewrite(msg, topic), "topic": topic})
        return self.chat_reply

    def chat_calls(self):
        return [c for c in self.calls if not c["system"].startswith(("You rewrite", "You are a concise summariser"))]


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mentora_test_"))
        self._old = (config.UPLOAD_DIR, config.DATA_DIR)
        config.UPLOAD_DIR = self.tmp / "uploads"
        config.DATA_DIR = self.tmp / "data"
        config.UPLOAD_DIR.mkdir()
        db.configure(self.tmp / "data" / "t.db")
        embedding.set_embedder(embedding.HashingEmbedder())
        vectorstore.set_store(MemoryStore())
        self.fake = FakeLLM()
        llm.set_backend(self.fake)
        limiter.reset()

    def tearDown(self):
        llm.set_backend(None)
        config.UPLOAD_DIR, config.DATA_DIR = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers
    def upload(self, owner, src, name=None):
        name = name or Path(src).name
        dst = rag.owner_dir(owner)
        dst.mkdir(parents=True, exist_ok=True)
        p = dst / ("x_" + name)
        shutil.copy(src, p)
        return rag.ingest_file(owner, p, name)

    def upload_text(self, owner, name, text):
        p = self.tmp / name
        p.write_text(text, encoding="utf-8")
        return self.upload(owner, p, name)
