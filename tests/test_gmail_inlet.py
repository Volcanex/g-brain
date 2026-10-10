"""gmail inlet: trimmed text for the model, and the applier's all-or-nothing ack."""
import json

import pytest

from inlets import gmail_apply, gmail_fetch


def test_clean_strips_quotes_links_and_signature_chain():
    raw = ("Hi, can you pay by Friday?\n> old quoted line\nSee https://clicks.example.com/f/a/" + "x" * 300
           + " https://t.co/zzz now\nOn Mon, 1 Jan 2026 at 10:00, Bob wrote:\nstuff after\n")
    out = gmail_fetch._clean(raw)
    assert "old quoted" not in out and "stuff after" not in out and "xxxx" not in out
    assert out.startswith("Hi, can you pay by Friday?") and "[link]" in out


def _stub(monkeypatch, created, fail_on=None):
    def call(path, payload=None):
        if path.startswith("/todos?"):
            return [{"title": "Already open"}]
        if fail_on and payload["title"] == fail_on:
            return {"detail": "bad"}
        created.append(payload)
        return {"slug": "s"}
    monkeypatch.setattr(gmail_apply, "_call", call)
    import brain_pulse
    monkeypatch.setattr(brain_pulse, "record_state", lambda *a, **k: {})
    acked = []
    monkeypatch.setattr(gmail_fetch, "main", lambda: acked.append(True))
    return acked


def test_apply_skips_duplicates_creates_tags_source_and_acks(monkeypatch):
    created = []
    acked = _stub(monkeypatch, created)
    made, skipped = gmail_apply.apply({"todos": [{"title": "already open", "priority": "P1"},
                                                  {"title": "Pay rent", "priority": "P1", "due": "2026-10-09", "body": "q"}]})
    assert (made, skipped, acked) == (1, 1, [True])
    assert created[0]["body"].endswith("source: pulse/gmail")


def test_apply_failure_does_not_ack(monkeypatch):
    created = []
    acked = _stub(monkeypatch, created, fail_on="Pay rent")
    with pytest.raises(RuntimeError):
        gmail_apply.apply({"todos": [{"title": "Pay rent", "priority": "P1"}]})
    assert acked == []
