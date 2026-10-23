import glob
import math
import os
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

from app import config
from app.errors import ApiError
from app.security import RateLimiter, sanitize_filename, validate_owner, validate_upload_content
from app.services import voice
from tests.base import BaseCase


def make_wav(path, seconds=1.5, sr=16000, ch=1):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(ch); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes(b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 220 * i / sr))) * ch
                               for i in range(int(sr * seconds))))
    return Path(path).read_bytes()


def leftovers():
    return glob.glob(os.path.join(tempfile.gettempdir(), "mentora_audio_*"))


class TestAudio(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def code(self, data, mime="audio/webm"):
        with self.assertRaises(ApiError) as cm:
            voice.process_audio(data, mime)
        return cm.exception.status, cm.exception.code

    def test_valid_wav_mono_and_stereo_44k(self):
        for sr, ch in ((16000, 1), (44100, 2)):
            r = voice.process_audio(make_wav(self.tmp / "a.wav", 1.2, sr, ch), "audio/wav")
            self.assertEqual(r["format"], "wav")
            self.assertAlmostEqual(r["duration_s"], 1.2, delta=0.05)
            self.assertTrue(0 <= r["pitch_mean"] <= 1 and 0 <= r["speech_rate"] <= 1)
        self.assertEqual(leftovers(), [])

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_real_webm_opus_recording(self):
        p = self.tmp / "r.webm"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=2", "-c:a", "libopus", str(p)], check=True)
        r = voice.process_audio(p.read_bytes(), "audio/webm;codecs=opus")
        self.assertEqual(r["format"], "webm")
        self.assertAlmostEqual(r["duration_s"], 2.0, delta=0.15)
        self.assertEqual(leftovers(), [])                                  # temp files cleaned (success)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
    def test_corrupt_webm_clear_error_and_cleanup(self):
        bad = b"\x1a\x45\xdf\xa3" + os.urandom(3000)
        self.assertEqual(self.code(bad), (422, "corrupt_audio"))
        self.assertEqual(leftovers(), [])                                  # temp files cleaned (failure)

    def test_webm_without_ffmpeg_gives_actionable_503(self):
        with mock.patch("app.services.voice.shutil.which", return_value=None):
            st, code = self.code(b"\x1a\x45\xdf\xa3" + b"0" * 100)
        self.assertEqual((st, code), (503, "decoder_missing"))
        self.assertEqual(leftovers(), [])

    def test_empty_unsupported_oversize_long_short(self):
        self.assertEqual(self.code(b""), (400, "empty_audio"))
        self.assertEqual(self.code(b"hello this is not audio at all", "audio/x-foo"), (415, "unsupported_audio"))
        old = config.MAX_AUDIO_BYTES
        config.MAX_AUDIO_BYTES = 1000
        try:
            self.assertEqual(self.code(make_wav(self.tmp / "b.wav", 1.0), "audio/wav"), (413, "audio_too_large"))
        finally:
            config.MAX_AUDIO_BYTES = old
        old = config.MAX_AUDIO_SECONDS
        config.MAX_AUDIO_SECONDS = 1
        try:
            self.assertEqual(self.code(make_wav(self.tmp / "c.wav", 3.0, 8000), "audio/wav"), (413, "audio_too_long"))
        finally:
            config.MAX_AUDIO_SECONDS = old
        self.assertEqual(self.code(make_wav(self.tmp / "d.wav", 0.1), "audio/wav"), (422, "audio_too_short"))
        self.assertEqual(self.code(make_wav(self.tmp / "e.wav", 1)[:60], "audio/wav")[0], 422)   # truncated WAV
        self.assertEqual(leftovers(), [])

    def test_declared_mime_is_not_trusted(self):
        r = voice.process_audio(make_wav(self.tmp / "f.wav"), "audio/webm")     # WAV bytes declared as webm
        self.assertEqual(r["format"], "wav")

    def test_transcription_only_when_enabled(self):
        wav = make_wav(self.tmp / "g.wav")
        old = config.STT_PROVIDER
        config.STT_PROVIDER = "browser"            # force the default: a developer's .env may set groq
        try:
            self.assertIsNone(voice.process_audio(wav, "audio/wav")["transcript"])    # browser STT: no server call
        finally:
            config.STT_PROVIDER = old
        fake = mock.Mock()
        fake.audio.transcriptions.create.return_value = "explain round robin"
        config.STT_PROVIDER = "groq"
        try:
            with mock.patch("app.services.llm.get_client", return_value=fake):
                r = voice.process_audio(wav, "audio/wav")
        finally:
            config.STT_PROVIDER = old
        self.assertEqual(r["transcript"], "explain round robin")
        fake.audio.transcriptions.create.side_effect = RuntimeError("x")
        config.STT_PROVIDER = "groq"
        try:
            with mock.patch("app.services.llm.get_client", return_value=fake):
                r = voice.process_audio(wav, "audio/wav")                              # STT failure must not fail the request
        finally:
            config.STT_PROVIDER = old
        self.assertIsNone(r["transcript"]); self.assertEqual(r["stt_error"], "transcription_failed")


class TestSecurity(BaseCase):
    def test_filename_sanitising(self):
        ok = {".pdf", ".docx", ".txt"}
        self.assertEqual(sanitize_filename("../../etc/passwd.txt", ok), "passwd.txt")
        self.assertEqual(sanitize_filename("C:\\Users\\x\\notes.docx", ok), "notes.docx")
        self.assertEqual(sanitize_filename("my notes (v2).PDF", ok), "my notes (v2).pdf")
        self.assertNotIn("/", sanitize_filename("a/b\x00c.txt", ok))
        for bad in ("evil.exe", "noext", "x.docx.exe", ""):
            with self.assertRaises(ApiError):
                sanitize_filename(bad, ok)

    def test_content_validation(self):
        d = self.tmp
        cases = {("fake.pdf", b"MZ\x90 not a pdf"): ".pdf", ("fake.docx", b"PK\x03\x04junk"): ".docx",
                 ("bin.txt", b"abc\x00def"): ".txt", ("empty.txt", b""): ".txt"}
        for (name, content), ext in cases.items():
            p = d / name
            p.write_bytes(content)
            with self.assertRaises(ApiError, msg=name):
                validate_upload_content(p, ext)
        (d / "ok.txt").write_text("hello", encoding="utf-8")
        validate_upload_content(d / "ok.txt", ".txt")
        from tests.base import NOTES
        validate_upload_content(NOTES, ".docx")

    def test_owner_validation_and_rate_limit(self):
        for bad in (None, "", "short", "x" * 100, "bad id with spaces!!!!!!!"):
            with self.assertRaises(ApiError) as cm:
                validate_owner(bad)
            self.assertEqual(cm.exception.status, 401)
        self.assertEqual(validate_owner("a" * 32), "a" * 32)
        rl = RateLimiter()
        for i in range(3):
            rl.check("o", "chat", 3, 60, now=100 + i)
        with self.assertRaises(ApiError) as cm:
            rl.check("o", "chat", 3, 60, now=103)
        self.assertEqual(cm.exception.status, 429)
        rl.check("o", "chat", 3, 60, now=170)            # window passed
        rl.check("other", "chat", 3, 60, now=103)        # independent per owner

    def test_config_is_not_wildcard_and_no_secrets_shipped(self):
        self.assertNotIn("*", config.CORS_ORIGINS)
        root = Path(__file__).resolve().parent.parent.parent
        # (a local .env is fine on your own machine; the shipped zip simply does not include one)
        self.assertIn("GROQ_API_KEY=", (root / ".env.example").read_text())
        for f in list(root.glob("*.bat")) + [p for p in root.rglob("*.py") if "tests" not in p.parts]:
            txt = f.read_text(errors="ignore")
            self.assertNotRegex(txt, r"gsk_[A-Za-z0-9]{20,}", f.name)     # real-key-shaped strings only


if __name__ == "__main__":
    unittest.main()
