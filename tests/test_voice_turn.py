"""Offline live-call tests, with real Kokoro bm_george speech recorded 2026-10-24.

Fixtures were rendered once via localhost:8078/tts, at its default speed:
question: "What have I got on tomorrow morning?"
incomplete: "So I was thinking that maybe we could" (ends mid-clause)
pause_resume: question + exactly 2 s of zeros + "And what about the afternoon?"
All WAVs are mono PCM16 at 24 kHz. No test calls TTS or downloads models.
Real inference tests need ensure_models() run beforehand, or VOICE_LIVE_MODELS.
"""
import hashlib
import io
import os
from pathlib import Path
import wave

import numpy as np
import pytest


FIXTURES = Path(__file__).parent / "fixtures" / "voice_turn"
FRAME = 768  # one 512-sample VAD window at the 24 kHz source rate


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    from voice import turn

    def refuse(*args, **kwargs):
        pytest.fail("endpointer tests must not use the network")

    monkeypatch.setattr(turn.urllib.request, "urlopen", refuse)


@pytest.fixture
def models():
    from voice import turn

    root = Path(os.path.expanduser(os.environ.get(
        "VOICE_LIVE_MODELS", "~/.local/share/voice-live")))
    if not all((root / name).is_file() for name in turn._MODELS):
        pytest.skip("install voice-live models first (tests never download them)")
    return turn


@pytest.fixture
def controlled(monkeypatch):
    from voice import turn

    monkeypatch.setattr(turn, "_sessions", lambda: (None, None))
    monkeypatch.setattr(turn.Endpointer, "_is_speech", lambda self, audio: bool(audio.any()))
    return turn.Endpointer


def pcm(name):
    with wave.open(str(FIXTURES / (name + ".wav"))) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, 24000)
        return w.readframes(w.getnframes())


def speech(frames):
    return b"\x01\x00" * (frames * FRAME)


def quiet(frames):
    return bytes(frames * FRAME * 2)


def stream(endpoint, data, chunk=960):
    events = []
    for start in range(0, len(data), chunk):
        events.extend(endpoint.feed(data[start:start + chunk]))
    return events


def test_real_question_ends_at_fast_or_retry(models):
    endpoint = models.Endpointer()
    events = stream(endpoint, pcm("question")) + endpoint.silence(1600)
    assert [e["type"] for e in events] == ["speech_start", "speech_confirmed", "turn_end"]
    end = events[-1]
    assert end["reason"] == "smart_turn" and end["prob"] >= 0.5
    assert 200 <= end["silence_ms"] <= 600 + 32
    assert end["speech_ms"] >= 300 and not endpoint.in_speech
    assert len(end["audio"]) % 2 == 0


def test_real_incomplete_clause_waits_until_max_silence(models):
    endpoint = models.Endpointer()
    events = stream(endpoint, pcm("incomplete")) + endpoint.silence(1800)
    ends = [e for e in events if e["type"] == "turn_end"]
    assert len(ends) == 1
    assert ends[0]["reason"] == "max_silence" and ends[0]["prob"] < 0.5
    assert 1400 <= ends[0]["silence_ms"] < 1400 + 32


def test_real_two_second_pause_starts_a_second_turn(models):
    endpoint = models.Endpointer()
    events = stream(endpoint, pcm("pause_resume")) + endpoint.silence(1600)
    assert [e["type"] for e in events] == [
        "speech_start", "speech_confirmed", "turn_end",
        "speech_start", "speech_confirmed", "turn_end"]
    ends = [e for e in events if e["type"] == "turn_end"]
    assert all(e["reason"] == "smart_turn" for e in ends)
    assert len(ends[0]["audio"]) / 48000 < len(pcm("question")) / 48000 + 1
    assert len(ends[1]["audio"]) / 48000 < 3


def test_real_arbitrary_chunks_match_one_large_chunk(models):
    data = pcm("pause_resume") + bytes(1600 * 48)
    expected = models.Endpointer().feed(data)
    endpoint = models.Endpointer()
    events, offset, index = [], 0, 0
    sizes = [1, 337, 1921, 4800, 7]
    while offset < len(data):
        size = sizes[index % len(sizes)]
        events.extend(endpoint.feed(data[offset:offset + size]))
        offset += size
        index += 1
    assert events == expected


def test_real_silence_matches_pcm_and_sessions_are_shared(models):
    first, second = models.Endpointer(), models.Endpointer()
    assert first._vad is second._vad and first._turn is second._turn
    assert first._state is not second._state
    assert first.feed(pcm("question")) == second.feed(pcm("question"))
    assert first.silence(1600) == second.feed(bytes(1600 * 48))


@pytest.mark.parametrize("rate", [16000, 24000, 44100, 48000])
def test_source_rate_and_original_pcm_are_preserved(controlled, monkeypatch, rate):
    endpoint = controlled(rate=rate)
    monkeypatch.setattr(endpoint, "_probability", lambda: 0.9)
    voiced = b"\x01\x00" * (rate * 320 // 1000)
    data = voiced + bytes(rate)
    events = endpoint.feed(data)
    end = events[-1]
    assert [e["type"] for e in events] == ["speech_start", "speech_confirmed", "turn_end"]
    assert end["audio"] == data[:len(end["audio"])]
    assert len(end["audio"]) == rate * (320 + 224) // 1000 * 2


def test_start_confirmation_preroll_and_no_duplicate_events(controlled, monkeypatch):
    endpoint = controlled()
    monkeypatch.setattr(endpoint, "_probability", lambda: 0.9)
    assert endpoint.feed(quiet(12)) == []
    assert endpoint.feed(speech(3)) == []
    assert not endpoint.in_speech
    assert endpoint.feed(speech(1)) == [{"type": "speech_start"}]
    assert endpoint.in_speech
    assert endpoint.feed(speech(6)) == [{"type": "speech_confirmed"}]
    assert endpoint.feed(speech(2)) == []
    end = endpoint.feed(quiet(7))[-1]
    assert end["audio"] == bytes(300 * 48) + speech(12) + quiet(7)
    assert end["speech_ms"] == 384 and end["silence_ms"] == 224
    assert not endpoint.in_speech


@pytest.mark.parametrize("probabilities,reason,silence_frames", [
    ([0.8], "smart_turn", 7),
    ([0.1, 0.8], "smart_turn", 19),
    ([0.1, 0.2], "max_silence", 44),
    ([0.5], "smart_turn", 7),
])
def test_silence_deadlines(controlled, monkeypatch, probabilities, reason, silence_frames):
    endpoint = controlled()
    values, probes = iter(probabilities), []

    def probability():
        probes.append(endpoint._silence_samples * 1000 // 16000)
        return next(values)

    monkeypatch.setattr(endpoint, "_probability", probability)
    endpoint.feed(speech(10))
    assert endpoint.feed(quiet(silence_frames - 1)) == []
    end = endpoint.feed(quiet(1))[-1]
    assert end["reason"] == reason and end["silence_ms"] == silence_frames * 32
    assert probes == [224, 608][:len(probabilities)]
    assert end["prob"] == probabilities[-1]


def test_resume_cancels_pending_decision_and_keeps_audio(controlled, monkeypatch):
    endpoint = controlled()
    probabilities = iter([0.1, 0.9])
    monkeypatch.setattr(endpoint, "_probability", lambda: next(probabilities))
    assert endpoint.feed(speech(10)) == [
        {"type": "speech_start"}, {"type": "speech_confirmed"}]
    assert endpoint.feed(quiet(7)) == []
    assert endpoint.in_speech
    assert endpoint.feed(speech(10)) == []
    end = endpoint.feed(quiet(7))[-1]
    assert end["reason"] == "smart_turn" and end["speech_ms"] == 640
    assert end["silence_ms"] == 224
    assert end["audio"] == speech(10) + quiet(7) + speech(10) + quiet(7)


def test_short_noise_never_becomes_a_turn(controlled, monkeypatch):
    endpoint = controlled()
    monkeypatch.setattr(endpoint, "_probability", lambda: pytest.fail("noise reached smart-turn"))
    assert endpoint.feed((speech(3) + quiet(10)) * 3 + quiet(50)) == []
    assert not endpoint.in_speech and endpoint._audio is None
    assert len(endpoint._preroll) <= 300 * 48


def test_rejected_early_noise_keeps_preroll_without_duplication(controlled, monkeypatch):
    endpoint = controlled()
    monkeypatch.setattr(endpoint, "_probability", lambda: 0.9)
    original = quiet(2) + speech(1) + quiet(1) + speech(10) + quiet(7)
    end = endpoint.feed(original)[-1]
    assert end["audio"] == original


def test_reset_drops_pending_audio_and_vad_state(controlled, monkeypatch):
    endpoint = controlled()
    monkeypatch.setattr(endpoint, "_probability", lambda: 0.9)
    endpoint.feed(speech(10) + b"\x01")
    endpoint._state.fill(1)
    endpoint.reset()
    assert not endpoint.in_speech and not endpoint._state.any()
    assert endpoint.silence(1600) == []
    events = endpoint.feed(speech(10) + quiet(7))
    assert len(events) == 3
    assert events[-1]["audio"] == bytes(300 * 48) + speech(10) + quiet(7)


def test_large_chunk_keeps_audio_after_turn_end(controlled, monkeypatch):
    endpoint = controlled()
    monkeypatch.setattr(endpoint, "_probability", lambda: 0.9)
    events = endpoint.feed((speech(10) + quiet(50)) * 2)
    assert [e["type"] for e in events] == [
        "speech_start", "speech_confirmed", "turn_end",
        "speech_start", "speech_confirmed", "turn_end"]
    assert events[-1]["audio"] == bytes(300 * 48) + speech(10) + quiet(7)


def test_whisper_shape_silence_and_eight_second_tail():
    from voice.turn import _whisper_features

    silent = _whisper_features(np.zeros(16000, dtype=np.float32))
    assert silent.shape == (1, 80, 800) and silent.dtype == np.float32
    np.testing.assert_array_equal(silent, -1.5)
    audio = np.random.default_rng(42).normal(size=160000).astype(np.float32)
    np.testing.assert_array_equal(_whisper_features(audio), _whisper_features(audio[-128000:]))


def test_smart_turn_resampler_preserves_speech_band_and_rejects_aliasing():
    from voice.turn import _resample

    times = np.arange(24000, dtype=np.float32) / 24000
    low = _resample(np.sin(2 * np.pi * 1000 * times), 24000)
    high = _resample(np.sin(2 * np.pi * 10000 * times), 24000)
    assert len(low) == len(high) == 16000
    expected = np.sin(2 * np.pi * 1000 * np.arange(16000) / 16000)
    assert np.max(np.abs(low[30:-30] - expected[30:-30])) < 0.005
    assert np.sqrt(np.mean(high[30:-30] ** 2)) < 0.005


def test_models_download_once_and_verify_existing_files(tmp_path, monkeypatch):
    from voice import turn

    data = b"test model contents"
    specs = {name: ("https://example.invalid/" + name, hashlib.sha256(data).hexdigest())
             for name in ("vad.onnx", "turn.onnx")}
    monkeypatch.setattr(turn, "_MODELS", specs)
    monkeypatch.setenv("VOICE_LIVE_MODELS", str(tmp_path / "models"))
    calls = []

    def download(url, timeout):
        calls.append(url)
        return io.BytesIO(data)

    monkeypatch.setattr(turn.urllib.request, "urlopen", download)
    paths = turn.ensure_models()
    assert len(calls) == 2
    assert all(path.read_bytes() == data for path in paths.values())
    assert turn.ensure_models() == paths and len(calls) == 2
    paths["vad.onnx"].write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        turn.ensure_models()


def test_bad_download_is_never_installed(tmp_path, monkeypatch):
    from voice import turn

    monkeypatch.setenv("VOICE_LIVE_MODELS", str(tmp_path))
    monkeypatch.setattr(turn, "_MODELS", {"vad.onnx": ("https://example.invalid", "0" * 64)})
    monkeypatch.setattr(turn.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"bad"))
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        turn.ensure_models()
    assert list(tmp_path.iterdir()) == []


def test_default_model_directory_uses_current_users_home(tmp_path, monkeypatch):
    from voice import turn

    monkeypatch.delenv("VOICE_LIVE_MODELS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(turn, "_MODELS", {})
    assert turn.ensure_models() == {}
    assert (tmp_path / ".local/share/voice-live").is_dir()


def test_utterance_cap_keeps_latest_audio_through_turn_end(controlled, monkeypatch):
    from voice import turn

    monkeypatch.setattr(turn, "MAX_UTTERANCE_S", 1)
    endpoint = controlled()
    monkeypatch.setattr(endpoint, "_probability", lambda: 0.9)
    data = speech(20) + b"\x02\x00" * (40 * FRAME)
    limit = endpoint.rate * 2
    events = endpoint.feed(data)
    assert [e["type"] for e in events] == ["speech_start", "speech_confirmed"]
    assert bytes(endpoint._audio) == data[-limit:]
    end = endpoint.feed(quiet(7))[-1]
    assert end["type"] == "turn_end" and end["reason"] == "smart_turn"
    assert end["audio"] == (data + quiet(7))[-limit:]
    assert len(end["audio"]) == limit and not endpoint.in_speech
