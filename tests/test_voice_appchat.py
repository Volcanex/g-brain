"""App session: reply blocks, the line budget, attachments and the read-only
activity boundary. The model stream is stubbed. voice is imported inside the
tests, never at collection (see conftest)."""
import json

import pytest


def test_blocks_keep_contract_shapes_and_drop_the_rest():
    from voice import blocks
    out = blocks.clean([
        {"type": "bullets", "items": ["one", {"text": "two", "label": "Money"}, {"nope": 1}]},
        {"type": "rows", "rows": [{"title": "Dentist", "trail": {"text": "15:00", "tone": "accent"},
                                    "action": {"path": "/x"}, "accent": "pink"}]},
        {"type": "detail", "text": "small print"},
        {"type": "image", "url": "https://evil.example/x.png"},
        {"type": "image", "url": "/api/voice/files/" + "a" * 32, "alt": "receipt"},
        {"type": "fold", "title": "More", "blocks": [{"type": "detail", "text": "x"},
                                                     {"type": "fold", "title": "deeper", "blocks": []}]},
        {"type": "panel", "name": "calendar", "blocks": ["smuggled"]},
        {"type": "panel", "name": "secrets"},
        {"type": "html", "html": "<b>"},
    ])
    assert [b["type"] for b in out] == ["bullets", "rows", "detail", "image", "fold", "panel"]
    assert out[0]["items"] == [{"text": "one"}, {"text": "two", "label": "Money"}]
    row = out[1]["rows"][0]
    assert "action" not in row and "accent" not in row and row["trail"] == {"text": "15:00", "tone": "accent"}
    assert out[4] == {"type": "fold", "title": "More", "open": False, "blocks": [{"type": "detail", "text": "x"}]}
    assert out[5] == {"type": "panel", "name": "calendar"}
    assert len(blocks.problems([{"type": "html"}, {"type": "rows"}])) == 2


def test_block_count_is_capped():
    from voice import blocks
    assert len(blocks.clean([{"type": "detail", "text": str(i)} for i in range(40)])) == blocks.MAX_BLOCKS


@pytest.fixture
def app_turn(tmp_path, monkeypatch):
    from voice import agent, appchat, transcript
    monkeypatch.setattr(transcript, "_TRANSCRIPT_DIR", str(tmp_path / "voice"))
    monkeypatch.setattr(agent, "_HISTORY", {})
    monkeypatch.setattr(appchat, "build_brief", lambda: "(brief stub)")
    return appchat, agent, tmp_path / "voice"


def _rounds(monkeypatch, appchat, rounds, seen=None):
    it = iter(rounds)

    def fake(messages, model=None):
        if seen is not None:
            seen.append(messages)
        return iter(next(it))
    monkeypatch.setattr(appchat, "_stream", fake)


def _call(name, args, cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_whats_on_today_shows_the_calendar_panel(app_turn, monkeypatch):
    appchat, agent, tdir = app_turn
    _rounds(monkeypatch, appchat, [
        [("calls", [_call("show_panel", {"name": "calendar"})])],
        [("text", "Three things today, "), ("text", "the dentist is the one to leave for."), ("calls", [])],
    ])
    evs = list(appchat.stream_turn("app", "what's on today?"))
    assert [e["t"] for e in evs] == ["tool", "delta", "delta", "done"]
    done = evs[-1]
    assert done["reply"] == "Three things today, the dentist is the one to leave for."
    assert done["blocks"] == [{"type": "panel", "name": "calendar"}]
    last = json.loads((tdir / "app.jsonl").read_text().splitlines()[-1])
    assert last["role"] == "assistant" and last["blocks"] == done["blocks"]
    # the model never re-reads block JSON
    assert agent._HISTORY["app"][-1] == {"role": "assistant", "content": done["reply"]}


def test_plain_chat_has_no_blocks(app_turn, monkeypatch):
    appchat, _, tdir = app_turn
    _rounds(monkeypatch, appchat, [[("text", "Morning."), ("calls", [])]])
    assert appchat.collect("app", "hey") == {"reply": "Morning.", "blocks": []}
    assert "blocks" not in json.loads((tdir / "app.jsonl").read_text().splitlines()[-1])


def test_attach_blocks_reports_what_it_dropped(app_turn, monkeypatch):
    appchat, _, _ = app_turn
    state = []
    out = appchat._app_tool("attach_blocks", json.dumps({"blocks": [
        {"type": "bullets", "items": ["a", "b", "c"]}, {"type": "table"}]}), state, None)
    assert out["attached"] == 1 and out["dropped"] and state[0]["type"] == "bullets"


def test_budget_reaches_the_app_prompt_only(app_turn, monkeypatch):
    appchat, _, _ = app_turn
    seen = []
    _rounds(monkeypatch, appchat, [[("text", "ok"), ("calls", [])]], seen)
    list(appchat.stream_turn("app", "hi", budget={"lines": 9, "chars_per_line": 38}))
    system = seen[0][0]["content"]
    assert "about 9 lines of about 38" in system and "G-Command app" in system
    assert "Screen budget" not in appchat.system_prompt(None)
    from voice import prompt, talk
    assert "Screen budget" not in prompt.SYSTEM and "Screen budget" not in talk.SPOKEN


def test_images_and_files_reach_the_model_not_history(app_turn, monkeypatch):
    appchat, agent, tdir = app_turn
    seen = []
    _rounds(monkeypatch, appchat, [[("text", "A receipt for 12 pounds."), ("calls", [])]], seen)
    meta = [{"id": "a" * 32, "kind": "image", "name": "r.jpg", "content_type": "image/jpeg",
             "size": 3, "url": "/api/voice/files/" + "a" * 32}]
    list(appchat.stream_turn("app", "what is this", images=[{"content_type": "image/jpeg", "data": "QUJD"}],
                             files=[{"name": "n.txt", "content_type": "text/plain", "text": "hello"}],
                             attachments=meta))
    live = seen[0][-1]["content"]
    from voice import config
    assert config.VISION_MODEL in seen[0][0]["content"]
    assert live[0]["type"] == "text" and "[file n.txt]\nhello" in live[0]["text"]
    assert live[1]["image_url"]["url"] == "data:image/jpeg;base64,QUJD"
    user = json.loads((tdir / "app.jsonl").read_text().splitlines()[0])
    assert user["text"] == "what is this [file: n.txt] [photo]" and user["attachments"] == meta
    assert "QUJD" not in (tdir / "app.jsonl").read_text()


def test_activity_needs_the_turns_grant(monkeypatch):
    from voice import appchat
    assert "error" in appchat.read_activity(None)
    got = {}

    class R:
        status_code = 200

        def json(self):
            return {"read_only": True, "hosts": {}}

    def fake_get(url, params, timeout, headers):
        got.update(url=url, params=params, headers=headers)
        return R()
    monkeypatch.setattr(appchat.httpx, "get", fake_get)
    out = appchat.read_activity({"url": "http://127.0.0.1:1/api/sessions/activity", "grant": "1.x"},
                                host="h", lines=5)
    assert out["read_only"] and got["headers"] == {"X-Voice-Grant": "1.x"}
    assert got["params"] == {"host": "h", "lines": 5}
    appchat.read_activity({"url": "u", "grant": "g"})
    assert got["params"] == {"lines": 4}


def test_voice_has_no_tool_that_drives_an_agent():
    """The boundary: Voice reads agent sessions and never types into them."""
    from voice import agent, appchat
    names = {t["function"]["name"] for t in appchat.TOOLS}
    assert names == {t["function"]["name"] for t in agent.TOOLS} | {"show_panel", "attach_blocks", "agent_activity"}
    for n in names:
        assert not any(w in n for w in ("send", "input", "keys", "type_", "drive", "prompt"))
    import inspect
    src = inspect.getsource(appchat)
    assert "send-keys" not in src and "httpx.post" not in src


def test_chat_route_keeps_web_plain_and_gives_app_blocks(app_turn, monkeypatch):
    appchat, agent, _ = app_turn
    from fastapi.testclient import TestClient
    from voice import app as app_mod
    monkeypatch.setattr(app_mod.agent, "handle", lambda session, message: "plain")
    _rounds(monkeypatch, appchat, [[("calls", [_call("show_panel", {"name": "tasks"})])],
                                   [("text", "Two open."), ("calls", [])]])
    c = TestClient(app_mod.app)
    assert c.post("/chat", json={"message": "hi"}).json() == {"reply": "plain"}
    assert c.post("/chat", json={"message": "todos?", "session": "app",
                                 "budget": {"lines": 10, "chars_per_line": 40}}).json() == {
        "reply": "Two open.", "blocks": [{"type": "panel", "name": "tasks"}]}
    assert c.post("/chat", json={"message": "x", "session": "app",
                                 "budget": {"lines": 0, "chars_per_line": 40}}).status_code == 422


def test_chat_stream_is_ndjson(app_turn, monkeypatch):
    appchat, _, _ = app_turn
    from fastapi.testclient import TestClient
    from voice import app as app_mod
    _rounds(monkeypatch, appchat, [[("text", "Hi "), ("text", "there."), ("calls", [])]])
    r = TestClient(app_mod.app).post("/chat/stream", json={"message": "hey", "session": "app"})
    evs = [json.loads(line) for line in r.text.splitlines()]
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert [e["t"] for e in evs] == ["delta", "delta", "done"] and evs[-1]["reply"] == "Hi there."
