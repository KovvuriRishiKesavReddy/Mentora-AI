"""Audio pipeline.

IMPORTANT — what this does and does not do:
  * Feature extraction (pitch, speech-rate proxy, pauses) feeds the *mood detector*. It is NOT speech-to-text.
  * Speech-to-text is separate (see `transcribe`, STT_PROVIDER). By default the browser's Web Speech API
    transcribes while the student speaks; STT_PROVIDER=groq additionally lets the server transcribe the
    recording through Groq's Whisper endpoint (audio is then sent to Groq — opt-in, same API key).

Root cause of the old "Format not recognised" error: the browser records WebM/Opus; libsndfile/soundfile cannot
read WebM, and the old code silently fell back to it whenever ffmpeg was missing or failed. The frontend now
converts the recording to 16 kHz mono WAV in the browser, so no ffmpeg is needed for the normal path; WebM/Ogg/MP3/MP4
uploads are still accepted if ffmpeg is installed, otherwise a clear 503 explains what to install.
"""
import io
import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

from app import config
from app.errors import ApiError

TARGET_SR = 16000
_EXT = {"wav": ".wav", "webm": ".webm", "ogg": ".ogg", "mp3": ".mp3", "mp4": ".m4a"}


def sniff_format(data: bytes):
    """Identify the container from magic bytes (the declared MIME type is not trusted)."""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"            # Matroska/WebM (EBML)
    if data[:4] == b"OggS":
        return "ogg"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return "mp3"
    if data[4:8] == b"ftyp":
        return "mp4"
    return None


def _read_wav(path: Path):
    with wave.open(str(path), "rb") as w:
        ch, width, sr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        if n == 0:
            raise ApiError(422, "The recording contains no audio.", "empty_audio")
        if n / sr > config.MAX_AUDIO_SECONDS + 1:
            raise ApiError(413, f"Recording is longer than {config.MAX_AUDIO_SECONDS}s.", "audio_too_long")
        raw = w.readframes(n)
    if width == 2:
        y = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 1:
        y = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 4:
        y = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        y = ((b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16)) << 8 >> 8)
        y = y.astype(np.float32) / 8388608.0
    else:
        raise wave.Error("unsupported sample width")
    if ch > 1:
        y = y[: len(y) // ch * ch].reshape(-1, ch).mean(axis=1)
    return y, sr


def _ffmpeg_to_wav(src: Path, dst: Path) -> None:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise ApiError(503, "Decoding this audio format needs ffmpeg, which is not installed on the server. "
                            "Install ffmpeg (https://ffmpeg.org/download.html) and restart, or use the web UI "
                            "(it uploads WAV, which needs no ffmpeg).", "decoder_missing")
    try:
        r = subprocess.run([exe, "-nostdin", "-v", "error", "-i", str(src), "-t", str(config.MAX_AUDIO_SECONDS + 5),
                            "-ar", str(TARGET_SR), "-ac", "1", "-f", "wav", "-y", str(dst)],
                           capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        raise ApiError(422, "Decoding the audio timed out.", "decode_timeout")
    if r.returncode != 0 or not dst.exists() or dst.stat().st_size < 100:
        raise ApiError(422, "The audio could not be decoded — the recording may be corrupted.", "corrupt_audio")


def _resample(y: np.ndarray, sr: int) -> np.ndarray:
    if sr == TARGET_SR or len(y) == 0:
        return y
    n = int(round(len(y) * TARGET_SR / sr))
    return np.interp(np.linspace(0, len(y) - 1, n), np.arange(len(y)), y).astype(np.float32)


def decode_audio(data: bytes, mime_type: str = "") -> tuple:
    """-> (samples float32 mono @16 kHz, format, decoder). Raises ApiError with a precise HTTP status."""
    if not data:
        raise ApiError(400, "The recording is empty.", "empty_audio")
    if len(data) > config.MAX_AUDIO_BYTES:
        raise ApiError(413, f"Audio file is larger than {config.MAX_AUDIO_BYTES // (1024 * 1024)} MB.", "audio_too_large")
    fmt = sniff_format(data)
    if fmt is None:
        raise ApiError(415, f"Unsupported audio format (declared type: {mime_type or 'unknown'}). "
                            "Use WAV, WebM, Ogg, MP3 or MP4/M4A audio.", "unsupported_audio")
    with tempfile.TemporaryDirectory(prefix="mentora_audio_") as tmp:      # always removed, success or failure
        src = Path(tmp) / f"in{_EXT[fmt]}"
        src.write_bytes(data)
        decoder = "builtin"
        if fmt == "wav":
            try:
                y, sr = _read_wav(src)
            except ApiError:
                raise
            except Exception:                       # e.g. float / extensible WAV: let ffmpeg try
                dst = Path(tmp) / "out.wav"
                _ffmpeg_to_wav(src, dst)
                y, sr = _read_wav(dst)
                decoder = "ffmpeg"
        else:
            dst = Path(tmp) / "out.wav"
            _ffmpeg_to_wav(src, dst)
            y, sr = _read_wav(dst)
            decoder = "ffmpeg"
    return _resample(y, sr), fmt, decoder


def _defaults() -> dict:
    return {"pitch_mean": 0.5, "pitch_variance": 0.3, "speech_rate": 0.5, "pause_count": 0}


def extract_features(y: np.ndarray, sr: int = TARGET_SR) -> dict:
    try:
        import librosa
    except ImportError:
        librosa = None
    pm, pv, sr_, pc = 0.5, 0.3, 0.5, 0
    if librosa is not None:
        try:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                f0, voiced, _ = librosa.pyin(y, fmin=librosa.note_to_hz("C2"), fmax=librosa.note_to_hz("C7"), sr=sr)
            f0v = f0[voiced] if voiced is not None else np.array([])
            f0v = f0v[~np.isnan(f0v)] if len(f0v) else np.array([])
            if len(f0v):
                pm = float(np.clip((np.mean(f0v) - 80) / 320, 0, 1))
                pv = float(np.clip(np.var(f0v) / 8000, 0, 1))
        except Exception:
            pass
        try:
            zcr = librosa.feature.zero_crossing_rate(y)[0]
            sr_ = float(np.clip(np.mean(zcr) * 1000 / 150, 0, 1))
            rms = librosa.feature.rms(y=y)[0]
        except Exception:
            rms = None
    else:
        zc = np.mean(np.abs(np.diff(np.signbit(y).astype(np.int8))))
        sr_ = float(np.clip(zc * 1000 / 150, 0, 1))
        frame = 512
        n = len(y) // frame
        rms = np.sqrt(np.mean(y[: n * frame].reshape(n, frame) ** 2, axis=1)) if n else None
    if rms is not None and len(rms):
        sil = rms < float(np.mean(rms)) * 0.15
        pc = int(np.sum(np.diff(sil.astype(int)) == 1))
    return {"pitch_mean": round(pm, 3), "pitch_variance": round(pv, 3), "speech_rate": round(sr_, 3), "pause_count": pc}


def transcribe(data: bytes, fmt: str):
    """Server-side speech-to-text via Groq Whisper (only when STT_PROVIDER=groq). Returns (text|None, error|None)."""
    if config.STT_PROVIDER != "groq":
        return None, None
    try:
        from app.services.llm import get_client
        client = get_client()
        r = client.audio.transcriptions.create(file=(f"speech{_EXT.get(fmt, '.wav')}", data),
                                               model=config.WHISPER_MODEL, response_format="text")
        text = (r if isinstance(r, str) else getattr(r, "text", "")).strip()
        return (text or None), (None if text else "no_speech_detected")
    except Exception as e:
        print(f"[voice] transcription failed: {type(e).__name__}")
        return None, "transcription_failed"


def process_audio(data: bytes, mime_type: str = "") -> dict:
    size = len(data or b"")
    try:
        y, fmt, decoder = decode_audio(data, mime_type)
    except ApiError as e:
        print(f"[voice] rejected mime={mime_type or '?'} bytes={size} reason={e.code}")
        raise
    duration = len(y) / TARGET_SR
    if duration < 0.3:
        print(f"[voice] rejected mime={mime_type or '?'} bytes={size} reason=too_short")
        raise ApiError(422, "The recording is too short to analyse (under 0.3 s).", "audio_too_short")
    feats = extract_features(y)
    transcript, stt_error = transcribe(data, fmt)
    print(f"[voice] ok mime={mime_type or '?'} sniffed={fmt} bytes={size} duration={duration:.1f}s decoder={decoder}")
    return {**feats, "duration_s": round(duration, 2), "format": fmt, "transcript": transcript,
            "stt": config.STT_PROVIDER, "stt_error": stt_error}
