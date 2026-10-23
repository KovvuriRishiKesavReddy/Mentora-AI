"""HTTP-level tests (need `pip install fastapi httpx`; skipped automatically otherwise)."""
import io
import unittest

try:
    from fastapi.testclient import TestClient
    HAVE = True
except Exception:
    HAVE = False

from tests.base import BaseCase, NOTES, OWNER_A, OWNER_B


@unittest.skipUnless(HAVE, "fastapi/httpx not installed")
class TestApi(BaseCase):
    def setUp(self):
        super().setUp()
        from app.main import app
        self.c = TestClient(app)                       # no `with`: skips the heavy startup (model download)
        self.h = {"X-Client-Id": OWNER_A}

    def test_auth_header_and_error_shape(self):
        r = self.c.get("/sessions")
        self.assertEqual(r.status_code, 401)
        self.assertIn("detail", r.json()); self.assertIn("code", r.json())

    def test_session_chat_refresh_and_isolation(self):
        sid = self.c.post("/sessions", headers=self.h).json()["session_id"]
        with open(NOTES, "rb") as f:
            up = self.c.post("/upload", headers=self.h, files={"file": ("../../evil/OS notes.docx", f)})
        self.assertEqual(up.status_code, 200, up.text)
        self.assertEqual(up.json()["filename"], "OS notes.docx")
        r = self.c.post("/chat", headers=self.h, json={"session_id": sid, "message": "Explain deadlock handling strategies"})
        self.assertEqual(r.status_code, 200, r.text)
        h = self.c.get(f"/sessions/{sid}", headers=self.h).json()
        self.assertEqual(len(h["messages"]), 2)
        self.assertEqual(self.c.get(f"/sessions/{sid}", headers={"X-Client-Id": OWNER_B}).status_code, 404)
        self.assertEqual(self.c.post("/chat", headers=self.h, json={"session_id": sid, "message": "  "}).status_code, 422)

    def test_upload_rejects_bad_type_and_content(self):
        r = self.c.post("/upload", headers=self.h, files={"file": ("x.exe", io.BytesIO(b"MZ"))})
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/upload", headers=self.h, files={"file": ("x.pdf", io.BytesIO(b"not a pdf"))})
        self.assertEqual(r.status_code, 400)

    def test_cors_not_wildcard(self):
        r = self.c.options("/chat", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
        self.assertNotEqual(r.headers.get("access-control-allow-origin"), "*")
        self.assertNotEqual(r.headers.get("access-control-allow-origin"), "http://evil.example")

    def test_study_endpoints(self):
        import json
        from app.services import llm
        def fake(system, messages, max_tokens=None):
            t = messages[0]["content"]
            if t.startswith("Summarise this part"):
                return "- point one\n- point two"
            if t.startswith("Below are chapter summaries"):
                return "Overview text.\nKEY TERMS: a, b"
            if t.startswith("Create exactly"):
                return json.dumps([{"front": "What is X?", "back": "It is Y."}])
            return "ok"
        llm.set_backend(fake)
        with open(NOTES, "rb") as f:
            doc = self.c.post("/upload", headers=self.h, files={"file": ("n.docx", f)}).json()["doc_id"]
        o = self.c.get(f"/outline/{doc}", headers=self.h)
        self.assertEqual(o.status_code, 200); self.assertGreaterEqual(len(o.json()["chapters"]), 11)
        head = "4. CPU Scheduling > Round Robin (RR)"
        p = self.c.get("/passage", headers=self.h, params={"doc_id": doc, "heading": head})
        self.assertEqual(p.status_code, 200); self.assertIn("quantum", p.json()["text"].lower())
        self.assertEqual(self.c.get("/passage", headers={"X-Client-Id": OWNER_B}, params={"doc_id": doc, "heading": head}).status_code, 404)
        self.assertEqual(self.c.get(f"/summary/{doc}", headers=self.h).status_code, 404)
        s = self.c.post("/summary", headers=self.h, json={"doc_id": doc})
        self.assertEqual(s.status_code, 200, s.text); self.assertTrue(s.json()["complete"])
        self.assertEqual(self.c.get(f"/summary/{doc}", headers=self.h).status_code, 200)
        f = self.c.post("/flashcards", headers=self.h, json={"doc_id": doc, "count": 3})
        self.assertEqual(f.status_code, 200, f.text); self.assertEqual(f.json()["cards"][0]["front"], "What is X?")
        self.assertEqual(self.c.post("/flashcards", headers=self.h, json={}).status_code, 400)
        r = self.c.get("/revision-plan", headers=self.h)
        self.assertEqual(r.status_code, 200); self.assertIn("summary", r.json())


if __name__ == "__main__":
    unittest.main()
