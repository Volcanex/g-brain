"""Voice photo ingest, transcripts and rehydration, against a throwaway BRAIN_ROOT.

The model is stubbed, the brief is stubbed (it would read the live pulse page), and
transcripts go to a temp dir that the fixture checks before any turn runs. voice is
imported inside the tests, never at collection: importing it pulls in brain_todos,
and test_todos must be the first to import that module (see conftest).
"""
import json

import pytest

TINY_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16


@pytest.fixture
def voice(tmp_path, monkeypatch):
    from voice import agent, transcript
    monkeypatch.setenv("BRAIN_ROOT", str(tmp_path))
    d = tmp_path / ".pulse" / "state" / "voice"
    monkeypatch.setattr(transcript, "_TRANSCRIPT_DIR", str(d))
    assert transcript._path("probe").startswith(str(d))
    monkeypatch.setattr(agent, "_HISTORY", {})
    monkeypatch.setattr(agent, "build_brief", lambda: "(brief stub)")
    monkeypatch.setattr(agent, "_call_model", lambda messages, use_tools=True:
                        {"role": "assistant", "content": "stub reply", "tool_calls": None})
    return agent, transcript, d


def _inbound(sender, text=None, media=None):
    payload = {"from": {"phone_number": sender}}
    if text is not None:
        payload["body"] = {"text": {"body": text}}
    if media is not None:
        payload["media"] = media
    return {"data": {"event_type": "message.received", "payload": payload}}


def test_parse_text_only():
    from voice.whatsapp import parse_inbound
    assert list(parse_inbound(_inbound("+447700900001", "hello world"))) == \
        [("+447700900001", "hello world", [])]


def test_parse_photo_with_caption():
    from voice.whatsapp import parse_inbound
    media = [{"url": "https://media.telnyx.com/abc123", "content_type": "image/jpeg"}]
    [(sender, text, got)] = parse_inbound(_inbound("+447700900002", "my fridge", media))
    assert (sender, text) == ("+447700900002", "my fridge")
    assert [(m["url"], m["content_type"]) for m in got] == \
        [("https://media.telnyx.com/abc123", "image/jpeg")]


def test_parse_photo_without_caption():
    from voice.whatsapp import parse_inbound
    media = [{"url": "https://media.telnyx.com/def456", "content_type": "image/png"}]
    [(sender, text, got)] = parse_inbound(_inbound("+447700900003", media=media))
    assert (sender, text) == ("+447700900003", None)
    assert [m["url"] for m in got] == ["https://media.telnyx.com/def456"]


def test_handle_text_guest_and_owner(voice):
    agent, _, _ = voice
    assert agent.handle("test_sender", "what's on my plate today", owner=False) == "stub reply"
    assert any(m["content"] == "what's on my plate today" for m in agent._HISTORY["test_sender"])
    assert agent.handle("test_sender_owner", "hello", owner=True, images=None) == "stub reply"


def test_vision_turn_keeps_placeholder_not_base64(voice):
    agent, transcript, _ = voice
    assert agent.handle("test_vision", "my fridge", owner=True,
                        images=[(TINY_JPEG, "image/jpeg")]) == "stub reply"
    stored = [m for m in agent._HISTORY["test_vision"] if m["role"] == "user"][0]["content"]
    assert "[photo]" in stored and "base64" not in stored
    first = json.loads(open(transcript._path("test_vision")).readline())
    assert "[photo]" in first["text"] and "base64" not in first["text"]


def test_transcript_lines_and_rehydration(voice):
    agent, transcript, voice_dir = voice
    agent.handle("fake_sender", "turn one", owner=False)
    agent.handle("fake_sender", "turn two", owner=False)
    path = transcript._path("fake_sender")
    assert path.startswith(str(voice_dir))
    roles = [json.loads(l)["role"] for l in open(path).read().splitlines() if l.strip()]
    assert roles == ["user", "assistant", "user", "assistant"]

    agent._HISTORY.pop("fake_sender")
    agent.handle("fake_sender", "turn three", owner=False)
    assert [m["content"] for m in agent._HISTORY["fake_sender"]][:4] == \
        ["turn one", "stub reply", "turn two", "stub reply"]
