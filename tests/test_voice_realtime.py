"""Real local WebSockets, provider-shaped events, and the public call interface.

Import voice inside the test so the suite's temp-vault guard runs first.
"""
import asyncio
import base64
import json

import pytest
from websockets.asyncio.server import serve


TOOLS = [{"type": "function", "function": {
    "name": "lookup", "description": "Read a value",
    "parameters": {"type": "object", "properties": {"key": {"type": "string"}},
                   "required": ["key"]},
}}]
PCM = b"\x01\x00" * 24000
USAGE = {"input_tokens": 130, "output_tokens": 60,
         "input_token_details": {"audio_tokens": 100, "text_tokens": 30,
                                 "cached_tokens_details": {"audio_tokens": 20, "text_tokens": 10}},
         "output_token_details": {"audio_tokens": 50, "text_tokens": 10}}


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


async def exercise(provider, script, *, greet=True, monkeypatch=None):
    from voice import realtime

    clock = Clock()
    if monkeypatch is not None:
        # Patch this module's clock, not asyncio's process-wide monotonic clock.
        class Time:
            monotonic = clock
        monkeypatch.setattr(realtime, "time", Time)
    frames, turns, tool_args = [], [], []
    queue = asyncio.Queue()
    tool_entered, tool_release = asyncio.Event(), asyncio.Event()

    async def send_json(event):
        frames.append(event)
        queue.put_nowait(event)

    async def send_audio(audio):
        frames.append(("audio", audio))
        queue.put_nowait(("audio", audio))

    async def run_tool(name, arguments):
        tool_entered.set()
        await tool_release.wait()
        tool_args.append((name, arguments))
        return {"value": 7}

    async def until(kind, **fields):
        while True:
            event = await queue.get()
            if isinstance(event, dict) and event.get("type") == kind:
                if all(event.get(k) == v for k, v in fields.items()):
                    return event

    done = asyncio.get_running_loop().create_future()

    async def handler(ws):
        async def emit(kind, **fields):
            await ws.send(json.dumps({"type": kind, **fields}))

        async def receive(kind):
            event = json.loads(await ws.recv())
            assert event["type"] == kind, event
            return event

        try:
            assert ws.request.headers["Authorization"] == "Bearer test-key"
            await emit("session.created", session={"id": "session-1"})
            config = await receive("session.update")
            session = config["session"]
            assert session["instructions"] == "Full system instructions"
            assert session["tools"] == [{"type": "function", **TOOLS[0]["function"]}]
            assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
            assert session["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 24000}
            if provider == "openai":
                assert session["type"] == "realtime"
                assert session["output_modalities"] == ["audio"]
                assert session["audio"]["output"]["voice"] == "alloy"
                assert session["audio"]["input"]["turn_detection"] == {
                    "type": "semantic_vad", "create_response": True, "interrupt_response": False}
                assert session["audio"]["input"]["transcription"]["model"] == "gpt-4o-mini-transcribe"
            else:
                assert session["voice"] == "eve"
                assert session["turn_detection"] == {"type": "server_vad"}
                assert session["audio"]["input"]["transcription"] == {"model": "grok-transcribe"}
            await emit("session.updated", session={"id": "session-1", **session})
            await until("state", state="listening")
            await script(call, emit, receive, until, clock, turns, tool_args,
                         tool_entered, tool_release)
            done.set_result(None)
        except BaseException as exc:
            if not done.done():
                done.set_exception(exc)

    async with serve(handler, "127.0.0.1", 0) as server:
        call = realtime.RealtimeCall(
            provider=provider, api_key="test-key",
            model="grok-voice-latest" if provider == "xai" else "gpt-realtime-mini",
            voice="eve" if provider == "xai" else "alloy",
            instructions="Full system instructions", tools=TOOLS,
            run_tool=run_tool, send_json=send_json, send_audio=send_audio,
            on_turn=lambda role, text: turns.append((role, text)), greet=greet,
            url=f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}",
        )
        try:
            await call.start()
            pump = asyncio.create_task(call.run())
            try:
                await done
                await pump
            finally:
                await call.close()
                await pump
        finally:
            await call.close()
    return frames, turns


def state(value):
    return {"type": "state", "state": value}


def reply(n):
    return {"type": "reply", "id": n}


def finished(n):
    return {"type": "reply_done", "id": n}


def assistant(n, text, final=False):
    return {"type": "assistant", "id": n, "text": text, "final": final}


def metrics(n, provider, cost_xai, *, eos=None, ttfa=None):
    # (80*10 + 20*.3 + 20*.6 + 10*.06 + 50*20 + 10*2.4)/1M.
    event = {"type": "metrics", "id": n,
             "cost": pytest.approx(cost_xai if provider == "xai" else 0.0018426)}
    if eos is not None:
        event["eos_ms"] = eos
    if ttfa is not None:
        event["ttfa_ms"] = ttfa
    return event


@pytest.mark.parametrize("provider,legacy", [("openai", False), ("xai", True), ("xai", False)])
def test_call_greeting_transcription_tools_and_barge_in(provider, legacy, monkeypatch):
    audio = "response.audio" if legacy else "response.output_audio"
    transcript = "response.audio_transcript" if legacy else "response.output_audio_transcript"

    async def script(call, emit, receive, until, clock, turns, tool_args, entered, release):
        async def begin(n, item=True):
            await emit("response.created", response={"id": f"r{n}", "status": "in_progress"})
            if item:
                await emit("response.output_item.added", response_id=f"r{n}",
                           item={"id": f"a{n}", "type": "message", "role": "assistant"})

        async def delta(n, text):
            await emit(audio + ".delta", response_id=f"r{n}", item_id=f"a{n}",
                       content_index=0, delta=base64.b64encode(PCM).decode())
            await emit(transcript + ".delta", response_id=f"r{n}", item_id=f"a{n}", delta=text)

        async def end(n, status="completed"):
            await emit("response.done", response={"id": f"r{n}", "status": status, "usage": USAGE})
            await until("metrics", id=n)

        await receive("response.create")
        await begin(1)
        clock.now = 100.25
        await delta(1, "Hello there.")
        await until("assistant", id=1, final=False)
        await emit(audio + ".done", response_id="r1", item_id="a1")
        await emit(transcript + ".done", response_id="r1", transcript="Hello there.")
        clock.now = 101.0
        await end(1)
        assert turns == []  # Generated audio is not evidence of playback.
        await call.played(1, 1000, True)
        assert turns == [("assistant", "Hello there.")]

        await call.mute(True)
        await call.feed(b"muted")
        await call.mute(False)
        await call.feed(b"\x02\x00" * 480)
        chunk = await receive("input_audio_buffer.append")
        assert base64.b64decode(chunk["audio"]) == b"\x02\x00" * 480
        await emit("input_audio_buffer.speech_started", item_id="u1", audio_start_ms=0)
        await until("state", state="hearing")
        clock.now = 102.0
        await emit("input_audio_buffer.speech_stopped", item_id="u1", audio_end_ms=900)
        await until("state", state="thinking")
        if provider == "xai":
            await emit("conversation.item.input_audio_transcription.updated", item_id="u1", transcript="look up the wrong")
            await emit("conversation.item.input_audio_transcription.updated", item_id="u1", transcript="Look up seven.")
        else:
            await emit("conversation.item.input_audio_transcription.delta", item_id="u1", delta="Look up ")
            await emit("conversation.item.input_audio_transcription.delta", item_id="u1", delta="seven.")
        await emit("conversation.item.input_audio_transcription.completed", item_id="u1", transcript="Look up seven.")
        await until("user", turn=1)

        await begin(2, item=False)
        await emit("response.output_item.added", response_id="r2", item={
            "type": "function_call", "id": "f1", "call_id": "c1", "name": "lookup"})
        await emit("response.function_call_arguments.delta", response_id="r2", call_id="c1", delta='{"key":')
        await emit("response.function_call_arguments.done", response_id="r2", call_id="c1",
                   name="lookup", arguments='{"key":"seven"}')
        await entered.wait()
        assert tool_args == []
        # The pump must still process provider events while the tool is waiting.
        clock.now = 103.0
        await end(2)
        release.set()
        output = await receive("conversation.item.create")
        assert output["item"] == {"type": "function_call_output", "call_id": "c1",
                                  "output": '{"value": 7}'}
        await receive("response.create")
        assert tool_args == [("lookup", '{"key":"seven"}')]

        await begin(3)
        clock.now = 103.2
        await delta(3, "Seven is the answer.")
        await until("assistant", id=3, final=False)
        await call.played(3, 400, False)
        await emit("input_audio_buffer.speech_started", item_id="u2", audio_start_ms=1500)
        await until("flush", id=3)
        assert await receive("conversation.item.truncate") == {
            "type": "conversation.item.truncate", "item_id": "a3",
            "content_index": 0, "audio_end_ms": 400}
        await receive("response.cancel")
        # The flush acknowledgement can give a later playback position.
        await call.played(3, 500, True)
        await emit(audio + ".delta", response_id="r3", item_id="a3",
                   delta=base64.b64encode(PCM).decode())
        await emit(transcript + ".done", response_id="r3", transcript="Seven is the answer. Extra unheard words.")
        clock.now = 104.0
        await end(3, status="cancelled")
        await emit("error", error={"message": "A provider error"})
        await until("error")

    frames, turns = asyncio.run(asyncio.wait_for(
        exercise(provider, script, monkeypatch=monkeypatch), 10))
    assert frames == [
        state("listening"), state("thinking"), reply(1), state("speaking"),
        ("audio", PCM), assistant(1, "Hello there."), assistant(1, "Hello there.", True),
        finished(1), metrics(1, provider, 0.08 / 60, ttfa=250), state("listening"),
        state("hearing"), state("thinking"),
        {"type": "user", "text": "Look up seven.", "turn": 1}, reply(2),
        {"type": "tool", "name": "lookup", "status": "start"},
        finished(2), metrics(2, provider, 2 * 0.08 / 60),
        {"type": "tool", "name": "lookup", "status": "done"},
        reply(3), state("speaking"), ("audio", PCM), assistant(3, "Seven is the answer."),
        {"type": "flush", "id": 3}, state("hearing"), assistant(3, "Seven is t", True),
        finished(3), metrics(3, provider, 0.08 / 60, eos=1200, ttfa=200),
        {"type": "error", "message": "A provider error"},
    ]
    assert turns == [("assistant", "Hello there."), ("user", "Look up seven."),
                     ("assistant", "Seven is t")]


@pytest.mark.parametrize("provider", ["openai", "xai"])
def test_typed_turn_interrupts_completed_but_still_playing_reply(provider, monkeypatch):
    async def script(call, emit, receive, until, clock, turns, *unused):
        await call.say("Say hello")
        assert (await receive("conversation.item.create"))["item"] == {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": "Say hello"}]}
        await receive("response.create")
        await emit("response.created", response={"id": "r1"})
        await emit("response.output_audio.delta", response_id="r1", item_id="a1",
                   delta=base64.b64encode(PCM).decode())
        await emit("response.output_audio_transcript.done", response_id="r1", transcript="Hello friend.")
        await until("assistant", id=1, final=True)
        clock.now = 101
        await emit("response.done", response={"id": "r1", "usage": USAGE})
        await until("metrics", id=1)
        await call.played(1, 400, False)
        await call.interrupt()
        assert await receive("conversation.item.truncate") == {
            "type": "conversation.item.truncate", "item_id": "a1", "content_index": 0, "audio_end_ms": 400}
        await call.played(1, 400, True)
        # The next frame must be typed input, with no cancellation of a done response.
        await call.say("Thanks")
        await receive("conversation.item.create")
        await receive("response.create")

    frames, turns = asyncio.run(asyncio.wait_for(
        exercise(provider, script, greet=False, monkeypatch=monkeypatch), 10))
    assert frames == [
        state("listening"), {"type": "user", "text": "Say hello", "turn": 1},
        state("thinking"), reply(1), state("speaking"), ("audio", PCM),
        assistant(1, "Hello friend.", True), finished(1),
        metrics(1, provider, 0.08 / 60 + 0.004, ttfa=0),
        {"type": "flush", "id": 1}, state("listening"), assistant(1, "Hello", True),
        {"type": "user", "text": "Thanks", "turn": 2}, state("thinking"),
    ]
    assert turns == [("user", "Say hello"), ("assistant", "Hello"), ("user", "Thanks")]


@pytest.mark.parametrize("provider", ["openai", "xai"])
@pytest.mark.parametrize("pending", ["playback", "tool"])
def test_new_speech_is_not_overwritten_by_late_playback_or_tool_completion(provider, pending, monkeypatch):
    async def script(call, emit, receive, until, clock, turns, tool_args, entered, release):
        await call.say("Hello")
        await receive("conversation.item.create")
        await receive("response.create")
        await emit("response.created", response={"id": "r1"})
        if pending == "playback":
            await emit("response.output_audio.delta", response_id="r1", item_id="a1",
                       delta=base64.b64encode(PCM).decode())
            await emit("response.output_audio_transcript.done", response_id="r1", transcript="Hello friend.")
            await until("assistant", id=1, final=True)
            await call.played(1, 200, False)
        else:
            await emit("response.function_call_arguments.done", response_id="r1", call_id="c1",
                       name="lookup", arguments="{}")
            await entered.wait()
        clock.now = 101
        await emit("response.done", response={"id": "r1", "usage": USAGE})
        await until("metrics", id=1)
        await emit("input_audio_buffer.speech_started", item_id="u2")
        await until("state", state="hearing")
        if pending == "playback":
            assert (await receive("conversation.item.truncate"))["audio_end_ms"] == 200
            await call.played(1, 400, True)
        else:
            release.set()
            assert (await receive("conversation.item.create"))["item"]["type"] == "function_call_output"
            await until("tool", status="done")
        # A stale tool continuation or cancellation would arrive before this marker.
        await call.feed(b"\x02\x00")
        await receive("input_audio_buffer.append")

    frames, turns = asyncio.run(asyncio.wait_for(
        exercise(provider, script, greet=False, monkeypatch=monkeypatch), 10))
    expected = [state("listening"), {"type": "user", "text": "Hello", "turn": 1},
                state("thinking"), reply(1)]
    if pending == "playback":
        expected += [state("speaking"), ("audio", PCM), assistant(1, "Hello friend.", True)]
    else:
        expected += [{"type": "tool", "name": "lookup", "status": "start"}]
    expected += [finished(1), metrics(1, provider, 0.08 / 60 + 0.004,
                                    ttfa=0 if pending == "playback" else None)]
    if pending == "playback":
        expected += [{"type": "flush", "id": 1}]
    expected += [state("hearing")]
    if pending == "playback":
        expected += [assistant(1, "Hello", True)]
        assert turns == [("user", "Hello"), ("assistant", "Hello")]
    else:
        expected += [{"type": "tool", "name": "lookup", "status": "done"}]
        assert turns == [("user", "Hello")]
    assert frames == expected
