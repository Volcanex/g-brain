"""Current-turn intent must authorize writes, regardless of model tool calls."""
import json

import pytest


@pytest.mark.parametrize("name", ["create_todo", "update_todo", "reschedule_todo", "mark_done",
                                  "mark_failed", "drop_todo", "delete_todo", "pulse_note",
                                  "execute_commit"])
def test_app_question_refuses_every_write_before_dispatch(tmp_path, monkeypatch, name):
    from voice import agent, appchat, transcript
    monkeypatch.setattr(transcript, "_TRANSCRIPT_DIR", str(tmp_path / "voice"))
    monkeypatch.setattr(agent, "_HISTORY", {"app": [
        {"role": "user", "content": "add the Tamarix interview Thursday at 11"},
        {"role": "assistant", "content": "Added the interview."}]})
    monkeypatch.setattr(appchat, "build_brief", lambda: "Tamarix interview: Thursday at 11.")
    ran = []
    monkeypatch.setitem(agent.DISPATCH, name, lambda **kw: ran.append(kw) or {"ok": True})
    call = {"id": "c1", "type": "function", "function": {
        "name": name, "arguments": json.dumps({"confirmed": True} if name == "delete_todo" else {})}}
    rounds = iter([[("calls", [call])], [("text", "Added it."), ("calls", [])]])
    results = []

    def stream(messages, model=None):
        results[:] = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        return iter(next(rounds))

    monkeypatch.setattr(appchat, "_stream", stream)
    out = appchat.collect("app", "what's on this week?")
    assert ran == []
    assert results[0]["error"] and results[0]["changed"] is False
    assert out["blocks"] == [] and out["reply"] != "Added it."


@pytest.mark.parametrize("text", [
    "what's on this week?", "anything due tomorrow?", "what bills are coming?",
    "when's my flight?", "am I free Friday?", "what do I owe?",
    "did I mark the dissertation done?", "what date did you move Excelas to?",
    "I've got a dentist Thursday at 3?", "Friday am I free?",
    "don't add the dentist Thursday", "can you tell me what's due?",
    "remind me what I owe?", "can you remind me what's due tomorrow?", "", None,
])
def test_question_or_missing_turn_never_authorizes_a_write(text):
    from voice import agent
    assert not agent.requests_write(text)


@pytest.mark.parametrize("text", [
    "add dentist Thursday 3pm", "move the Excelas call to Friday 10am",
    "mark the dissertation done", "I've got drinks with Rory on the 27th at 7",
    "I've got a dentist Thursday at 3", "drop the gym class",
    "Can you please move the Excelas call to Friday?", "please delete the dentist",
    "I've got a flight tomorrow", "Thursday at 3", "yes, go ahead",
])
def test_changes_dictation_and_capture_followups_are_allowed(text):
    from voice import agent
    assert agent.requests_write(text)


@pytest.mark.parametrize("surface", ["handle", "talk"])
def test_other_text_paths_pass_the_current_question_to_the_guard(tmp_path, monkeypatch, surface):
    from voice import agent, talk, transcript
    monkeypatch.setattr(transcript, "_TRANSCRIPT_DIR", str(tmp_path / "voice"))
    monkeypatch.setattr(agent, "_HISTORY", {})
    monkeypatch.setattr(agent, "build_brief", lambda: "Tamarix interview Thursday at 11")
    monkeypatch.setattr(talk, "build_brief", agent.build_brief)
    ran = []
    monkeypatch.setitem(agent.DISPATCH, "create_todo", lambda **kw: ran.append(kw))
    call = {"id": "c1", "function": {"name": "create_todo", "arguments": "{}"}}
    if surface == "handle":
        rounds = iter([{"tool_calls": [call]}, {"content": "Added it."}])
        monkeypatch.setattr(agent, "_call_model", lambda *a, **kw: next(rounds))
        reply = agent.handle("intent-test", "what's on this week?")
    else:
        rounds = iter([[("calls", [call])], [("text", "Added it."), ("calls", [])]])
        monkeypatch.setattr(talk, "_RECENT", {})
        monkeypatch.setattr(talk, "_stream_model", lambda *a, **kw: iter(next(rounds)))
        list(talk.stream_turn("intent-test", "what's on this week?"))
        reply = agent._HISTORY["intent-test"][-1]["content"]
    assert not ran and "nothing changed" in reply


def test_write_dispatch_requires_a_user_turn(monkeypatch):
    from voice import agent
    ran = []
    monkeypatch.setitem(agent.DISPATCH, "create_todo", lambda **kw: ran.append(kw))
    assert agent._run_tool("create_todo", "{}")["changed"] is False
    assert not ran


def test_intent_permission_keeps_the_delete_confirmation_guard(tmp_path, monkeypatch):
    import brain_todos
    from voice import agent
    monkeypatch.setattr(brain_todos, "TODOS_DIR", tmp_path / "todos")
    item = brain_todos.create("Delete fixture", "P1", due="2099-01-01", requester="gabriel")
    args = json.dumps({"target": item["slug"]})
    pending = agent._run_tool("delete_todo", args, user_text="delete the fixture")
    assert pending["needs_confirmation"] and not pending["deleted"]
    confirmed = json.dumps({"target": item["slug"], "confirmed": True})
    assert agent._run_tool("delete_todo", confirmed, user_text="what's on this week?")["changed"] is False
    assert brain_todos.get(item["slug"])
    assert agent._run_tool("delete_todo", confirmed, user_text="yes")["deleted"] is True
