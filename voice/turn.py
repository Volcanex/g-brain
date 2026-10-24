"""Local live-call endpointer: stateful Silero VAD and smart-turn v3.2 on CPU.

Only sessions are shared between calls; audio, VAD context and LSTM state are not.
Whisper features follow pipecat-ai/smart-turn's eight-second inference recipe.
"""
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import urllib.request
from functools import lru_cache
from math import gcd

import numpy as np


_MODELS = {
    "silero_vad.onnx": (
        "https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx",
        "1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3",
    ),
    "smart-turn-v3.2-cpu.onnx": (
        "https://huggingface.co/pipecat-ai/smart-turn-v3/resolve/main/smart-turn-v3.2-cpu.onnx",
        "2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f",
    ),
}
_SESSIONS = None
MAX_UTTERANCE_S = 120
_SESSION_LOCK = threading.Lock()
_SR = np.array(16000, dtype=np.int64)


def ensure_models() -> dict[str, Path]:
    """Fetch missing models atomically, refusing files with an unexpected digest."""
    root = Path(os.path.expanduser(os.environ.get(
        "VOICE_LIVE_MODELS", "~/.local/share/voice-live")))
    root.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, (url, expected) in _MODELS.items():
        path = root / name
        if path.exists():
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != expected:
                raise ValueError(f"SHA-256 mismatch for {path}")
        else:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=root, delete=False) as out:
                    temporary = Path(out.name)
                    digest = hashlib.sha256()
                    with urllib.request.urlopen(url, timeout=60) as response:
                        while chunk := response.read(1024 * 1024):
                            digest.update(chunk)
                            out.write(chunk)
                if digest.hexdigest() != expected:
                    raise ValueError(f"SHA-256 mismatch downloading {name}")
                temporary.chmod(0o644)
                temporary.replace(path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        paths[name] = path
    return paths


def _sessions():
    global _SESSIONS
    with _SESSION_LOCK:
        if _SESSIONS is None:
            import onnxruntime as ort

            paths = ensure_models()
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = 1
            opts.inter_op_num_threads = 1
            opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            _SESSIONS = tuple(ort.InferenceSession(
                str(paths[name]), sess_options=opts, providers=["CPUExecutionProvider"]
            ) for name in _MODELS)
    return _SESSIONS


@lru_cache(maxsize=1)
def _whisper_constants():
    # Slaney's linear scale below 1 kHz, logarithmic scale above it.
    mel_max = 15 + np.log(8) / (np.log(6.4) / 27)
    mels = np.linspace(0, mel_max, 82)
    hz = mels * (200 / 3)
    log = mels >= 15
    hz[log] = 1000 * np.exp((mels[log] - 15) * (np.log(6.4) / 27))
    frequencies = np.linspace(0, 8000, 201)
    ramps = hz[:, None] - frequencies[None, :]
    widths = np.diff(hz)
    triangles = np.maximum(0, np.minimum(
        -ramps[:-2] / widths[:-1, None], ramps[2:] / widths[1:, None]))
    filters = triangles * (2 / (hz[2:] - hz[:-2]))[:, None]
    return np.hanning(401)[:-1], filters


def _whisper_features(audio: np.ndarray) -> np.ndarray:
    """16 kHz float audio -> float32 (1, 80, 800), without torch/transformers."""
    audio = np.asarray(audio[-128000:], dtype=np.float32)
    audio = np.pad(audio, (128000 - len(audio), 0))
    audio = (audio - audio.mean()) / np.sqrt(audio.var() + 1e-7)
    window, filters = _whisper_constants()
    centered = np.pad(audio.astype(np.float64), (200, 200), mode="reflect")
    frames = np.lib.stride_tricks.sliding_window_view(centered, 400)[::160]
    spectrum = np.fft.rfft(frames * window, axis=-1)
    power = np.abs(spectrum) ** 2
    mel = np.einsum("mf,tf->mt", filters, power, optimize=False)
    log_mel = np.log10(np.maximum(mel, 1e-10)).astype(np.float32)[:, :-1]
    log_mel = np.maximum(log_mel, log_mel.max() - 8)
    return ((log_mel + 4) / 4)[None].astype(np.float32)


@lru_cache(maxsize=4)
def _resample_filter(rate):
    factor = gcd(rate, 16000)
    up, down = 16000 // factor, rate // factor
    width = max(up, down)
    half = 10 * width
    offsets = np.arange(-half, half + 1)
    kernel = np.sinc(offsets / width) * np.kaiser(2 * half + 1, 5) / width
    kernel /= kernel.sum()
    kernel = kernel.astype(np.float32) * up
    return up, down, half, kernel


def _resample(audio: np.ndarray, rate: int) -> np.ndarray:
    """Zero-phase Kaiser polyphase FIR, matching resample_poly's default filter."""
    if rate == 16000:
        return audio.astype(np.float32, copy=False)
    up, down, half, kernel = _resample_filter(rate)
    count = (len(audio) * up + down - 1) // down
    output = np.empty(count, dtype=np.float32)
    # Evaluate only the taps in each phase, without allocating upsampled audio.
    ticks = half + np.arange(count) * down
    phases = ticks % up
    padding = (len(kernel) + up - 1) // up
    padded = np.pad(audio, (padding, padding))
    for phase in range(up):
        selected = np.flatnonzero(phases == phase)
        if selected.size:
            positions = ticks[selected] // up + padding
            taps = kernel[phase::up]
            accumulated = np.zeros(len(selected), dtype=np.float32)
            # Accumulate oldest samples first, as in a conventional upfirdn.
            for tap in range(len(taps) - 1, -1, -1):
                accumulated += padded[positions - tap] * taps[tap]
            output[selected] = accumulated
    return output


class Endpointer:
    def __init__(self, rate=24000, *, fast_silence_ms=200, retry_silence_ms=600,
                 max_silence_ms=1400, threshold=0.5, min_speech_ms=120,
                 confirm_speech_ms=300, preroll_ms=300):
        if not isinstance(rate, int) or rate <= 0:
            raise ValueError("rate must be a positive integer")
        if not 0 < fast_silence_ms <= retry_silence_ms <= max_silence_ms:
            raise ValueError("silence deadlines must be positive and ordered")
        if not 0 < min_speech_ms <= confirm_speech_ms or preroll_ms < 0:
            raise ValueError("speech durations must be positive and ordered")
        if not 0 <= threshold <= 1:
            raise ValueError("threshold must be between zero and one")
        self.rate = rate
        self.fast_silence_ms = fast_silence_ms
        self.retry_silence_ms = retry_silence_ms
        self.max_silence_ms = max_silence_ms
        self.threshold = threshold
        self.min_speech_ms = min_speech_ms
        self.confirm_speech_ms = confirm_speech_ms
        self._preroll_bytes = int(preroll_ms * rate / 1000) * 2
        self._max_bytes = MAX_UTTERANCE_S * rate * 2   # a monologue keeps its last two minutes
        self._vad, self._turn = _sessions()
        self.reset()

    def reset(self) -> None:
        """Drop buffered input and utterance, including this call's VAD state."""
        self._pending = bytearray()
        self._frames = 0
        self._clear_turn()

    def _clear_turn(self):
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, 64), dtype=np.float32)
        self._preroll = bytearray()
        self._audio = None
        self._speech_samples = 0
        self._silence_samples = 0
        self._started = False
        self._confirmed = False
        self._probes = 0
        self._prob = None

    @property
    def in_speech(self) -> bool:
        """True from speech_start until turn_end/reset, including pending pauses."""
        return self._started

    def _is_speech(self, audio):
        inputs = np.concatenate((self._context, audio[None]), axis=1)
        out, self._state = self._vad.run(None, {
            "input": inputs, "state": self._state, "sr": _SR})
        self._context = inputs[:, -64:].copy()
        return float(out[0, 0]) >= 0.5

    def _probability(self):
        # The reply retains the whole utterance; inference needs only its tail.
        tail = self._audio[-8 * self.rate * 2:]
        audio = np.frombuffer(tail, dtype="<i2").astype(np.float32) / 32768
        features = _whisper_features(_resample(audio, self.rate))
        return float(self._turn.run(None, {"input_features": features})[0].flat[0])

    def _remember(self, frame):
        self._preroll.extend(frame)
        if len(self._preroll) > self._preroll_bytes:
            del self._preroll[:len(self._preroll) - self._preroll_bytes]

    def _advance(self, frame, voiced):
        events = []
        if self._audio is None:
            if not voiced:
                self._remember(frame)
                return events
            self._audio = self._preroll.copy()
        self._audio.extend(frame)
        if len(self._audio) > self._max_bytes:
            del self._audio[:len(self._audio) - self._max_bytes]
        if voiced:
            self._speech_samples += 512
            self._silence_samples = 0
            self._probes = 0
            self._prob = None
            speech_ms = self._speech_samples * 1000 / 16000
            if not self._started and speech_ms >= self.min_speech_ms:
                self._started = True
                events.append({"type": "speech_start"})
            if not self._confirmed and speech_ms >= self.confirm_speech_ms:
                self._confirmed = True
                events.append({"type": "speech_confirmed"})
            return events

        if not self._started:
            # A brief noise must not become a turn or accumulate into barge-in.
            self._preroll.clear()
            self._remember(self._audio)
            self._audio = None
            self._speech_samples = 0
            return events

        self._silence_samples += 512
        silence_ms = self._silence_samples * 1000 // 16000
        reason = None
        for index, deadline in enumerate((self.fast_silence_ms, self.retry_silence_ms)):
            if self._probes <= index and silence_ms >= deadline:
                self._prob = self._probability()
                self._probes = index + 1
                if self._prob >= self.threshold:
                    reason = "smart_turn"
                    break
        if reason is None and silence_ms >= self.max_silence_ms:
            reason = "max_silence"
        if reason:
            events.append({"type": "turn_end", "audio": bytes(self._audio),
                           "prob": self._prob, "reason": reason,
                           "silence_ms": silence_ms,
                           "speech_ms": self._speech_samples * 1000 // 16000})
            self._clear_turn()
        return events

    def feed(self, pcm16: bytes) -> list[dict]:
        """Consume arbitrary PCM16 chunks (even a split sample), returning ordered events."""
        data = self._pending + pcm16
        events = []
        offset = 0
        while True:
            start = self._frames * 512 * self.rate // 16000
            end = (self._frames + 1) * 512 * self.rate // 16000
            size = (end - start) * 2
            if offset + size > len(data):
                break
            frame = data[offset:offset + size]
            source = np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768
            positions = ((self._frames * 512 + np.arange(512))
                         * (self.rate / 16000) - start)
            model_audio = np.interp(positions, np.arange(len(source)), source).astype(np.float32)
            events.extend(self._advance(frame, self._is_speech(model_audio)))
            self._frames += 1
            offset += size
        self._pending = data[offset:]
        return events

    def silence(self, ms: int) -> list[dict]:
        """Advance muted time through the same VAD and deadline policy as feed."""
        if ms < 0:
            raise ValueError("silence duration must not be negative")
        remaining = int(ms * self.rate / 1000)
        events = []
        while remaining:
            count = min(remaining, self.rate)
            events.extend(self.feed(bytes(count * 2)))
            remaining -= count
        return events
