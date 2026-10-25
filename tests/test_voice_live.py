"""Live calls with scripted turn detection and OpenRouter, never the live vault."""
import asyncio
import base64
import hashlib
import io
import json
import os
import struct
import time
import wave
from collections import deque
from contextlib import asynccontextmanager

import httpx
import pytest
from starlette.websockets import WebSocketState


class ScriptedEndpointer:
    def __init__(self, rate=24000, **kwargs):
        self.rate = rate
        self.events = deque()
        self.frames = []
        self.in_speech = False
        self.resets = 0

    def feed(self, pcm):
        self.frames.append(pcm)
        events = self.events.popleft() if self.events else []
        for ev in events:
            self.in_speech = ev["type"] != "turn_end"
        return events

    def reset(self):
        self.resets += 1
        self.in_speech = False
        self.events.clear()


class Socket:
    def __init__(self):
        self.client_state = WebSocketState.CONNECTING
        self.inbox = asyncio.Queue()
        self.sent = []

    async def accept(self):
        self.client_state = WebSocketState.CONNECTED

    async def receive(self):
        return await self.inbox.get()

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def send_bytes(self, pcm):
        self.sent.append(pcm)

    async def close(self, **kwargs):
        self.client_state = WebSocketState.DISCONNECTED

    def control(self, **event):
        self.inbox.put_nowait({"type": "websocket.receive", "text": json.dumps(event)})

    def events(self, kind):
        return [e for e in self.sent if isinstance(e, dict) and e["type"] == kind]


async def until(predicate):
    async with asyncio.timeout(1.5):
        while not predicate():
            await asyncio.sleep(0.001)


def audio_delta(text="All good.", pcm=b"\x01\x00" * 480):
    return {"audio": {"data": base64.b64encode(pcm).decode(), "transcript": text}}


def tool_delta(name, arguments="{}", index=0, cid="t1"):
    return {"tool_calls": [{"index": index, "id": cid, "type": "function",
                            "function": {"name": name, "arguments": arguments}}]}


class SSE(httpx.AsyncByteStream):
    def __init__(self, deltas, cost, pause=False):
        self.deltas, self.cost, self.pause = deltas, cost, pause
        self.cancelled = False

    async def __aiter__(self):
        try:
            for delta in self.deltas:
                yield ("data: " + json.dumps({"choices": [{"delta": delta}]}) + "\n\n").encode()
            if self.pause:
                await asyncio.Event().wait()
            yield ("data: " + json.dumps({"choices": [], "usage": {"cost": self.cost}}) + "\n\n").encode()
            yield b"data: [DONE]\n\n"
        except asyncio.CancelledError:
            self.cancelled = True
            raise


@pytest.fixture
def live_env(monkeypatch, tmp_path):
    from voice import agent, live, talk, transcript, turn

    monkeypatch.setattr(turn, "Endpointer", ScriptedEndpointer)
    monkeypatch.setattr(agent, "_HISTORY", {})
    monkeypatch.setattr(live, "_RECENT", {})
    monkeypatch.setattr(talk, "_TURN", {})
    monkeypatch.setattr(talk, "_PROPOSED", {})
    monkeypatch.setattr(live, "build_brief", lambda: "A test brief.")
    monkeypatch.setattr(live, "FILLER_DIR", str(tmp_path / "fillers"))
    monkeypatch.setattr(live, "GREETINGS", {})
    monkeypatch.setattr(live, "GOODBYES", ())
    monkeypatch.setattr(live, "ACKS", ())
    monkeypatch.setattr(live, "_LAST_GREETING", {})
    monkeypatch.setattr(live.config, "LIVE_ENGINE", "openrouter")
    written = []
    monkeypatch.setattr(transcript, "append", lambda *args, **kw: written.append(args))
    monkeypatch.setattr(transcript, "load_tail", lambda *args: [])
    filler_dir = tmp_path / "fillers" / "marin"
    filler_dir.mkdir(parents=True)
    for i in range(len(live.FILLERS)):
        (filler_dir / f"{i}.pcm").write_bytes(b"\x02\x00" * 240)

    @asynccontextmanager
    async def session(streams=None, *, greet=False, sender="talk", stt="What is due?", handler=None, stt_gate=None):
        requests = []
        rounds = deque(streams or [SSE([audio_delta()], 0.0007)])

        async def respond(request):
            payload = json.loads(request.content)
            requests.append(payload)
            if payload["messages"][0]["content"] == live.STT_PROMPT:
                if stt_gate is not None:
                    await stt_gate.wait()
                return httpx.Response(200, json={"choices": [{"message": {"content": stt}}],
                                                "usage": {"cost": 0.0001}})
            if handler:
                return await handler(payload)
            return httpx.Response(200, stream=rounds.popleft(), headers={"content-type": "text/event-stream"})

        ws = Socket()
        call = live.Call(ws, sender, "marin", "test")
        await call.http.aclose()
        call.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with monkeypatch.context() as patch:
            patch.setattr(live, "Call", lambda *args: call)
            task = asyncio.create_task(live.serve(ws))
            ws.control(type="hello", client="test", session=sender, voice="marin", greet=greet)
            try:
                await until(lambda: ws.events("ready"))
                await until(lambda: call.fillers)
                yield live, call, ws, requests, written
            finally:
                ws.control(type="bye")
                await asyncio.wait_for(task, 1.5)
                await until(lambda: not call.background)

    return session


async def feed(call, ws, *events):
    call.ep.events.append(list(events))
    ws.inbox.put_nowait({"type": "websocket.receive", "bytes": b"\x01\x00" * 480})
    await until(lambda: not call.ep.events)


def turn_end(pcm=b"\x03\x00" * 12000, speech_ms=500):
    return {"type": "turn_end", "audio": pcm, "prob": 0.95, "reason": "smart_turn",
            "silence_ms": 200, "speech_ms": speech_ms}


async def remembered(live, call):
    await until(lambda: len(live.agent._HISTORY[call.sender]) >= 2 and not call.background)


def test_hello_greeting_is_not_remembered(live_env):
    async def run():
        async with live_env(greet=True) as (live, call, ws, requests, written):
            await until(lambda: ws.events("metrics"))
            ready = ws.events("ready")[0]
            assert ready["engine"] == "openrouter" and ready["rate"] == 24000
            assert ws.events("reply") == [{"type": "reply", "id": 1}]
            assert any(isinstance(e, bytes) for e in ws.sent)
            assert ws.events("reply_done") == [{"type": "reply_done", "id": 1}]
            assert ws.events("assistant")[-1] == {
                "type": "assistant", "id": 1, "text": "All good.", "final": True}
            assert ws.events("metrics")[0]["cost"] == 0.0007
            assert "tools" not in requests[0]
            assert live.agent._HISTORY["talk"] == [] and written == []
    asyncio.run(run())


def test_turn_audio_caption_metrics_and_history(live_env):
    async def run():
        async with live_env([SSE([audio_delta("Two "), audio_delta("things.")], 0.0007)]) as env:
            live, call, ws, requests, written = env
            await feed(call, ws, turn_end())
            await remembered(live, call)
            assert {"type": "state", "state": "thinking"} in ws.sent
            assert ws.events("reply") and ws.events("reply_done")
            assert len([e for e in ws.sent if isinstance(e, bytes)]) == 2
            assert ws.events("user") == [{"type": "user", "text": "What is due?", "turn": 1}]
            assert ws.events("assistant")[-1]["text"] == "Two things."
            metrics = ws.events("metrics")[0]
            assert 180 <= metrics["eos_ms"] < 1000
            assert 0 <= metrics["ttfa_ms"] < 1000
            assert metrics["cost"] == 0.0008
            assert live.agent._HISTORY["talk"] == [
                {"role": "user", "content": "What is due?"},
                {"role": "assistant", "content": "Two things."}]
            assert written == [("talk", "user", "What is due?"), ("talk", "assistant", "Two things.")]
            assert all(p["messages"][-1]["content"][0]["type"] == "input_audio" for p in requests)
    asyncio.run(run())


def test_barge_in_flushes_and_remembers_only_heard_words(live_env):
    stream = SSE([audio_delta("one two three four", b"\x01\x00" * 24000)], 0, pause=True)

    async def run():
        async with live_env([stream]) as (live, call, ws, requests, written):
            await feed(call, ws, turn_end())
            await until(lambda: ws.events("assistant"))
            ws.control(type="played", id=1, ms=500)
            await until(lambda: call.reply.played_ms == 500)
            await feed(call, ws, {"type": "speech_confirmed"})
            await remembered(live, call)
            assert ws.events("flush") == [{"type": "flush", "id": 1}]
            assert stream.cancelled and call.reply.task.cancelled()
            assert live.agent._HISTORY["talk"][-1]["content"] == live.heard("one two three four", 500, 1000)
            flushed = ws.sent.index(ws.events("flush")[0])
            assert not any(isinstance(e, bytes) for e in ws.sent[flushed + 1:])
            assert not ws.events("reply_done")
    asyncio.run(run())


def test_resume_before_audio_merges_turn_and_discards_reply(live_env):
    async def run():
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        count = 0

        async def handler(payload):
            nonlocal count
            count += 1
            if count == 1:
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return httpx.Response(200, stream=SSE([audio_delta("The whole answer.")], 0.0007))

        async with live_env(handler=handler) as (live, call, ws, requests, written):
            first, second = b"\x03\x00" * 7200, b"\x04\x00" * 7200
            await feed(call, ws, turn_end(first))
            await entered.wait()
            old = call.reply
            await feed(call, ws, {"type": "speech_start"})
            await cancelled.wait()
            await feed(call, ws, turn_end(second))
            await remembered(live, call)
            assert old.discard and not ws.events("flush")
            assert call.reply.pcm == first + second
            payload = [p for p in requests if p.get("stream")][-1]
            data = payload["messages"][-1]["content"][0]["input_audio"]["data"]
            with wave.open(io.BytesIO(base64.b64decode(data))) as wav:
                assert wav.readframes(wav.getnframes()) == first + second
            assert ws.events("reply") == [{"type": "reply", "id": 2}]
            assert len(written) == 2 and len(live.agent._HISTORY["talk"]) == 2
            assert ws.events("metrics")[0]["id"] == 2
    asyncio.run(run())


def test_short_turn_without_held_audio_is_ignored(live_env):
    async def run():
        async with live_env() as (live, call, ws, requests, written):
            await feed(call, ws, turn_end(speech_ms=live.MIN_TURN_MS - 1))
            assert call.reply is None and requests == [] and written == []
    asyncio.run(run())


def test_tool_loop_plays_filler_once_and_joins_tool_fragments(live_env, monkeypatch):
    rounds = [SSE([tool_delta("list_", '{"status":'),
                   {"tool_calls": [{"index": 0, "function": {"name": "todos", "arguments": '"open"}'}}]}], 0.0002),
              SSE([tool_delta("list_todos", cid="t2")], 0.0002),
              SSE([audio_delta("Nothing due.")], 0.0003)]

    async def run():
        async with live_env(rounds) as (live, call, ws, requests, written):
            ran = []
            monkeypatch.setattr(live.agent, "_run_tool", lambda *a, **kw: ran.append((a, kw)) or {"todos": []})
            await feed(call, ws, turn_end())
            await remembered(live, call)
            frames = [e for e in ws.sent if isinstance(e, bytes)]
            assert frames == [call.fillers[0], b"\x01\x00" * 480]
            assert [r[0][0] for r in ran] == ["list_todos", "list_todos"]
            assert json.loads(ran[0][0][1]) == {"status": "open"}
            assert [e["status"] for e in ws.events("tool")] == ["start", "done", "start", "done"]
            payload = [p for p in requests if p.get("stream")][-1]
            assert [m["role"] for m in payload["messages"]][-4:] == ["assistant", "tool", "assistant", "tool"]
            assert ws.events("metrics")[0]["cost"] == 0.0008
    asyncio.run(run())


def test_no_filler_when_the_model_says_it_is_checking(live_env, monkeypatch):
    first = b"\x03\x00" * 480
    rounds = [SSE([audio_delta("Let me check.", first), tool_delta("list_todos")], 0.0002),
              SSE([audio_delta("Nothing due.")], 0.0003)]

    async def run():
        async with live_env(rounds) as (live, call, ws, requests, written):
            monkeypatch.setattr(live.agent, "_run_tool", lambda *a, **kw: [])
            await feed(call, ws, turn_end())
            await remembered(live, call)
            frames = [e for e in ws.sent if isinstance(e, bytes)]
            assert frames == [first, b"\x01\x00" * 480]
            words = [e["text"] for e in ws.events("assistant") if not e["final"]]
            assert "".join(words) == "Let me check. Nothing due."
            assert [e for e in ws.events("assistant") if e["final"]][0]["text"] == "Let me check. Nothing due."
    asyncio.run(run())


@pytest.mark.parametrize("sender,name,error", [
    ("talk", "execute_commit", "no plan with that id was proposed on this call; call propose_commit first"),
    ("bench", "create_todo", "read-only session"),
])
def test_tool_gates_never_dispatch(live_env, monkeypatch, sender, name, error):
    async def run():
        rounds = [SSE([tool_delta(name, '{"plan_id":"missing","operations":[]}')], 0.0002),
                  SSE([audio_delta("I need confirmation.")], 0.0003)]
        async with live_env(rounds, sender=sender, stt="yes") as (live, call, ws, requests, written):
            def forbidden(*args, **kwargs):
                pytest.fail("gated tool reached the dispatcher")
            monkeypatch.setattr(live.agent, "_run_tool", forbidden)
            await feed(call, ws, turn_end())
            await remembered(live, call)
            streams = [p for p in requests if p.get("stream")]
            result = streams[-1]["messages"][-1]
            assert result["role"] == "tool" and json.loads(result["content"]) == {"error": error}
            if sender == "bench":
                assert all({t["function"]["name"] for t in p["tools"]} == live.READ_TOOLS | {"end_call"} for p in streams)
    asyncio.run(run())


def test_say_is_text_and_muted_audio_is_dropped(live_env):
    async def run():
        async with live_env() as (live, call, ws, requests, written):
            ws.control(type="mute", on=True)
            await until(lambda: call.muted)
            await call.on_audio(b"\x01\x00" * 480)
            assert call.ep.frames == [] and call.ep.resets == 1 and not requests
            ws.control(type="say", text="  Hello   there. ")
            await remembered(live, call)
            assert requests[0]["messages"][-1] == {"role": "user", "content": "Hello there."}
            assert len(requests) == 1 and not ws.events("user")
            ws.control(type="mute", on=False)
            await until(lambda: not call.muted)
            await call.on_audio(b"\x01\x00" * 480)
            assert len(call.ep.frames) == 1 and call.ep.resets == 2
    asyncio.run(run())


@pytest.mark.parametrize("played,audio,expected", [
    (0, 1000, ""), (500, 1000, "one two " + chr(8212)),
    (1000, 1000, "one two three four"), (2000, 1000, "one two three four"),
    (0, 0, "one two three four"), (-10, 1000, ""),
])
def test_heard(played, audio, expected):
    from voice.live import heard
    assert heard("one two three four", played, audio) == expected


@pytest.mark.parametrize("flags,expected,failed", [
    ([], 0, []),
    (["--max-eos-p50", "0", "--max-flush-ms", "0", "--max-turn-cost", "0"], 1,
     ["eos_p50", "flush", "turn_cost"]),
])
def test_bench_selftest_and_threshold_exit_codes(tmp_path, capsys, flags, expected, failed):
    from voice import live_bench
    output = tmp_path / "bench.json"
    tape = tmp_path / "call.wav"
    assert live_bench.main(["--selftest", "--turns", "1", "--json", str(output), "--record", str(tape),
                            *flags]) == expected
    with wave.open(str(tape)) as wav:
        assert wav.getframerate() == live_bench.RATE
        samples = struct.unpack(f"<{wav.getnframes()}h", wav.readframes(wav.getnframes()))
    assert 1000 in samples, "the first utterance is missing from the recording"
    report = json.loads(output.read_text())
    assert report["passed"] == (expected == 0)
    assert all(not report["checks"][key] for key in failed)
    assert report["captions_correct"] and report["interruption_answered"]
    assert report["selftest"]["mic_frames"] > 10
    assert report["selftest"]["renders"] == 3
    assert len(report["turns"]) == 4
    assert report["turns"][0]["eos_ms"] is None
    assert report["total_cost"] == 0.002
    assert report["flush_ms"] >= 0
    assert all(row["server"]["eos_ms"] == 20 for row in report["turns"])
    printed = capsys.readouterr().out
    assert "selftest: continuous mic" in printed and ("PASS" if not expected else "FAIL:") in printed


def test_bench_server_error_exits_two_and_writes_json(tmp_path, monkeypatch, capsys):
    from voice import live_bench
    from websockets.asyncio.server import serve

    async def server(ws):
        hello = json.loads(await ws.recv())
        assert hello["session"] == "bench" and hello["greet"] is True
        await ws.send(json.dumps({"type": "error", "message": "canned failure"}))
        await ws.wait_closed()

    async def failing_run(args):
        async with serve(server, "127.0.0.1", 0) as fake:
            url = f"ws://127.0.0.1:{fake.sockets[0].getsockname()[1]}/live"
            async with live_bench.connect(url, proxy=None) as ws:
                return await live_bench.Bench(ws).run([(live_bench.COUNT, b"\x01\x00" * 480),
                                                       (live_bench.STOP, b"\x01\x00" * 480)], "marin")

    monkeypatch.setattr(live_bench, "run_bench", failing_run)
    output = tmp_path / "error.json"
    assert live_bench.main(["--json", str(output)]) == 2
    assert json.loads(output.read_text()) == {"error": "canned failure", "passed": False}
    assert "bench error: canned failure" in capsys.readouterr().err


@pytest.mark.parametrize("expected,caption,match", [
    ("Okay stop, that's enough.", "okay stop thats enough", True),
    ("What's on my list today?", "What's on my list today", True),
    ("What time is it?", "I like trains", False),
    ("How's it going?", "", False),
])
def test_bench_caption_fuzzy_match(expected, caption, match):
    from voice.live_bench import caption_matches
    assert caption_matches(expected, caption) is match


def test_bench_bad_caption_and_ignored_interruption_fail_gate():
    from voice import live_bench
    args = live_bench.parser().parse_args([])
    report = {"eos_p50_ms": 100, "eos_p90_ms": 200, "flush_ms": 100, "total_cost": 0.001,
              "per_turn_cost": {"1": 0.001}, "captions_correct": False,
              "interruption_answered": False}
    assert live_bench.evaluate(report, args) == 1
    assert not report["checks"]["captions"] and not report["checks"]["interruption"]
    assert live_bench.acknowledges_stop("Okay, I'll stop.")
    assert not live_bench.acknowledges_stop("Four, five, six, seven.")


def test_bench_gate_picks_the_deciding_checks():
    from voice import live_bench
    report = {"eos_p50_ms": 2000, "eos_p90_ms": 2600, "flush_ms": 300, "total_cost": 0.02,
              "per_turn_cost": {"1": 0.003, "2": 0.005}, "captions_correct": True,
              "interruption_answered": True, "cut_cost_known": True}
    gate = lambda name: live_bench.evaluate(dict(report), live_bench.parser().parse_args(["--gate", name]))
    assert gate("latency") == 1
    assert gate("barge-in") == 0
    assert gate("cost") == 0          # the mean, $0.004, is the threshold, not the dearest turn
    assert gate("none") == 0
    assert gate("all") == 1


def test_bench_reuses_a_recent_run_only(tmp_path, monkeypatch):
    from voice import live_bench
    monkeypatch.setattr(live_bench, "LAST", tmp_path / "last.json")
    assert live_bench.recent(900) is None
    live_bench.remember({"eos_p50_ms": 1})
    assert live_bench.recent(900) == {"eos_p50_ms": 1}
    assert live_bench.recent(0) is None
    live_bench.remember({"error": "down", "passed": False})
    assert live_bench.recent(900) is None


def test_bench_rejects_non_pcm24k_tts(tmp_path):
    from voice import live_bench
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x01\x00" * 320)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, content=buf.getvalue()))) as client:
            with pytest.raises(ValueError, match="24 kHz mono PCM16"):
                await live_bench.render("Hello", tmp_path, client)
        assert list(tmp_path.iterdir()) == []
    asyncio.run(run())


def test_delayed_stt_does_not_block_audio_and_is_in_call_cost(live_env):
    async def run():
        stt_gate = asyncio.Event()
        async with live_env(stt_gate=stt_gate) as (live, call, ws, requests, written):
            await feed(call, ws, turn_end())
            await until(lambda: ws.events("reply_done"))
            assert any(isinstance(ev, bytes) for ev in ws.sent)
            assert not ws.events("user") and not ws.events("metrics")
            stt_gate.set()
            await remembered(live, call)
            assert ws.events("metrics")[0]["cost"] == 0.0008
            assert call.cost == pytest.approx(0.0008)
    asyncio.run(run())


def test_commit_approval_uses_reply_turn_and_consumes_one_plan(live_env, monkeypatch):
    rounds = [SSE([tool_delta("propose_commit")], 0),
              SSE([tool_delta("execute_commit", '{"plan_id":"first"}')], 0),
              SSE([{"content": "Please confirm the plan."}], 0),
              SSE([tool_delta("execute_commit", '{"plan_id":"first"}', index=0, cid="one"),
                   tool_delta("execute_commit", '{"plan_id":"first"}', index=1, cid="replay"),
                   tool_delta("execute_commit", '{"plan_id":"second"}', index=2, cid="two")], 0),
              SSE([{"content": "Saved the first plan."}], 0),
              SSE([tool_delta("execute_commit", '{"plan_id":"first"}')], 0),
              SSE([{"content": "That plan has already run."}], 0)]

    async def run():
        async with live_env(rounds) as (live, call, ws, requests, written):
            call.fillers = []
            dispatched = []

            def dispatch(name, args, **kwargs):
                dispatched.append(name)
                if name == "propose_commit":
                    live.talk._TURN[call.sender] += 10
                    return {"plan_id": "first"}
                assert "first" not in live.talk._PROPOSED[call.sender]
                return {"ok": True}

            monkeypatch.setattr(live.agent, "_run_tool", dispatch)
            ws.control(type="say", text="yes")
            await until(lambda: len(written) == 2 and not call.background)
            proposed = call.reply
            assert live.talk._PROPOSED[call.sender] == {"first": proposed.gturn}
            assert dispatched == ["propose_commit"]
            denied = json.loads(requests[2]["messages"][-1]["content"])
            assert "next turn" in denied["error"]

            live.talk._PROPOSED[call.sender]["second"] = proposed.gturn
            ws.control(type="say", text="yes")
            await until(lambda: len(written) == 4 and not call.background)
            results = [json.loads(m["content"]) for m in requests[4]["messages"]
                       if m["role"] == "tool"]
            assert results[0] == {"ok": True}
            assert "no plan" in results[1]["error"]
            assert "one yes" in results[2]["error"]
            assert dispatched == ["propose_commit", "execute_commit"]
            assert live.talk._PROPOSED[call.sender] == {"second": proposed.gturn}

            ws.control(type="say", text="yes")
            await until(lambda: len(written) == 6 and not call.background)
            assert "no plan" in json.loads(requests[6]["messages"][-1]["content"])["error"]
            assert dispatched == ["propose_commit", "execute_commit"]
    asyncio.run(run())


@pytest.fixture
def relay_env(live_env, monkeypatch):
    from voice import live, realtime

    class Relay:
        def __init__(self, **kwargs):
            self.callbacks = kwargs
            self.closed = False
            self.stopped = asyncio.Event()

        async def start(self):
            pass

        async def run(self):
            await self.stopped.wait()

        async def close(self):
            self.closed = True
            self.stopped.set()

    @asynccontextmanager
    async def session(engine="openai"):
        instances = []

        def make_relay(**kwargs):
            relay = Relay(**kwargs)
            instances.append(relay)
            return relay

        monkeypatch.setattr(realtime, "RealtimeCall", make_relay)
        ws = Socket()
        await ws.accept()
        task = asyncio.create_task(live._relay(ws, "talk", "alloy", engine, False))
        try:
            await until(lambda: ws.events("ready"))
            yield live, instances[0], ws, task
        finally:
            ws.control(type="bye")
            await asyncio.wait_for(task, 1.5)

    return session


@pytest.mark.parametrize("engine", ["openai", "xai"])
def test_realtime_commit_approval_cannot_be_reused(relay_env, monkeypatch, engine):
    async def run():
        async with relay_env(engine) as (live, relay, ws, task):
            dispatched = []
            live.talk._TURN["talk"] = 1
            live.talk._PROPOSED["talk"] = {"first": 1, "second": 1}

            def dispatch(name, args, **kwargs):
                assert "first" not in live.talk._PROPOSED["talk"]
                dispatched.append((name, args))
                return {"ok": True}

            monkeypatch.setattr(live.agent, "_run_tool", dispatch)
            relay.callbacks["on_turn"]("user", "yes")
            tool = relay.callbacks["run_tool"]
            assert await tool("execute_commit", '{"plan_id":"first"}') == {"ok": True}
            assert "clear yes" in (await tool("execute_commit", '{"plan_id":"second"}'))["error"]
            relay.callbacks["on_turn"]("user", "yes")
            assert "no plan" in (await tool("execute_commit", '{"plan_id":"first"}'))["error"]
            assert dispatched == [("execute_commit", '{"plan_id":"first"}')]
            assert live.talk._PROPOSED["talk"] == {"second": 1}
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["audio", "text"])
def test_greeting_counts_towards_limit_before_stt_or_reply(live_env, monkeypatch, kind):
    from voice import live

    monkeypatch.setattr(live, "RATE_REPLIES", 1)

    async def run():
        async with live_env(greet=True) as (live, call, ws, requests, written):
            await until(lambda: ws.events("metrics") and not call.background)
            greeting = call.reply
            await call.on_control({"type": "played", "id": greeting.id,
                                   "ms": greeting.audio_ms, "done": True})
            assert len(requests) == 1
            if kind == "audio":
                await feed(call, ws, turn_end())
            else:
                ws.control(type="say", text="Another turn.")
            await until(lambda: ws.events("error"))
            await asyncio.sleep(0)
            assert "too many turns" in ws.events("error")[0]["message"]
            assert len(requests) == 1
            assert call.reply is greeting and call.held == b""
            assert not call.background and written == []
    asyncio.run(run())


def test_replacement_wakes_playback_and_waits_for_ordered_history(live_env, monkeypatch):
    rounds = [SSE([audio_delta("one two three four", b"\x01\x00" * 240000)], 0),
              SSE([{"content": "The next answer."}], 0)]

    async def run():
        async with live_env(rounds) as (live, call, ws, requests, written):
            remembering, release = asyncio.Event(), asyncio.Event()
            original_remember = call.remember

            async def remember(reply):
                if reply.id == 1:
                    remembering.set()
                    await release.wait()
                await original_remember(reply)

            monkeypatch.setattr(call, "remember", remember)
            try:
                await feed(call, ws, turn_end())
                await until(lambda: ws.events("metrics"))
                previous = call.reply
                assert previous.task.done() and not previous.remembered.is_set()
                await call.on_control({"type": "say", "text": "Next question."})
                await call.on_control({"type": "played", "id": previous.id, "ms": 5000, "done": True})
                await asyncio.wait_for(remembering.wait(), 0.3)
                await until(lambda: call.state == "thinking")
                assert len([p for p in requests if p.get("stream")]) == 1
                assert not call.reply.task.done()
                release.set()
                await until(lambda: len(written) == 4 and not call.background)
                msgs = requests[-1]["messages"]
                assert msgs[-4:-2] == [
                    {"role": "user", "content": "What is due?"},
                    {"role": "assistant", "content": "one two —"}]
                assert msgs[-2]["role"] == "system" and msgs[-1] == {"role": "user", "content": "Next question."}
                assert written == [("talk", "user", "What is due?"),
                                   ("talk", "assistant", "one two —"),
                                   ("talk", "user", "Next question."),
                                   ("talk", "assistant", "The next answer.")]
                assert previous.remembered.is_set() and call.reply.prev is None
            finally:
                release.set()
    asyncio.run(run())


def test_late_playhead_ack_targets_cut_reply_and_controls_heard_history(live_env):
    rounds = [SSE([audio_delta("one two three four", b"\x01\x00" * 24000)], 0, pause=True),
              SSE([{"content": "Next answer."}], 0)]

    async def run():
        async with live_env(rounds) as (live, call, ws, requests, written):
            await feed(call, ws, turn_end())
            await until(lambda: ws.events("assistant"))
            previous = call.reply
            previous.first_audio_at = live.time.time() - 1
            await call.on_control({"type": "played", "id": previous.id, "ms": 500})
            await call.on_control({"type": "say", "text": "Next question."})
            await until(lambda: call.reply is not previous and previous.cut)
            assert not previous.remembered.is_set()
            assert live.agent._HISTORY[call.sender] == []
            assert call.heard_ms(previous) == 750
            previous.cut_at = previous.first_audio_at + 0.1
            assert call.heard_ms(previous) == 500

            await call.on_control({"type": "played", "id": previous.id, "ms": 900, "done": True})
            assert previous.ack.is_set() and previous.played_ms == 900
            assert call.reply.played_ms == 0 and call.heard_ms(previous) == 900
            await until(lambda: len(written) == 4 and not call.background)
            assert live.agent._HISTORY[call.sender][1] == {"role": "assistant", "content": "one two three —"}
            assert not [e for e in ws.events("reply_done") if e["id"] == previous.id]
            final = [e for e in ws.events("assistant") if e["id"] == previous.id and e["final"]]
            assert [e["text"] for e in final] == ["one two three —"]
    asyncio.run(run())


def test_barge_in_preserves_inflight_write_and_proposal_bookkeeping(live_env, monkeypatch):
    import threading

    async def run():
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        completed = []

        def dispatch(name, args, **kwargs):
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(1)
            completed.append(name)
            return {"plan_id": "surviving-write"}

        async with live_env([SSE([tool_delta("propose_commit")], 0)]) as env:
            live, call, ws, requests, written = env
            monkeypatch.setattr(live.agent, "_run_tool", dispatch)
            try:
                await feed(call, ws, turn_end())
                await asyncio.wait_for(entered.wait(), 0.3)
                previous = call.reply
                await call.on_control({"type": "interrupt"})
                await asyncio.gather(previous.task, return_exceptions=True)
                await call.on_control({"type": "played", "id": previous.id, "ms": 0, "done": True})
                assert previous.task.cancelled() and not completed
                live.talk._TURN[call.sender] += 10
                release.set()
                await until(lambda: "surviving-write" in live.talk._PROPOSED.get(call.sender, {}))
                assert completed == ["propose_commit"]
                assert live.talk._PROPOSED[call.sender]["surviving-write"] == previous.gturn
                assert [e["status"] for e in ws.events("tool")] == ["start"]
                assert len([p for p in requests if p.get("stream")]) == 1
            finally:
                release.set()
    asyncio.run(run())


def test_close_drains_finisher_spawned_by_reply_cancellation(live_env, monkeypatch):
    import threading

    async def run():
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        stream = SSE([], 0, pause=True)
        async with live_env([stream]) as (live, call, ws, requests, written):
            original_append = live.transcript.append

            def append(*args):
                assert not call.http.is_closed
                loop.call_soon_threadsafe(entered.set)
                assert release.wait(1)
                original_append(*args)

            monkeypatch.setattr(live.transcript, "append", append)
            ws.control(type="say", text="Remember this turn.")
            await until(lambda: requests)
            previous = call.reply
            closing = asyncio.create_task(call.close())
            try:
                await asyncio.wait_for(entered.wait(), 0.3)
                assert previous.task.cancelled() and stream.cancelled
                assert not closing.done() and not call.http.is_closed
                release.set()
                await asyncio.wait_for(closing, 0.3)
                assert previous.remembered.is_set() and not call.background
                assert call.http.is_closed
                assert written == [("talk", "user", "Remember this turn.")]
            finally:
                release.set()
                await closing
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["greet", "text"])
def test_audio_turn_cancels_pending_greeting_or_typed_reply(live_env, kind):
    async def run():
        entered, cancelled = asyncio.Event(), asyncio.Event()
        count = 0

        async def handler(payload):
            nonlocal count
            count += 1
            if count == 1:
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return httpx.Response(200, stream=SSE([{"content": "The spoken answer."}], 0))

        async with live_env(handler=handler, greet=kind == "greet") as env:
            live, call, ws, requests, written = env
            if kind == "text":
                ws.control(type="say", text="The typed question.")
            await asyncio.wait_for(entered.wait(), 0.3)
            previous = call.reply
            try:
                await feed(call, ws, turn_end())
                await until(lambda: call.reply is not previous and call.reply.remembered.is_set()
                            and not call.background)
                assert previous.task.cancelled() and cancelled.is_set()
                assert previous.cut and call.reply.kind == "audio"
                assert ws.events("assistant")[-1] == {
                    "type": "assistant", "id": call.reply.id, "text": "The spoken answer.", "final": True}
                assert not ws.events("flush")
            finally:
                previous.task.cancel()
                await asyncio.gather(previous.task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("engine", ["openrouter", "openai", "xai"])
def test_idle_call_enforces_maximum_duration(live_env, relay_env, monkeypatch, engine):
    from voice import live

    monkeypatch.setattr(live, "MAX_CALL_S", 0.05)

    async def run():
        if engine == "openrouter":
            async with live_env() as (live, call, ws, requests, written):
                await asyncio.wait_for(until(lambda: ws.client_state == WebSocketState.DISCONNECTED), 0.3)
                assert call.http.is_closed and not call.background
                assert requests == [] and written == []
        else:
            async with relay_env(engine) as (live, relay, ws, task):
                await asyncio.wait_for(asyncio.shield(task), 0.3)
                assert relay.closed
    asyncio.run(run())


def test_text_only_reply_sends_final_caption_without_reply_done(live_env):
    async def run():
        async with live_env([SSE([{"content": "Text "}, {"content": "only."}], 0)]) as env:
            live, call, ws, requests, written = env
            ws.control(type="say", text="Hello.")
            await remembered(live, call)
            assert ws.events("assistant") == [
                {"type": "assistant", "id": 1, "text": "Text ", "final": False},
                {"type": "assistant", "id": 1, "text": "only.", "final": False},
                {"type": "assistant", "id": 1, "text": "Text only.", "final": True}]
            assert not ws.events("reply") and not ws.events("reply_done")
            assert not any(isinstance(e, bytes) for e in ws.sent)
            assert written == [("talk", "user", "Hello."), ("talk", "assistant", "Text only.")]
    asyncio.run(run())


def test_cost_includes_discarded_stream_late_stt_and_rejected_filler(live_env):
    class ChargedStream(SSE):
        async def __aiter__(self):
            try:
                yield b'data: {"choices": [], "usage": {"cost": 0.0007}}\n\n'
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def run():
        stt_gate = asyncio.Event()
        stream = ChargedStream([], 0)
        rounds = [stream, SSE([audio_delta("The wrong filler words.")], 0.0004)]
        async with live_env(rounds, stt_gate=stt_gate) as env:
            live, call, ws, requests, written = env
            try:
                await feed(call, ws, turn_end())
                await until(lambda: len(requests) == 2 and not call.reply.started)
                await until(lambda: call.reply.cost == pytest.approx(0.0007))
                assert call.cost == pytest.approx(0.0007)
                previous = call.reply
                await feed(call, ws, {"type": "speech_start"})
                await asyncio.wait_for(previous.remembered.wait(), 0.3)
                assert previous.discard and stream.cancelled
                assert call.replies == 0 and not ws.events("metrics")
                stt_gate.set()
                await asyncio.wait_for(previous.stt, 0.3)
                assert call.cost == pytest.approx(0.0008)
                assert previous.cost == pytest.approx(0.0008)
                assert not ws.events("user") and written == []
                assert await call.read_aloud("One sec.") == b""
                assert call.cost == pytest.approx(0.0012)
                assert previous.cost == pytest.approx(0.0008)
            finally:
                stt_gate.set()
    asyncio.run(run())


def test_call_gets_todo_count_and_soonest_not_every_record():
    from voice import live
    todos = [{"slug": f"t{i}", "title": f"T{i}", "due": None if i % 2 else f"2099-11-{i + 1:02d}",
              "priority": "P2", "body": "long " * 100} for i in range(20)]
    digest = live._todo_digest(todos)
    assert digest["open_in_all"] == 20 and digest["more_after_these"] == 17
    assert digest["due_today_or_overdue"] == 0
    assert [t["slug"] for t in digest["say_these"]] == ["t0", "t2", "t4"]
    assert "body" not in digest["say_these"][0]
    assert [t["slug"] for t in live._todo_digest(todos, 3)["say_these"]] == ["t6", "t8", "t10"]
    assert live._todo_digest(todos, 18) == {**digest, "say_these": live._todo_digest(todos, 18)["say_these"],
                                            "more_after_these": 0}


def test_asking_again_next_turn_pages_on_and_a_later_ask_starts_over(live_env, monkeypatch):
    todos = [{"slug": f"t{i}", "title": f"T{i}", "due": f"2099-11-{i + 1:02d}"} for i in range(8)]
    rounds = []
    for cid in ("a", "b", "c"):
        rounds += [SSE([tool_delta("list_todos", cid=cid)], 0.0001), SSE([audio_delta("Right.")], 0.0001)]
    rounds.insert(4, SSE([audio_delta("Fine.")], 0.0001))

    async def run():
        async with live_env(rounds) as (live, call, ws, requests, written):
            monkeypatch.setattr(live.agent, "_run_tool", lambda *a, **kw: list(todos))
            for _ in range(4):
                n = len(live.agent._HISTORY["talk"])
                if call.reply:
                    ws.control(type="played", id=call.reply.id, ms=20, done=True)
                    await until(lambda: call.reply.playback_done)
                await feed(call, ws, turn_end())
                await until(lambda: len(live.agent._HISTORY["talk"]) >= n + 2 and not call.background)
            pages = {m["tool_call_id"]: m["content"] for p in requests for m in p.get("messages", [])
                     if m.get("role") == "tool"}
            slugs = [[t["slug"] for t in json.loads(pages[c])["say_these"]] for c in ("a", "b", "c")]
            assert slugs == [["t0", "t1", "t2"], ["t3", "t4", "t5"], ["t0", "t1", "t2"]]
    asyncio.run(run())


def test_the_same_list_asked_again_pages_on_and_a_different_one_starts_over(live_env, monkeypatch):
    todos = [{"slug": f"t{i}", "title": f"T{i}", "due": f"2099-11-{i + 1:02d}"} for i in range(8)]
    asks = {"a": "{}", "b": '{"status": "open"}', "c": '{"priority": "P1"}'}
    rounds = []
    for cid, args in asks.items():
        rounds += [SSE([tool_delta("list_todos", args, cid=cid)], 0.0001), SSE([audio_delta("Right.")], 0.0001)]

    async def run():
        async with live_env(rounds) as (live, call, ws, requests, written):
            monkeypatch.setattr(live.agent, "_run_tool", lambda *a, **kw: list(todos))
            for _ in asks:
                n = len(live.agent._HISTORY["talk"])
                if call.reply:
                    ws.control(type="played", id=call.reply.id, ms=20, done=True)
                    await until(lambda: call.reply.playback_done)
                await feed(call, ws, turn_end())
                await until(lambda: len(live.agent._HISTORY["talk"]) >= n + 2 and not call.background)
            pages = {m["tool_call_id"]: m["content"] for p in requests for m in p.get("messages", [])
                     if m.get("role") == "tool"}
            slugs = [[t["slug"] for t in json.loads(pages[c])["say_these"]] for c in asks]
            assert slugs == [["t0", "t1", "t2"], ["t3", "t4", "t5"], ["t0", "t1", "t2"]]
    asyncio.run(run())


def test_hang_up_waits_for_the_goodbye_to_play_when_its_stream_stalled(live_env):
    async def run():
        async with live_env() as (live, call, ws, requests, written):
            r = live.Reply(9, 1, "audio")
            r.started, r.first_audio_at, r.audio_ms = True, time.time() - 5, 1000
            call.reply = r
            hang = asyncio.create_task(call.hang_up(r))
            await asyncio.sleep(0.3)
            assert not hang.done() and not ws.events("end")
            r.playback_done = True
            r.ended.set()
            assert await hang
            assert ws.events("end") == [{"type": "end", "reason": "goodbye"}]
    asyncio.run(run())


def test_reminder_with_the_time_follows_old_history(live_env, monkeypatch):
    rounds = [SSE([audio_delta("Nine to midnight.")], 0.0002)]

    async def run():
        async with live_env(rounds) as (live, call, ws, requests, written):
            live.agent._HISTORY[call.sender][:] = [{"role": "user", "content": "Time?"},
                                                   {"role": "assistant", "content": "Twenty to midnight."}]
            await feed(call, ws, turn_end())
            await remembered(live, call)
            msgs = [p for p in requests if p.get("stream")][0]["messages"]
            assert [m["role"] for m in msgs] == ["system", "user", "assistant", "system", "user"]
            assert "out of date" in msgs[3]["content"]
    asyncio.run(run())


def test_bench_calls_start_without_old_history(live_env):
    rounds = [SSE([audio_delta("Hi.")], 0.0002)]

    async def run():
        from voice import agent
        agent._HISTORY["bench"] = [{"role": "assistant", "content": "Twenty to midnight."}]
        async with live_env(rounds, sender="bench") as (live, call, ws, requests, written):
            await feed(call, ws, turn_end())
            await remembered(live, call)
            msgs = [p for p in requests if p.get("stream")][0]["messages"]
            assert "Twenty to midnight." not in json.dumps(msgs)
    asyncio.run(run())


@pytest.mark.parametrize("due,said", [
    ("2026-10-10", "overdue since the 10th of October"),
    ("2026-10-22", "overdue since Thursday"),
    ("2026-10-23", "overdue since yesterday"),
    ("2026-10-24", "due today"),
    ("2026-10-25", "due tomorrow"),
    ("2026-10-30", "due on Friday"),
    ("2026-11-01", "due on the 1st of November"),
    ("2027-01-12", "due on the 12th of January 2027"),
    (None, None), ("soon", None)])
def test_due_dates_are_said_the_british_way(due, said):
    import datetime
    from voice import live
    assert live.spoken_due(due, datetime.date(2026, 10, 24)) == said


def test_ready_greeting_plays_without_a_model_call(live_env, monkeypatch):
    async def run():
        from voice import live
        monkeypatch.setattr(live, "GREETINGS", {b: ("Hey, up late.", "Evening.") for b in
                                                ("morning", "afternoon", "evening", "late")})
        cached = {}
        for line in ("Hey, up late.", "Evening."):
            key = hashlib.sha1(line.encode()).hexdigest()[:12]
            path = os.path.join(live.FILLER_DIR, "marin", f"line-{key}.pcm")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            cached[line] = line.encode().ljust(480, b"\x00")
            open(path, "wb").write(cached[line])
        async with live_env(greet=True) as (live, call, ws, requests, written):
            await until(lambda: ws.events("metrics"))
            line = ws.events("assistant")[-1]["text"]
            assert [e for e in ws.sent if isinstance(e, bytes)] == [cached[line]]
            assert ws.events("reply_done") == [{"type": "reply_done", "id": 1}]
            assert not [p for p in requests if p.get("stream")]
            assert live.agent._HISTORY["talk"] == []
            # The next call doesn't open with the same line, even when it's the only one cached.
            other, _ = call.ready_greeting()
            assert other != line
            os.remove(call.line_path(line))
            assert call.ready_greeting() == ("", b"")
    asyncio.run(run())


def test_missing_greetings_are_read_aloud_for_next_time(live_env, monkeypatch):
    async def run():
        from voice import live
        monkeypatch.setattr(live, "GREETINGS", {b: ("Evening.",) for b in
                                                ("morning", "afternoon", "evening", "late")})

        greeted = asyncio.Event()

        async def handler(payload):
            if payload["messages"][0]["content"] == live.READ_PROMPT:
                await greeted.wait()
                stream = SSE([audio_delta("Evening.", b"\x05\x00" * 240)], 0.0001)
            else:
                stream = SSE([audio_delta("Hello there.")], 0.0007)
            return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

        async with live_env(greet=True, handler=handler) as (live, call, ws, requests, written):
            await until(lambda: ws.events("metrics"))
            assert ws.events("assistant")[-1]["text"] == "Hello there."
            greeted.set()
            await until(lambda: os.path.exists(call.line_path("Evening.")))
            assert call.ready_greeting() == ("Evening.", b"\x05\x00" * 240)
    asyncio.run(run())


def test_goodbye_hangs_up_once_it_has_played(live_env):
    rounds = [SSE([audio_delta("Night, speak soon."), tool_delta("end_call")], 0.0004)]

    async def run():
        async with live_env(rounds, stt="Cheers, that's all. Bye.") as (live, call, ws, requests, written):
            await feed(call, ws, turn_end())
            await until(lambda: ws.events("reply_done"))
            assert not ws.events("end")
            ws.control(type="played", id=1, ms=20, done=True)
            await until(lambda: ws.events("end"))
            assert ws.events("state")[-1]["state"] == "speaking"
            assert ws.client_state == live.WebSocketState.DISCONNECTED
            assert ws.events("end") == [{"type": "end", "reason": "goodbye"}]
            assert [e for e in ws.sent if isinstance(e, bytes)] == [b"\x01\x00" * 480]
            assert len([p for p in requests if p.get("stream")]) == 1
            assert requests[-1]["tools"][-1]["function"]["name"] == "end_call"
            await remembered(live, call)
            assert live.agent._HISTORY["talk"][-1] == {"role": "assistant", "content": "Night, speak soon."}
    asyncio.run(run())


def test_goodbye_tool_without_words_gets_said_then_hangs_up(live_env):
    rounds = [SSE([tool_delta("end_call")], 0.0002), SSE([audio_delta("Bye.")], 0.0003)]

    async def run():
        async with live_env(rounds, stt="That's all, bye.") as (live, call, ws, requests, written):
            await feed(call, ws, turn_end())
            await until(lambda: ws.events("reply_done"))
            ws.control(type="played", id=1, ms=20, done=True)
            await until(lambda: ws.events("end"))
            # No "one sec" before a goodbye.
            assert [e for e in ws.sent if isinstance(e, bytes)] == [b"\x01\x00" * 480]
            assert not ws.events("tool")
            last = [p for p in requests if p.get("stream")][-1]
            assert last["messages"][-1]["role"] == "tool"
    asyncio.run(run())


def test_talking_over_the_goodbye_keeps_the_call(live_env):
    rounds = [SSE([audio_delta("Bye then.", b"\x01\x00" * 24000), tool_delta("end_call")], 0.0004),
              SSE([audio_delta("Go on.")], 0.0003)]

    async def run():
        async with live_env(rounds, stt="Bye. Oh wait.") as (live, call, ws, requests, written):
            await feed(call, ws, turn_end())
            await until(lambda: ws.events("reply_done"))
            await feed(call, ws, {"type": "speech_confirmed"})
            await until(lambda: ws.events("flush"))
            await asyncio.sleep(0.05)
            assert not ws.events("end")
            assert ws.client_state == live.WebSocketState.CONNECTED
    asyncio.run(run())


def test_stop_is_not_a_goodbye_so_end_call_keeps_the_call(live_env):
    rounds = [SSE([tool_delta("end_call")], 0.0002), SSE([audio_delta("Sure.")], 0.0003)]

    async def run():
        async with live_env(rounds, stt="Okay, stop. That's enough.") as (live, call, ws, requests, written):
            await feed(call, ws, turn_end())
            await until(lambda: ws.events("reply_done"))
            ws.control(type="played", id=1, ms=20, done=True)
            await remembered(live, call)
            assert not ws.events("end") and ws.client_state == live.WebSocketState.CONNECTED
            assert ws.events("assistant")[-1]["text"] == "Sure."
            last = [p for p in requests if p.get("stream")][-1]
            assert "not a goodbye" in last["messages"][-1]["content"]
    asyncio.run(run())


def test_refused_hang_up_plays_a_ready_ack_without_a_second_request(live_env, monkeypatch):
    rounds = [SSE([tool_delta("end_call")], 0.0002)]

    async def run():
        from voice import live
        monkeypatch.setattr(live, "ACKS", ("Okay.",))
        key = hashlib.sha1(b"Okay.").hexdigest()[:12]
        open(os.path.join(live.FILLER_DIR, "marin", f"line-{key}.pcm"), "wb").write(b"\x05\x00" * 240)
        async with live_env(rounds, stt="Okay, stop. That's enough.") as (live, call, ws, requests, written):
            await feed(call, ws, turn_end())
            await until(lambda: ws.events("reply_done"))
            ws.control(type="played", id=1, ms=10, done=True)
            await remembered(live, call)
            assert not ws.events("end") and ws.client_state == live.WebSocketState.CONNECTED
            assert [e for e in ws.sent if isinstance(e, bytes)] == [b"\x05\x00" * 240]
            assert len([p for p in requests if p.get("stream")]) == 1
            assert ws.events("assistant")[-1]["text"] == "Okay."
    asyncio.run(run())


def test_goodbye_tool_without_words_plays_a_ready_goodbye(live_env, monkeypatch):
    rounds = [SSE([tool_delta("end_call")], 0.0002)]

    async def run():
        from voice import live
        monkeypatch.setattr(live, "GOODBYES", ("Speak soon.",))
        key = hashlib.sha1(b"Speak soon.").hexdigest()[:12]
        open(os.path.join(live.FILLER_DIR, "marin", f"line-{key}.pcm"), "wb").write(b"\x07\x00" * 240)
        async with live_env(rounds, stt="That's all, bye.") as (live, call, ws, requests, written):
            await feed(call, ws, turn_end())
            await until(lambda: ws.events("reply_done"))
            ws.control(type="played", id=1, ms=10, done=True)
            await until(lambda: ws.events("end"))
            assert [e for e in ws.sent if isinstance(e, bytes)] == [b"\x07\x00" * 240]
            assert len([p for p in requests if p.get("stream")]) == 1
            assert ws.events("assistant")[-1]["text"] == "Speak soon."
    asyncio.run(run())
