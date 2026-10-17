"""Kokoro TTS for the talk page: one warm model behind 127.0.0.1, one sentence per call.

Runs in its own venv (kokoro-onnx + onnxruntime), not the voice venv, so the
voice service never loads a model. systemd unit voice-tts.service holds the
memory and CPU budget; this process holds one model and serialises synthesis,
so two tabs cannot double the CPU load on h.

  POST /tts {"text": "...", "voice": "bf_emma", "speed": 1.1} -> audio/wav (24 kHz mono 16-bit)
  GET  /health -> {"ok": true}

Env: VOICE_TTS_DIR (kokoro.onnx + voices.bin), VOICE_TTS_PORT (8078), VOICE_TTS_THREADS (3).
"""
import io
import json
import os
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import onnxruntime as ort
from kokoro_onnx import Kokoro

DIR = os.environ.get("VOICE_TTS_DIR", os.path.expanduser("~/.local/share/voice-tts"))
PORT = int(os.environ.get("VOICE_TTS_PORT", "8078"))
THREADS = int(os.environ.get("VOICE_TTS_THREADS", "3"))
MAX_CHARS = 400  # one sentence or two; the talk page splits longer replies
DEFAULT_VOICE = os.environ.get("VOICE_TTS_VOICE", "bf_emma")

_opts = ort.SessionOptions()
_opts.intra_op_num_threads = THREADS
_opts.inter_op_num_threads = 1
_session = ort.InferenceSession(os.path.join(DIR, "kokoro.onnx"), _opts,
                                providers=["CPUExecutionProvider"])
_kokoro = Kokoro.from_session(_session, os.path.join(DIR, "voices.bin"))
_lock = threading.Lock()


def synth(text: str, voice: str, speed: float) -> bytes:
    lang = "en-gb" if voice.startswith("b") else "en-us"
    with _lock:
        audio, sr = _kokoro.create(text, voice=voice, speed=speed, lang=lang)
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # one line per synthesis instead, below
        pass

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._json(200, {"ok": True, "voice": DEFAULT_VOICE, "threads": THREADS})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/tts":
            return self._json(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(min(n, 8192)) or b"{}")
            text = str(body.get("text", "")).strip()[:MAX_CHARS]
            voice = str(body.get("voice") or DEFAULT_VOICE)
            speed = min(max(float(body.get("speed", 1.1)), 0.7), 1.4)
            if not text:
                return self._json(400, {"error": "empty text"})
            t0 = time.time()
            wav = synth(text, voice, speed)
            took = time.time() - t0
        except Exception as e:
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        print(f"tts chars={len(text)} synth_ms={took * 1000:.0f} bytes={len(wav)}", flush=True)
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(wav)))
        self.send_header("X-Synth-Ms", f"{took * 1000:.0f}")
        self.end_headers()
        self.wfile.write(wav)


if __name__ == "__main__":
    synth("Ready.", DEFAULT_VOICE, 1.1)  # warm the graph before taking traffic
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
