"""Spoken turns: sentence splitting, the small-talk gate, and the streamed tool loop.

The model stream is stubbed; transcripts go to the temp dir conftest set up.
voice is imported inside the tests, never at collection (see conftest).
"""
import pytest


def test_sentences_wait_for_whitespace_then_flush_on_final():
    from voice.talk import _pop_sentences
    assert _pop_sentences("Hello there. How", False) == (["Hello there."], "How")
    assert _pop_sentences("It costs 3.", False) == ([], "It costs 3.")
    assert _pop_sentences("trailing words", True) == (["trailing words"], "")


def test_first_clause_goes_out_early():
    from voice.talk import _pop_sentences
    assert _pop_sentences("Going well, thanks, though I", False, eager=True) == (
        ["Going well, thanks,"], "though I")
    assert _pop_sentences("Going well, thanks, though I", False, eager=False) == (
        [], "Going well, thanks, though I")


def test_small_talk_gate():
    from voice.talk import is_small_talk
    assert is_small_talk("hey, how are you?")
    assert is_small_talk("thanks, cheers")
    assert not is_small_talk("remind me to call mum")
    assert not is_small_talk("what's on tomorrow")
    assert not is_small_talk(" ".join(["word"] * 12))


@pytest.fixture
def talk(tmp_path, monkeypatch):
    from voice import agent, talk, transcript
    monkeypatch.setattr(transcript, "_TRANSCRIPT_DIR", str(tmp_path / "voice"))
    monkeypatch.setattr(agent, "_HISTORY", {})
    monkeypatch.setattr(talk, "build_brief", lambda: "(brief stub)")
    return talk, agent


def test_small_talk_streams_without_tools(talk, monkeypatch):
    talk_mod, agent = talk
    seen = []

    def fake(messages, use_tools):
        seen.append(use_tools)
        for part in ["Doing well. ", "You?"]:
            yield "text", part
        yield "calls", []
    monkeypatch.setattr(talk_mod, "_stream_model", fake)
    evs = list(talk_mod.stream_turn("probe", "hey there"))
    assert [e["text"] for e in evs if e["t"] == "s"] == ["Doing well.", "You?"]
    assert seen == [False] and evs[-1]["small_talk"] is True
    assert agent._HISTORY["probe"][-1] == {"role": "assistant", "content": "Doing well. You?"}


def test_tool_turn_runs_tool_then_speaks(talk, monkeypatch):
    talk_mod, agent = talk
    rounds = iter([
        [("calls", [{"id": "c1", "type": "function",
                     "function": {"name": "list_todos", "arguments": "{}"}}])],
        [("text", "You have two things due."), ("calls", [])],
    ])
    monkeypatch.setattr(talk_mod, "_stream_model", lambda m, use_tools: iter(next(rounds)))
    monkeypatch.setattr(agent, "_run_tool", lambda name, args: {"todos": []})
    evs = list(talk_mod.stream_turn("probe", "what's due this week?"))
    assert [e["t"] for e in evs] == ["tool", "s", "done"]
    assert evs[-1]["tools"] == ["list_todos"] and evs[-1]["small_talk"] is False


def test_rate_limit_answers_without_model(talk, monkeypatch):
    from voice import talk as talk_mod
    calls = []

    def fake(messages, use_tools):
        calls.append(1)
        yield "text", "Hi."
        yield "calls", []
    monkeypatch.setattr(talk_mod, "_stream_model", fake)
    monkeypatch.setattr(talk_mod, "_RECENT", {})
    for _ in range(talk_mod.RATE_TURNS):
        list(talk_mod.stream_turn("rate-probe", "hey there"))
    evs = list(talk_mod.stream_turn("rate-probe", "hey there"))
    assert len(calls) == talk_mod.RATE_TURNS
    assert evs[0]["text"] == talk_mod.SLOW_DOWN
    assert evs[-1]["limited"] is True
