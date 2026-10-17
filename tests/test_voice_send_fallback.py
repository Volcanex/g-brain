"""A refused WhatsApp reply pages, and with VOICE_NTFY_FALLBACK the owner's reply
text also goes out through ntfy. Telnyx, the model and ntfy are all stubbed.

voice is imported inside the tests, never at collection (see conftest).
"""
import pytest


@pytest.fixture
def app(monkeypatch):
    from voice import app as voice_app
    sent = []
    monkeypatch.setattr(voice_app.agent, "handle", lambda *a, **k: "**hi** there")
    monkeypatch.setattr(voice_app.whatsapp, "send_text", lambda to, body: {
        "status": 403, "body": '{"errors":[{"code":"10038","title":"Feature not permitted","detail":"x"}]}'})
    monkeypatch.setattr(voice_app.brain_alerts, "send",
                        lambda level, source, msg, dedup_key=None, **k: sent.append((source, msg, dedup_key)))
    monkeypatch.setattr(voice_app.brain_alerts, "clear", lambda key: None)
    return voice_app, sent


def test_refused_send_pages_without_fallback(app, monkeypatch):
    voice_app, sent = app
    monkeypatch.setattr(voice_app.config, "NTFY_FALLBACK", False)
    voice_app._process("+447700900001", "hello", [], owner=True)
    assert [s[0] for s in sent] == ["voice"]
    assert "10038" in sent[0][1] and sent[0][2] == "voice-send"


def test_refused_send_pushes_owner_reply_with_fallback(app, monkeypatch):
    voice_app, sent = app
    monkeypatch.setattr(voice_app.config, "NTFY_FALLBACK", True)
    voice_app._process("+447700900001", "hello", [], owner=True)
    assert sent[1] == ("voice reply", "*hi* there", None)


def test_fallback_never_pushes_guest_replies(app, monkeypatch):
    voice_app, sent = app
    monkeypatch.setattr(voice_app.config, "NTFY_FALLBACK", True)
    voice_app._process("+447700900002", "hello", [], owner=False)
    assert [s[0] for s in sent] == ["voice"]
