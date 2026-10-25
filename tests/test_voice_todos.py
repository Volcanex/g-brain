"""Voice's todo and calendar tools: edits, natural dates, the ambiguity rule,
and the delete guard (Voice deletes only on a confirmed second call). Every record stays in a temp vault."""
import datetime as dt
import json

import pytest


@pytest.fixture
def T(tmp_path, monkeypatch):
    import brain_todos
    monkeypatch.setattr(brain_todos, "TODOS_DIR", tmp_path / "todos")
    from voice import todos
    monkeypatch.setattr(todos, "today", lambda: dt.date(2026, 10, 24))   # a Saturday
    monkeypatch.setattr(brain_todos, "_today", lambda: "2026-10-24")
    return todos


def _make(title, **kw):
    import brain_todos
    return brain_todos.create(title, kw.pop("priority", "P2"), requester="gabriel", composer="agent",
                              source="test", **kw)


def _stored(slug):
    import brain_todos
    return brain_todos.get(slug).metadata


def test_natural_dates_resolve_against_london_today(T):
    base = dt.date(2026, 10, 24)
    assert T.resolve_date("tomorrow", base) == dt.date(2026, 10, 25)
    assert T.resolve_date("next Friday", base) == dt.date(2026, 10, 30)
    assert T.resolve_date("friday", base) == dt.date(2026, 10, 30)
    assert T.resolve_date("saturday", base) == dt.date(2026, 10, 31)   # never today
    assert T.resolve_date("in 3 days", base) == dt.date(2026, 10, 27)
    assert T.resolve_date("2 March", base) == dt.date(2027, 3, 2)      # nearest future
    assert T.resolve_date("2026-11-05", base) == dt.date(2026, 11, 5)
    assert T.resolve_date("whenever", base) is None
    assert T.resolve_time("3pm") == "15:00:00" and T.resolve_time("9.30") == "09:30:00"
    assert T.resolve_time("3") is None


def test_update_title_priority_star_and_time(T):
    it = _make("Call the plumber", due="2026-10-26", category="chore")
    out = T.update_todo("plumber", title="Call the plumber back", priority="P1", starred=True, time="3pm")
    assert out["ok"]
    md = _stored(it["slug"])
    assert md["title"] == "Call the plumber back" and md["priority"] == "P1" and md["urgent"] is True
    assert str(md["start"]) == "2026-10-26T15:00:00" and "due" not in md
    row = out[T.BLOCKS][0]["rows"][0]
    assert row["lead"]["text"] == "15:00" and row["marks"][0]["icon"] == "star"
    assert "action" not in row


def test_reschedule_keeps_the_time_and_duration(T):
    it = _make("Dentist", start="2026-10-26T09:00:00", end="2026-10-26T09:30:00", category="health")
    out = T.reschedule_todo(it["slug"], "next Friday")
    md = _stored(it["slug"])
    assert str(md["start"]) == "2026-10-30T09:00:00" and str(md["end"]) == "2026-10-30T09:30:00"
    assert out[T.BLOCKS][0]["title"] == "Rescheduled"
    assert "error" in T.reschedule_todo(it["slug"], "someday")


def test_reschedule_a_due_date(T):
    it = _make("Renew passport", due="2026-10-20")
    T.reschedule_todo("passport", "tomorrow")
    assert str(_stored(it["slug"])["due"]) == "2026-10-25"


def test_done_failed_and_drop(T):
    a, b, c = _make("Bin day", due="2026-10-24"), _make("Gym class", due="2026-10-24"), _make("Old idea")
    assert T.mark_done("bin day")["todo"]["status"] == "done"
    assert T.mark_failed("gym")["todo"]["status"] == "failed"
    assert T.drop_todo("old idea")["todo"]["status"] == "cancelled"
    assert _stored(a["slug"])["status"] == "done"
    assert T.mark_done("bin day")[T.BLOCKS][0]["rows"][0]["struck"] is True


def test_recurring_marks_one_date_and_refuses_moves(T):
    it = _make("Walk", start="2026-10-01", rrule="FREQ=DAILY")
    T.mark_done("walk")
    md = _stored(it["slug"])
    assert md["status"] == "open" and md["done_dates"] == ["2026-10-24"]
    T.mark_failed("walk", date="tomorrow")
    assert _stored(it["slug"])["failed_dates"] == ["2026-10-25"]
    assert "error" in T.reschedule_todo("walk", "tomorrow")


def test_ambiguous_target_changes_nothing_and_shows_candidates(T):
    a, b = _make("Email Sam about rent"), _make("Email Sam the photos")
    out = T.mark_done("email sam")
    assert out["ambiguous"] and {c["slug"] for c in out["candidates"]} == {a["slug"], b["slug"]}
    assert len(out[T.BLOCKS][0]["rows"]) == 2
    assert _stored(a["slug"])["status"] == "open" and _stored(b["slug"])["status"] == "open"
    assert "error" in T.update_todo("nothing like this", title="x")


def test_unconfirmed_delete_writes_nothing_and_returns_a_delete_row(T, tmp_path):
    it = _make("Test delete me")
    before = sorted(p.name for p in (tmp_path / "todos").iterdir())
    out = T.delete_todo("delete me")
    assert out["deleted"] is False
    assert sorted(p.name for p in (tmp_path / "todos").iterdir()) == before
    assert _stored(it["slug"])["status"] == "open"
    act = out[T.BLOCKS][0]["rows"][0]["action"]
    assert act["method"] == "DELETE" and act["path"] == f"/api/brain/api/todos/{it['slug']}"
    assert T.delete_todo("delete me", confirmed="yes")["deleted"] is False


def test_confirmed_delete_removes_the_todo(T, tmp_path):
    it = _make("Test delete me")
    out = T.delete_todo("delete me", confirmed=True)
    assert out["deleted"] is True and out["todo"]["slug"] == it["slug"]
    assert not (tmp_path / "todos" / f"{it['slug']}.md").exists()
    assert "error" in T.delete_todo(it["slug"], confirmed=True)


def test_rest_delete_route(T):
    import brain_todos
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(brain_todos.create_router())
    it = _make("Test delete me")
    c = TestClient(app)
    assert c.delete(f"/todos/{it['slug']}").status_code == 200
    assert c.delete(f"/todos/{it['slug']}").status_code == 404


def test_stop_repeating_keeps_the_next_open_day(T):
    import brain_todos
    it = _make("Team Standup", start="2026-06-08T09:30:00", end="2026-06-08T10:00:00",
               rrule="FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", category="work")
    brain_todos.update(it["slug"], {"done_date": "2026-10-26"})
    out = T.update_todo("standup", repeat="none")
    assert out["ok"]
    md = _stored(it["slug"])
    assert "rrule" not in md and "done_dates" not in md
    assert str(md["start"]) == "2026-10-27T09:30:00" and str(md["end"]) == "2026-10-27T10:00:00"


def test_stop_repeating_on_a_named_day_and_set_a_repeat(T):
    it = _make("Daily walk", start="2026-09-19T16:00:00", rrule="FREQ=DAILY", category="health")
    T.update_todo("walk", repeat="off", date="friday")
    md = _stored(it["slug"])
    assert "rrule" not in md and str(md["start"]) == "2026-10-30T16:00:00"
    T.update_todo("walk", repeat="every Monday and Thursday")
    assert _stored(it["slug"])["rrule"] == "FREQ=WEEKLY;BYDAY=MO,TH"
    assert "error" in T.update_todo("walk", repeat="now and then")


def test_repeat_is_said_and_shown():
    from voice import todos
    assert todos.repeat_text("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR") == "every weekday"
    assert todos.repeat_text("FREQ=MONTHLY") == "every month"
    assert todos.repeat_text("FREQ=WEEKLY;INTERVAL=2") == "every 2 weeks"
    assert todos._when({"start": "2026-06-08T09:30:00", "rrule": "FREQ=DAILY"}) == "every day, 09:30"


def test_create_resolves_a_phrase_and_shows_the_row(T):
    out = T.create_todo(title="Buy stamps", category="errand", due="tomorrow", session_id="s", turn_id=1)
    assert out["due"] == "2026-10-25" and out[T.BLOCKS][0]["title"] == "Added"


def _call(name, args, cid="c1"):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_app_turn_draws_the_delete_row_with_its_action(T, tmp_path, monkeypatch):
    from voice import agent, appchat, transcript
    monkeypatch.setattr(transcript, "_TRANSCRIPT_DIR", str(tmp_path / "voice"))
    monkeypatch.setattr(agent, "_HISTORY", {})
    monkeypatch.setattr(appchat, "build_brief", lambda: "(brief stub)")
    it = _make("Test delete me")
    rounds = iter([[("calls", [_call("delete_todo", {"target": it["slug"]})])],
                   [("text", "Tap Delete if you're sure."), ("calls", [])]])
    seen = []
    monkeypatch.setattr(appchat, "_stream", lambda m, model=None: (seen.append(m), iter(next(rounds)))[1])
    out = appchat.collect("app", "delete the test todo")
    assert out["blocks"][0]["rows"][0]["action"]["method"] == "DELETE"
    assert _stored(it["slug"])["status"] == "open"
    tool_msg = json.loads(seen[1][-1]["content"])
    assert T.BLOCKS not in tool_msg and tool_msg["deleted"] is False


def test_model_written_rows_still_lose_actions():
    from voice import blocks
    b = blocks.clean([{"type": "rows", "rows": [{"title": "x", "action": {
        "label": "Delete", "method": "DELETE", "path": "/api/brain/api/todos/x"}}]}])
    assert "action" not in b[0]["rows"][0]


def test_whatsapp_path_strips_rows(T):
    from voice import agent
    it = _make("Strip me")
    res = agent._run_tool("mark_done", json.dumps({"target": it["slug"]}))
    assert res["ok"] and agent.todos.BLOCKS not in res
