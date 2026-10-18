"""Gmail ingestion: stable event ids, retry safety and all-or-nothing ack."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_clean_strips_quotes_links_and_signature_chain():
    from inlets import gmail_fetch
    raw = ("Hi, can you pay by Friday?\n> old quoted line\nSee https://clicks.example.invalid/f/a/" + "x" * 300
           + " https://example.invalid/zzz now\nOn Mon, 1 Jan 2026 at 10:00, Morgan wrote:\nstuff after\n")
    out = gmail_fetch._clean(raw)
    assert "old quoted" not in out and "stuff after" not in out and "xxxx" not in out
    assert out.startswith("Hi, can you pay by Friday?") and "[link]" in out


@pytest.fixture
def applier(tmp_path, monkeypatch):
    import brain_todos
    import brain_pulse
    from inlets import gmail_apply, gmail_fetch
    monkeypatch.setattr(brain_todos, "TODOS_DIR", tmp_path / "todos")
    app = FastAPI()
    app.include_router(brain_todos.create_router())
    client = TestClient(app)
    posts = []

    def call(path, payload=None, method=None):
        if payload is None:
            response = client.get(path)
        elif method == "PATCH":
            response = client.patch(path, json=payload)
        else:
            posts.append(payload)
            response = client.post(path, json=payload)
        response.raise_for_status()
        return response.json()

    monkeypatch.setattr(gmail_apply, "_call", call)
    monkeypatch.setattr(brain_pulse, "record_state", lambda *a, **k: {})
    acked = []
    monkeypatch.setattr(gmail_fetch, "main", lambda: acked.append(True))
    return gmail_apply, brain_todos, posts, acked


def test_apply_skips_duplicates_creates_provenance_and_acks(applier):
    gmail_apply, todos, posts, acked = applier
    todos.create("Already open", "P1")
    made, skipped = gmail_apply.apply({"todos": [
        {"title": "already open", "source_id": "fake-1", "priority": "P1"},
        {"title": "Pay rent", "source_id": "fake-2", "priority": "P1", "body": "one fact"}]})
    assert (made, skipped, acked) == (1, 1, [True])
    assert posts[0]["body"] == "one fact"
    item = next(t for t in todos.list_todos() if t["title"] == "Pay rent")
    assert item["provenance"] == {"requester": "pulse.gmail.distil", "composer": "pulse.gmail.distil", "source": "gmail:fake-2"}
    assert item["source_key"] == "gmail:fake-2#1"


def test_retry_of_same_plan_creates_nothing_even_after_completion(applier):
    gmail_apply, todos, posts, acked = applier
    plan = {"todos": [{"title": "Pay water bill", "priority": "P1", "source_id": "fake-3"}]}
    assert gmail_apply.apply(plan) == (1, 0)
    todos.mark_done(todos.list_todos()[0]["slug"])
    assert gmail_apply.apply(plan) == (0, 1)
    assert len(posts) == 1 and len(acked) == 2


def test_same_batch_normalized_duplicate_creates_one(applier):
    gmail_apply, todos, posts, acked = applier
    plan = {"todos": [{"title": r'Pay\nwater bill"', "priority": "P1", "source_id": "fake-4"},
                      {"title": "Pay water bill", "priority": "P1", "source_id": "fake-5"}]}
    assert gmail_apply.apply(plan) == (1, 1)
    assert len(posts) == 1 and len(todos.list_todos()) == 1


@pytest.mark.parametrize("already_exists", [False, True])
def test_title_duplicate_completion_and_identical_retry_create_nothing(applier, already_exists):
    gmail_apply, todos, posts, acked = applier
    if already_exists:
        todos.create("Pay water bill", "P1", source_key="original:fake")
    plan = {"todos": [
        {"title": "Pay water bill", "priority": "P1", "source_id": "duplicate-fake-1"},
        {"title": "pay water bill", "priority": "P1", "source_id": "duplicate-fake-2"},
    ]}
    assert gmail_apply.apply(plan) == ((0, 2) if already_exists else (1, 1))
    survivor, = todos.list_todos()
    todos.mark_done(survivor["slug"])
    assert gmail_apply.apply(plan) == (0, 2)
    assert len(todos.list_todos(status="all")) == 1
    assert len(acked) == 2
    for source_id in ("duplicate-fake-1", "duplicate-fake-2"):
        assert todos.create("Retry with new wording", source_key=f"gmail:{source_id}#1")["deduped"]


def test_two_todos_from_one_message_survive(applier):
    gmail_apply, todos, posts, acked = applier
    plan = {"todos": [{"title": "Catch outward train", "priority": "P1", "source_id": "fake-6"},
                      {"title": "Catch return train", "priority": "P1", "source_id": "fake-6"}]}
    assert gmail_apply.apply(plan) == (2, 0)
    assert {t["source_key"] for t in todos.list_todos()} == {"gmail:fake-6#1", "gmail:fake-6#2"}
    assert gmail_apply.apply(plan) == (0, 2)


def test_apply_failure_does_not_ack(applier):
    gmail_apply, todos, posts, acked = applier
    with pytest.raises(Exception):
        gmail_apply.apply({"todos": [{"title": "Deploy workflow failure", "priority": "P1", "source_id": "fake-7"}]})
    assert acked == []


def test_missing_source_id_fails_before_any_write_or_ack(applier, monkeypatch, capsys):
    import io
    gmail_apply, todos, posts, acked = applier
    plan = {"todos": [{"title": "Pay bill", "priority": "P1", "source_id": "fake-8"},
                      {"title": "Reply to Morgan", "priority": "P1"}]}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(plan)))
    assert gmail_apply.main() == 1
    assert "source_id" in capsys.readouterr().out
    assert posts == [] and acked == []


@pytest.mark.parametrize("message_id,expected", [("<fake@example.invalid>", "<fake@example.invalid>"), (None, "41")])
def test_gate_exposes_stable_message_id_and_reads_without_marking(tmp_path, monkeypatch, capsys, message_id, expected):
    from inlets import gmail_fetch
    raw = b"From: sender@example.invalid\r\nSubject: Example\r\n\r\nPay bill"
    if message_id:
        raw = f"Message-ID: {message_id}\r\n".encode() + raw
    calls = []

    class IMAP:
        def login(self, *args):
            pass

        def select(self, mailbox, readonly=False):
            assert mailbox == "INBOX" and readonly

        def uid(self, operation, *args):
            calls.append((operation, args))
            return ("OK", [b"41"]) if operation == "search" else ("OK", [(b"meta", raw)])

        def logout(self):
            pass

    monkeypatch.setattr(gmail_fetch.imaplib, "IMAP4_SSL", lambda *a: IMAP())
    monkeypatch.setattr(gmail_fetch, "_env", lambda key: "fake")
    monkeypatch.setattr(gmail_fetch, "STATE", str(tmp_path / "state.json"))
    monkeypatch.setattr(gmail_fetch.urllib.request, "urlopen", lambda *a, **k: __import__("io").StringIO("[]"))
    monkeypatch.setattr("sys.argv", ["gmail_fetch", "--check"])
    gmail_fetch.main()
    assert "source_id: " + expected in capsys.readouterr().out
    assert ("fetch", ("41", "(BODY.PEEK[])")) in calls


def test_double_encoded_json_plan_body_is_normalized(applier):
    gmail_apply, todos, posts, acked = applier
    plan_json = json.dumps({"todos": [{"title": "Call the clinic", "priority": "P1", "source_id": "fake-9",
                                      "body": r'Bring\r\nID\tand \"card\"'}]})
    assert gmail_apply.apply(json.loads(plan_json)) == (1, 0)
    assert todos.list_todos()[0]["body"] == 'Bring\nID\tand "card"'
