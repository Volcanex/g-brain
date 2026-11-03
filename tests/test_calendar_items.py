"""One calendar: every item has a date, no duplicates, the agenda contract, and
Voice confirming only what a tool actually did. Every record stays in a temp vault."""
import datetime as dt
import json

import frontmatter
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def todos(tmp_path, monkeypatch):
    import brain_todos
    monkeypatch.setattr(brain_todos, "TODOS_DIR", tmp_path / "todos")
    return brain_todos


@pytest.fixture
def client(todos):
    app = FastAPI()
    app.include_router(todos.create_router(), prefix="/api")
    return TestClient(app)


def _day(n):
    return (dt.date.today() + dt.timedelta(days=n)).isoformat()


# ------------------------------------------------------------- 1. a date is required

def test_create_refuses_an_undated_item_with_an_actionable_reason(todos, client):
    with pytest.raises(ValueError, match="Ask Gabriel when"):
        todos.create("Buy a present", "P2", requester="gabriel")
    with pytest.raises(ValueError, match="doesn't belong on the calendar"):
        todos.create_todo("Buy a present", "P2", requester="gabriel")
    r = client.post("/api/todos", json={"title": "Buy a present", "priority": "P2"})
    assert r.status_code == 400 and "Ask Gabriel when" in r.json()["detail"]
    with pytest.raises(ValueError, match="Ask Gabriel when"):
        todos.upsert("present", {"title": "Buy a present"}, "", {})
    assert list(todos.iter_todos()) == []


def test_due_start_or_rrule_each_count_as_a_date(todos):
    assert todos.create("Dentist", "P2", due=_day(3))["status"] == "open"
    assert todos.create("Call", "P2", start=_day(2) + "T15:00:00")["status"] == "open"
    assert todos.create("Bins", "P2", rrule="FREQ=WEEKLY", start=_day(1))["status"] == "open"
    with pytest.raises(ValueError, match="Unreadable date"):
        todos.create("Bad", "P2", due="next-ish")


def test_update_cannot_clear_the_date(todos, client):
    item = todos.create("Dentist", "P2", due=_day(3))
    with pytest.raises(ValueError, match="Cannot clear the date"):
        todos.update(item["slug"], {"due": None})
    assert client.patch(f"/api/todos/{item['slug']}", json={"due": None}).status_code == 400
    assert todos.get(item["slug"])["due"] == _day(3)
    # Moving it is fine, and so is closing a legacy undated item.
    assert todos.update(item["slug"], {"due": _day(5)})["due"] == _day(5)
    todos._save("legacy", frontmatter.Post("", title="Legacy", status="open", priority="P2"))
    assert todos.update("legacy", {"status": "cancelled"})["status"] == "cancelled"


def test_expiry_for_dated_items_unchanged_and_none_for_undated(todos):
    assert todos._default_expires("P2", "2026-01-10", None) == "2026-02-09"
    assert todos._default_expires("P0", "2026-01-10", None) is None
    assert todos._default_expires("P2", "2026-01-10", "FREQ=DAILY") is None
    assert todos._default_expires("P3", None, None) is None


# ------------------------------------------------------------- 2. no duplicates

def test_same_title_same_day_returns_the_existing_item(todos, client):
    first = todos.create("Rory's birthday", "P2", due="2026-11-27", requester="gabriel")
    again = todos.create("rorys  Birthday!", "P2", due="2026-11-27", requester="gabriel")
    assert again["duplicate"] is True and again["slug"] == first["slug"]
    r = client.post("/api/todos", json={"title": "Rory's birthday", "priority": "P2", "due": "2026-11-27"})
    assert r.json()["duplicate"] is True and r.json()["slug"] == first["slug"]
    assert len(list(todos.iter_todos())) == 1
    # Another day is another item; a closed one does not block a new one.
    assert "duplicate" not in todos.create("Rory's birthday", "P2", due="2027-11-27")
    todos.update(first["slug"], {"status": "done"})
    assert "duplicate" not in todos.create("Rory's birthday", "P2", due="2026-11-27")


def test_recurring_duplicate_is_same_title_and_rrule(todos):
    first = todos.create("Rory's birthday", "P2", due="2026-11-27", rrule="FREQ=YEARLY")
    again = todos.create("Rory's Birthday", "P2", due="2025-11-27", rrule="freq=yearly")
    assert again["duplicate"] is True and again["slug"] == first["slug"]
    assert "duplicate" not in todos.create("Rory's birthday", "P2", due="2026-11-27", rrule="FREQ=MONTHLY")


# ------------------------------------------------------------- 6. the agenda contract

ITEM_KEYS = {"slug", "title", "date", "time", "all_day", "category", "amount", "recurring"}


def test_agenda_sections_lines_and_bills(todos, client):
    today = dt.date.today()
    todos.create("Water and energy", "P1", due=_day(-3))
    todos.create("Interview", "P1", start=_day(0) + "T16:30:00", end=_day(0) + "T17:00:00")
    todos.create("Fly to Stockholm", "P1", start=_day(1) + "T14:15:00")
    todos.create("Dissertation", "P0", due=_day(4))
    todos.create("ChatGPT £118.34", "P2", category="finance", amount=118.34,
                 start=_day(5) + "T09:00:00", rrule="FREQ=MONTHLY")
    todos.create("Webshare £7.24", "P2", category="finance", amount=7.24,
                 start=_day(2) + "T09:00:00", rrule="FREQ=MONTHLY")
    todos.create("Far away", "P2", due=_day(30))
    # A yearly item whose first date is in the past expands to its next occurrence.
    todos.create("Anniversary", "P2", due=(today - dt.timedelta(days=365 - 3)).isoformat(),
                 rrule="FREQ=YEARLY")
    a = client.get("/api/todos/agenda").json()
    assert set(a) == {"generated", "overdue", "today", "tomorrow", "week", "bills", "lines"}
    for key in ("overdue", "today", "tomorrow", "week", "bills"):
        for it in a[key]:
            assert set(it) == ITEM_KEYS
    assert [i["title"] for i in a["overdue"]] == ["Water and energy"]
    assert [(i["title"], i["time"], i["all_day"]) for i in a["today"]] == [("Interview", "16:30", False)]
    assert [i["title"] for i in a["tomorrow"]] == ["Fly to Stockholm"]
    week = {i["title"]: i for i in a["week"]}
    assert set(week) == {"Dissertation", "ChatGPT £118.34", "Anniversary"}   # under £50 left out
    assert week["Anniversary"]["recurring"] is True and week["Anniversary"]["date"] > today.isoformat()
    assert week["Dissertation"]["all_day"] is True and week["Dissertation"]["time"] is None
    assert [(b["title"], b["amount"]) for b in a["bills"]] == [("ChatGPT £118.34", 118.34)]
    assert len(a["lines"]) == 3
    assert a["lines"][0].startswith("Today: 16:30 Interview.") and "Overdue: Water and energy" in a["lines"][0]
    assert a["lines"][1] == "Tomorrow: 14:15 Fly to Stockholm."
    third = a["lines"][2]
    assert third.startswith("This week: ") and "Dissertation" in third and "Anniversary" in third
    assert third.index("Anniversary") < third.index("Dissertation")   # day+3 before day+4
    day5 = (today + dt.timedelta(days=5)).strftime("%a")
    assert third.endswith(f"£118.34 ChatGPT {day5}")
    assert "Webshare" not in " ".join(a["lines"])
    assert a == {**todos.agenda(), "generated": a["generated"]}   # deterministic
    assert todos.agenda_tool()["lines"] == a["lines"]


def test_a_bill_tomorrow_is_said_once_on_the_bills_line(todos):
    todos.create("Rent £650", "P1", category="finance", amount=650, due=_day(1))
    a = todos.agenda()
    assert [i["title"] for i in a["tomorrow"]] == ["Rent £650"]
    day = (dt.date.today() + dt.timedelta(days=1)).strftime("%a")
    assert a["lines"] == [f"This week: £650.00 Rent {day}"]


def test_this_week_line_caps_at_four_names(todos):
    for n in range(2, 8):
        todos.create(f"Deadline {n}", "P1", due=_day(n))
    line = todos.agenda()["lines"][-1]
    d2 = (dt.date.today() + dt.timedelta(days=2)).strftime("%a")
    assert line.startswith(f"This week: {d2} Deadline 2 · ") and line.endswith(" +2 more")
    assert line.count(" · ") == 3


def test_agenda_leaves_out_empty_lines_and_ticked_occurrences(todos):
    item = todos.create("Walk", "P2", start=_day(0) + "T08:00:00", rrule="FREQ=DAILY")
    todos.update(item["slug"], {"done_date": _day(0)})
    a = todos.agenda()
    assert a["today"] == [] and [i["date"] for i in a["tomorrow"]] == [_day(1)]
    assert a["lines"] == ["Tomorrow: 08:00 Walk."]
    todos.update(item["slug"], {"status": "cancelled"})
    assert todos.agenda()["lines"] == []


def test_agenda_reports_an_unreadable_store(todos, client, monkeypatch, tmp_path):
    monkeypatch.setattr(todos, "TODOS_DIR", tmp_path / "missing")
    a = client.get("/api/todos/agenda").json()
    assert len(a["lines"]) == 1 and a["lines"][0].startswith("Calendar unavailable: ")
    assert a["today"] == [] and a["week"] == []


def test_pulse_shows_the_agenda_not_a_raw_list(todos):
    from g_pulse import pulse
    todos.create("Fly to Stockholm", "P1", start=_day(1) + "T14:15:00")
    todos.create("Dissertation", "P0", due=_day(3))
    text = pulse._agenda_block()
    assert text.startswith("Calendar:\nTomorrow: 14:15 Fly to Stockholm.")
    assert "Later this week:" in text and "Dissertation" in text
    payload = pulse.composed_payload()
    assert payload.startswith("Calendar:\n") and "Dated, next 7 days" not in payload


def test_subs_items_carry_a_numeric_amount(todos, monkeypatch):
    from inlets import subs
    monkeypatch.setattr(subs, "_fetch", lambda: [{
        "id": "chatgpt", "label": "ChatGPT", "amount": 118.34, "status": "watch", "seen": True,
        "lapsed": False, "next_due": _day(3), "cycle": "monthly", "last_charged": _day(-27),
        "charges": 3}])
    subs.run()
    assert todos.serialize("sub-chatgpt", todos.get("sub-chatgpt"))["amount"] == 118.34
    assert [b["slug"] for b in todos.agenda()["bills"]] == ["sub-chatgpt"]


# ------------------------------------------------------------- voice: confirm only on success

def test_guard_replaces_a_confirmation_no_tool_backed():
    from voice import agent
    assert agent.claims_action("Done - it's on the calendar.")
    assert not agent.claims_action("Nothing was cancelled yet.")
    assert not agent.claims_action("When is it?")
    said = "Done, added Rory's birthday."
    assert agent.confirm_guard(said, []) != said
    assert "nothing changed" in agent.confirm_guard(said, [("create_todo", {"error": "No date."})])
    pending = ("delete_todo", {"ok": False, "deleted": False, "needs_confirmation": True})
    assert agent.confirm_guard("Deleted it.", [pending]) != "Deleted it."
    assert agent.confirm_guard(said, [("create_todo", {"slug": "rory", "title": "x"})]) == said
    assert agent.confirm_guard("Your week is quiet.", []) == "Your week is quiet."


def test_handle_does_not_confirm_when_no_tool_ran(todos, tmp_path, monkeypatch):
    from voice import agent, transcript
    monkeypatch.setattr(transcript, "_TRANSCRIPT_DIR", str(tmp_path / "t"))
    monkeypatch.setattr(agent, "build_brief", lambda: "")
    monkeypatch.setattr(agent, "_call_model", lambda m, use_tools=True: {"content": "Done, it's added."})
    reply = agent.handle("fake-guard-session", "Remind me to call Rory")
    assert "haven't changed anything" in reply
    assert list(todos.iter_todos()) == []


def test_handle_refuses_undated_create_and_says_so(todos, tmp_path, monkeypatch):
    from voice import agent, transcript
    monkeypatch.setattr(transcript, "_TRANSCRIPT_DIR", str(tmp_path / "t"))
    monkeypatch.setattr(agent, "build_brief", lambda: "")
    rounds = iter([
        {"content": None, "tool_calls": [{"id": "c1", "function": {
            "name": "create_todo", "arguments": json.dumps({"title": "Call Rory", "category": "social"})}}]},
        {"content": "Added."},
    ])
    monkeypatch.setattr(agent, "_call_model", lambda m, use_tools=True: next(rounds))
    reply = agent.handle("fake-guard-session-2", "Remind me to call Rory")
    assert reply.startswith("That didn't go through") and "Ask Gabriel when" in reply
    assert list(todos.iter_todos()) == []


def test_tool_calls_are_capped_per_turn(todos, tmp_path, monkeypatch):
    from voice import agent, config, transcript
    monkeypatch.setattr(transcript, "_TRANSCRIPT_DIR", str(tmp_path / "t"))
    monkeypatch.setattr(agent, "build_brief", lambda: "")
    ran, seen_tools = [], []
    monkeypatch.setitem(agent.DISPATCH, "brain_search", lambda **k: ran.append(k) or [])

    def model(messages, use_tools=True):
        seen_tools.append(use_tools)
        if len(seen_tools) > 3:
            return {"content": "Here is what I found."}
        return {"content": None, "tool_calls": [
            {"id": f"c{len(seen_tools)}-{i}", "function": {"name": "brain_search",
                                                         "arguments": json.dumps({"query": str(i)})}}
            for i in range(4)]}
    monkeypatch.setattr(agent, "_call_model", model)
    agent.handle("fake-cap-session", "find everything")
    assert len(ran) == config.MAX_TOOL_CALLS
    assert seen_tools[-1] is False   # past the cap the model is asked without tools


def test_unconfirmed_delete_is_not_a_success(todos, monkeypatch):
    from voice import agent
    from voice import todos as T
    item = todos.create("Rory's birthday", "P2", due="2026-11-27", requester="gabriel")
    first = T.delete_todo(item["slug"])
    assert first["ok"] is False and first["needs_confirmation"] and not agent.succeeded("delete_todo", first)
    assert todos.get(item["slug"])
    gone = T.delete_todo(item["slug"], confirmed=True)
    assert gone["deleted"] is True and agent.succeeded("delete_todo", gone)
    assert list(todos.iter_todos()) == []


def test_voice_create_requires_and_resolves_a_date(todos):
    from voice import todos as T
    out = T.create_todo(title="Present for Kizz", category="personal")
    assert "Ask Gabriel when" in out["error"]
    made = T.create_todo(title="Interview", category="work", due=_day(1),
                         start=_day(1) + "T16:30:00")
    assert made["start"] == _day(1) + "T16:30:00" and made["due"] is None
    dup = T.create_todo(title="interview", category="work", due=_day(1), start=_day(1) + "T16:30:00")
    assert dup["duplicate"] is True and dup["_blocks"][0]["title"] == "Already there"


def test_create_tool_schema_requires_a_date():
    from voice import agent
    tool = next(t for t in agent.TOOLS if t["function"]["name"] == "create_todo")
    assert "due" in tool["function"]["parameters"]["required"]
    from voice.prompt import SYSTEM
    assert 'ask "When?" once' in SYSTEM
