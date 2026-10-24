"""Live-call bench, with optional residual speaker echo. --selftest is cost-free."""
import argparse
import asyncio
import contextlib
from collections import deque
import hashlib
import io
import json
import math
from pathlib import Path
import random
import re
import statistics
import struct
import sys
import time
import wave
from difflib import SequenceMatcher

import httpx
from websockets.asyncio.client import connect

RATE = 24000
FRAME_MS = 20
FRAME_BYTES = RATE * 2 * FRAME_MS // 1000
COST_BUDGET = 0.05
QUESTIONS = (
    "How's it going?",
    "What's on my list today?",
    "What time is it?",
    "Remind me what you can do, briefly.",
    "What's a good way to take a short break?",
)
COUNT = "Can you count slowly from one to thirty?"
STOP = "Okay stop, that's enough."


def wav_pcm(data):
    with wave.open(io.BytesIO(data), "rb") as wav:
        if (wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getcomptype()) != (RATE, 1, 2, "NONE"):
            raise ValueError("Kokoro must return a 24 kHz mono PCM16 WAV")
        pcm = wav.readframes(wav.getnframes())
    if not pcm:
        raise ValueError("Kokoro returned empty audio")
    return pcm


async def render(text, cache, http):
    key = hashlib.sha256(("bm_george\n" + text).encode()).hexdigest()
    path = Path(cache) / (key + ".wav")
    if path.exists():
        return wav_pcm(path.read_bytes())
    response = await http.post("http://127.0.0.1:8078/tts", json={"text": text, "voice": "bm_george"})
    response.raise_for_status()
    pcm = wav_pcm(response.content)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(response.content)
    temporary.replace(path)
    return pcm


def caption_matches(expected, actual):
    def clean(text):
        return " ".join(re.findall(r"\w+", text.lower()))
    return SequenceMatcher(None, clean(expected), clean(actual)).ratio() >= 0.7


def acknowledges_stop(text):
    return bool(re.search(r"\b(stop\w*|okay|ok|sure|enough|no problem|all right|alright|got it|"
                          r"of course|understood|done|sorry|won.t|will not)\b", text, re.I))


def percentile(values, fraction):
    values = sorted(values)
    index = (len(values) - 1) * fraction
    lower = math.floor(index)
    return values[lower] + (values[math.ceil(index)] - values[lower]) * (index - lower)


class Echo:
    """Delayed speaker leakage, with a 3.2 kHz low-pass and quiet room noise."""
    def __init__(self, db):
        self.gain = 10 ** (db / 20)
        self.delay = 0.06
        self.alpha = 1 - math.exp(-2 * math.pi * 3200 / RATE)
        self.filtered = 0.0
        self.pending = deque()
        self.rng = random.Random(19)

    def play(self, pcm, at):
        samples = []
        for value in struct.unpack(f"<{len(pcm) // 2}h", pcm):
            self.filtered += self.alpha * (value - self.filtered)
            samples.append(self.filtered * self.gain)
        self.pending.append((at + self.delay, samples))

    def mix(self, pcm, at):
        samples = [v + self.rng.randint(-12, 12) for v in struct.unpack(f"<{len(pcm) // 2}h", pcm)]
        while self.pending:
            start, echo = self.pending[0]
            offset = round((start - at) * RATE)
            if offset >= len(samples):
                break
            begin, end = max(0, -offset), min(len(echo), len(samples) - offset)
            for i in range(begin, end):
                samples[offset + i] += echo[i]
            self.pending.popleft()
            if end < len(echo):
                self.pending.appendleft((start + end / RATE, echo[end:]))
                break
        return struct.pack(f"<{len(samples)}h", *(max(-32768, min(32767, round(v))) for v in samples))


class Bench:
    def __init__(self, ws, echo_db=None):
        self.ws = ws
        self.changed = asyncio.Event()
        self.mic = asyncio.Queue()
        self.rows = []
        self.by_id = {}
        self.pending = None
        self.active = None
        self.ready_at = None
        self.ready = None
        self.interruption = None
        self.flush_ms = None
        self.expected_flush = None
        self.cut_cost_known = False
        self.failure = None
        self.tasks = []
        self.tape = None          # (time, pcm) as sent and as played, for --record
        self.echo_db = echo_db
        self.echo = Echo(echo_db) if echo_db is not None else None
        self.sending_question = False
        self.self_interruptions = []
        self.captions = {}

    async def control(self, **event):
        await self.ws.send(json.dumps(event))

    async def wait(self, predicate, timeout=45):
        async with asyncio.timeout(timeout):
            while True:
                self.changed.clear()
                if self.failure:
                    raise self.failure
                if predicate():
                    return
                await self.changed.wait()

    def row(self, label, text=""):
        row = {"label": label, "script": text, "id": None, "onset_at": None,
               "eos_at": None, "first_audio_at": None, "received_bytes": 0,
               "buffer_bytes": 0, "played_bytes": 0, "reply_done": False,
               "playback_done": False, "flushed": False, "metrics": None, "metrics_events": 0,
               "reply_text": "", "user_text": "", "audio": bytearray()}
        self.rows.append(row)
        self.pending = row
        return row

    async def receive(self):
        try:
            async for raw in self.ws:
                now = time.perf_counter()
                if isinstance(raw, bytes):
                    if self.active is None or self.active["flushed"]:
                        raise ValueError("audio received without an active reply")
                    row = self.active
                    if row["first_audio_at"] is None:
                        row["first_audio_at"] = now
                    row["received_bytes"] += len(raw)
                    row["buffer_bytes"] += len(raw)
                    if self.tape is not None or self.echo is not None:
                        row["audio"] += raw
                else:
                    ev = json.loads(raw)
                    kind = ev.get("type")
                    if kind == "ready":
                        if ev.get("rate") != RATE:
                            raise ValueError("server audio rate must be 24000")
                        self.ready, self.ready_at = ev, now
                    elif kind == "reply":
                        row = self.bind(ev["id"])
                        if row is None:
                            raise ValueError("unexpected reply")
                        if self.active is not None and not self.active["playback_done"]:
                            self.interrupted(self.active, now, "replaced")
                            self.active["buffer_bytes"] = 0
                            self.active["playback_done"] = True
                        self.active = row
                        if row is self.pending:
                            self.pending = None
                    elif kind == "user":
                        # Captions carry a turn number rather than a reply id.
                        index = ev.get("turn", 0)
                        if self.echo is not None:
                            # This audio-only call greets at id 1; echo adds turns
                            # that have no corresponding scripted question.
                            self.captions[index + 1] = ev.get("text", "")
                            if index + 1 in self.by_id:
                                self.by_id[index + 1]["user_text"] = ev.get("text", "")
                        else:
                            spoken = [r for r in self.rows if r["script"]]
                            if 0 < index <= len(spoken):
                                spoken[index - 1]["user_text"] = ev.get("text", "")
                    elif kind in ("reply_done", "assistant", "metrics", "flush"):
                        row = self.by_id.get(ev.get("id"))
                        if row is None and (kind == "assistant" or self.echo is not None):
                            row = self.bind(ev.get("id"))   # words can come before the audio
                        if row is None:
                            raise ValueError(f"{kind} received for an unknown reply")
                        if kind == "reply_done":
                            row["reply_done"] = True
                        elif kind == "assistant":
                            if ev.get("final"):
                                row["reply_text"] = ev.get("text", "")
                            else:
                                row["reply_text"] += ev.get("text", "")
                        elif kind == "metrics":
                            if not isinstance(ev.get("cost"), (int, float)) or ev["cost"] < 0:
                                raise ValueError("invalid server cost metric")
                            row["metrics"] = ev
                            row["metrics_events"] += 1
                            if self.total_cost() >= COST_BUDGET:
                                raise ValueError("call reached the $0.05 budget")
                        else:
                            self.interrupted(row, now, "flush")
                            row["buffer_bytes"] = 0
                            row["flushed"] = row["playback_done"] = True
                            await self.played(row, done=True)
                            if ev["id"] == self.expected_flush and self.interruption["onset_at"] is not None:
                                self.flush_ms = (now - self.interruption["onset_at"]) * 1000
                    elif kind == "error":
                        raise RuntimeError(ev.get("message", "server error"))
                self.changed.set()
            raise ConnectionError("server closed before the bench finished")
        except Exception as error:
            self.failure = error
            self.changed.set()

    def bind(self, rid):
        """The row for reply `rid`, giving the waiting row that id on first sight."""
        row = self.by_id.get(rid)
        if row is None and self.pending is not None and self.pending["id"] is None:
            row = self.pending
            row["id"] = rid
            self.by_id[rid] = row
        if row is None and self.echo is not None:
            pending = self.pending
            row = self.row(f"echo-{rid}")
            self.pending = pending
            row["id"] = rid
            self.by_id[rid] = row
        if row is not None and rid in self.captions:
            row["user_text"] = self.captions[rid]
        return row

    def interrupted(self, row, now, reason):
        intentional = (row["id"] == self.expected_flush and self.interruption is not None
                       and self.interruption["onset_at"] is not None)
        if not row["playback_done"] and not self.sending_question and not intentional:
            first = row["first_audio_at"]
            self.self_interruptions.append({"id": row["id"], "label": row["label"],
                                            "played_ms": round(row["played_bytes"] / 48, 1),
                                            "elapsed_ms": round((now - first) * 1000, 1) if first else None,
                                            "reason": reason})

    def total_cost(self):
        return sum(r["metrics"]["cost"] for r in self.rows if r["metrics"] is not None)

    async def send_mic(self):
        rng = random.Random(7)
        noise = struct.pack("<480h", *(rng.randint(-56, 56) for _ in range(480)))
        current = None
        deadline = time.perf_counter()
        while True:
            if current is None:
                try:
                    current = self.mic.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            if current is None:
                frame = noise
            else:
                row, pcm, offset = current
                if row["onset_at"] is None:
                    row["onset_at"] = time.perf_counter()
                frame = pcm[offset:offset + FRAME_BYTES]
                frame = frame.ljust(FRAME_BYTES, b"\x00")
                if self.tape is not None:
                    self.tape.append((time.perf_counter(), frame))
            self.sending_question = current is not None
            if self.echo is not None:
                frame = self.echo.mix(frame, time.perf_counter())
            await self.ws.send(frame)
            if current is not None:
                offset += FRAME_BYTES
                if offset >= len(pcm):
                    row["eos_at"] = time.perf_counter()
                    current = None
                    self.sending_question = False
                    self.changed.set()
                else:
                    current = row, pcm, offset
            deadline += FRAME_MS / 1000
            await asyncio.sleep(max(0, deadline - time.perf_counter()))

    async def played(self, row, done=False):
        await self.control(type="played", id=row["id"], ms=round(row["played_bytes"] / 48), done=done)

    async def playback(self):
        last = time.perf_counter()
        acknowledged = last
        while True:
            await asyncio.sleep(FRAME_MS / 1000)
            now = time.perf_counter()
            row = self.active
            if row is not None and not row["playback_done"]:
                take = min(row["buffer_bytes"], int((now - last) * RATE) * 2)
                if (self.tape is not None or self.echo is not None) and take:
                    start = row["played_bytes"]
                    pcm = bytes(row["audio"][start:start + take])
                    if self.tape is not None:
                        self.tape.append((last, pcm))
                    if self.echo is not None:
                        self.echo.play(pcm, last)
                row["buffer_bytes"] -= take
                row["played_bytes"] += take
                if row["reply_done"] and row["buffer_bytes"] == 0:
                    await self.played(row, done=True)
                    row["playback_done"] = True
                    self.changed.set()
                elif now - acknowledged >= 0.25:
                    await self.played(row)
                    acknowledged = now
            last = now

    async def supervise(self, coroutine):
        try:
            await coroutine
        except Exception as error:
            self.failure = error
            self.changed.set()

    async def speak(self, row, pcm):
        if self.total_cost() + 0.005 >= COST_BUDGET:
            raise ValueError("insufficient room in the $0.05 call budget")
        self.mic.put_nowait((row, pcm, 0))
        await self.wait(lambda: row["eos_at"] is not None)

    async def completed(self, row, gap):
        await self.wait(lambda: row["playback_done"] and row["metrics"] is not None)
        await asyncio.sleep(gap)

    async def run(self, utterances, voice, gap=0.6):
        greeting = self.row("greeting")
        # bench is the only session this client ever requests.
        await self.control(type="hello", client="bench", session="bench", greet=True, voice=voice)
        self.tasks = [asyncio.create_task(self.supervise(coro)) for coro in
                      (self.receive(), self.send_mic(), self.playback())]
        try:
            await self.wait(lambda: self.ready_at is not None)
            await self.completed(greeting, gap)
            for index, (text, pcm) in enumerate(utterances[:-2], 1):
                row = self.row(str(index), text)
                await self.speak(row, pcm)
                await self.completed(row, gap)
            count = self.row("count", COUNT)
            await self.speak(count, utterances[-2][1])
            await self.wait(lambda: count["received_bytes"] >= int(1.5 * RATE * 2) or count["playback_done"])
            already_cut = count["flushed"]
            stop = self.row("interrupt", STOP)
            self.interruption, self.expected_flush = stop, count["id"]
            await self.speak(stop, utterances[-1][1])
            if not already_cut:
                await self.wait(lambda: self.flush_ms is not None or count["flushed"])
            await self.completed(stop, gap)
            await self.wait(lambda: count["metrics"] is not None)
            # A cut reply never gets its usage chunk; the server looks its cost up
            # afterwards and sends corrected metrics.
            with contextlib.suppress(TimeoutError):
                await self.wait(lambda: count["metrics_events"] >= 2, timeout=25)
            self.cut_cost_known = count["metrics_events"] >= 2
            return self.report()
        except Exception as error:
            if self.echo is None:
                raise
            report = self.report()
            report["error"] = str(error) or type(error).__name__
            return report
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            with contextlib.suppress(Exception):
                await self.control(type="bye")

    def report(self):
        turns = []
        for row in self.rows:
            eos = None
            if row["eos_at"] is not None and row["first_audio_at"] is not None:
                eos = (row["first_audio_at"] - row["eos_at"]) * 1000
            turns.append({"label": row["label"], "id": row["id"], "script": row["script"],
                          "eos_ms": round(eos, 1) if eos is not None else None,
                          "server": row["metrics"], "reply_text": row["reply_text"],
                          "user_text": row["user_text"], "audio_ms": row["received_bytes"] / 48,
                          "played_ms": row["played_bytes"] / 48, "flushed": row["flushed"],
                          "caption_correct": caption_matches(row["script"], row["user_text"]) if row["script"] else None})
        eos = [r["eos_ms"] for r in turns if r["eos_ms"] is not None]
        if not eos and self.echo is None:
            raise ValueError("no turn latency measurements")
        greeting = self.rows[0]["first_audio_at"]
        stop = next((r for r in turns if r["label"] == "interrupt"), None)
        return {"ready": self.ready, "turns": turns,
                "greeting_ms": round((greeting - self.ready_at) * 1000, 1) if greeting and self.ready_at else None,
                "eos_p50_ms": round(statistics.median(eos), 1) if eos else None,
                "eos_p90_ms": round(percentile(eos, 0.9), 1) if eos else None,
                "flush_ms": round(self.flush_ms, 1) if self.flush_ms is not None else None,
                "echo_db": self.echo_db, "self_interruptions": self.self_interruptions,
                "total_cost": round(self.total_cost(), 6),
                "per_turn_cost": {r["label"]: r["server"]["cost"] for r in turns if r["server"] is not None},
                "captions_correct": all(r["caption_correct"] for r in turns if r["script"]),
                "interruption_answered": acknowledges_stop(stop["reply_text"]) if stop else False,
                "cut_cost_known": self.cut_cost_known}


# Which checks decide the exit code for each g-levels gate (charter "voice").
GATES = {"latency": ("eos_p50", "eos_p90"), "barge-in": ("flush", "interruption"),
         "cost": ("turn_cost", "total_cost", "cut_cost"), "echo": ("echo",), "none": ()}
LAST = Path.home() / ".cache" / "voice-live-bench" / "last.json"


def evaluate(report, args):
    costs = list(report["per_turn_cost"].values()) or [0]
    checks = {"eos_p50": report["eos_p50_ms"] is not None and report["eos_p50_ms"] <= args.max_eos_p50,
              "eos_p90": report["eos_p90_ms"] is not None and report["eos_p90_ms"] <= args.max_eos_p90,
              "flush": report["flush_ms"] is not None and report["flush_ms"] <= args.max_flush_ms,
              "turn_cost": statistics.mean(costs) <= args.max_turn_cost,
              "total_cost": report["total_cost"] < COST_BUDGET,
              "captions": report["captions_correct"],
              "interruption": report["interruption_answered"],
              "cut_cost": report.get("cut_cost_known", False)}
    if args.echo is not None or args.gate == "echo":
        checks["echo"] = args.echo is None or not report.get("self_interruptions", [])
    report["checks"] = checks
    report["mean_turn_cost"] = round(statistics.mean(costs), 6)
    deciding = GATES.get(args.gate, tuple(checks))
    report["passed"] = all(checks[k] for k in deciding)
    return 0 if report["passed"] else 1


def recent(seconds):
    """The last real run's report, if it is younger than `seconds`. Lets the four
    bench gates share one paid run."""
    try:
        if seconds and time.time() - LAST.stat().st_mtime < seconds:
            report = json.loads(LAST.read_text())
            return None if report.get("error") else report
    except (OSError, ValueError):
        pass
    return None


def remember(report):
    try:
        LAST.parent.mkdir(parents=True, exist_ok=True)
        LAST.write_text(json.dumps(report, indent=2) + "\n")
    except OSError as error:
        print(f"bench: could not save {LAST}: {error}", file=sys.stderr)


def print_report(report):
    def latency(value):
        return "-" if value is None else f"{value:.0f}"
    print("turn       client eos  server eos  ttfa     cost       caption")
    for row in report["turns"]:
        server = row["server"] or {}
        cost = f"${server['cost']:.6f}" if "cost" in server else "unknown"
        caption = "-" if not row["script"] else ("yes" if row["caption_correct"] else "no")
        print(f"{row['label']:<10} {latency(row['eos_ms']):>9} {latency(server.get('eos_ms')):>11} "
              f"{latency(server.get('ttfa_ms')):>5}  {cost:>9}  {caption}")
        print(f"  user: {row['user_text'] or '-'}")
        print(f"  reply: {row['reply_text']}")
    print(f"greeting {latency(report['greeting_ms'])} ms, eos p50 {latency(report['eos_p50_ms'])} ms, "
          f"p90 {latency(report['eos_p90_ms'])} ms, flush {latency(report['flush_ms'])} ms")
    print(f"self-interruptions: {len(report.get('self_interruptions', []))} (echo {report.get('echo_db')} dB)")
    for cut in report.get("self_interruptions", []):
        print(f"  self-interruption: reply {cut['id']} ({cut['label']}) at {cut['played_ms']:.0f} ms played, "
              f"{cut['elapsed_ms']} ms elapsed ({cut['reason']})")
    print(f"total ${report['total_cost']:.6f}, mean ${report.get('mean_turn_cost', 0):.6f} a turn, "
          f"captions {'yes' if report['captions_correct'] else 'no'}, "
          f"interruption answered {'yes' if report['interruption_answered'] else 'no'}")
    failed = [k for k, v in report["checks"].items() if not v]
    print(("PASS" if report["passed"] else "FAIL") + (": failed " + ", ".join(failed) if failed else ""))


async def run_bench(args, *, http=None, gap=0.6):
    texts = [QUESTIONS[i % len(QUESTIONS)] for i in range(args.turns)] + [COUNT, STOP]
    if http is None:
        async with httpx.AsyncClient(timeout=60) as client:
            utterances = [(text, await render(text, args.cache, client)) for text in texts]
    else:
        utterances = [(text, await render(text, args.cache, http)) for text in texts]
    async with connect(args.url, max_size=None, proxy=None, open_timeout=10) as ws:
        bench = Bench(ws, args.echo)
        if getattr(args, "record", None):
            bench.tape = []
        try:
            report = await bench.run(utterances, args.voice, gap)
        finally:
            if bench.tape:
                write_tape(bench.tape, args.record)
    return report


def write_tape(tape, path):
    """The call as a listener beside the speaker hears it: each utterance when it was
    sent and each reply as far as it played, so a cut reply stops where it was cut."""
    import numpy as np
    t0 = min(t for t, _ in tape)
    end = max(int((t - t0) * RATE) + len(pcm) // 2 for t, pcm in tape)
    mix = np.zeros(end, dtype=np.int32)
    for t, pcm in tape:
        at = int((t - t0) * RATE)
        mix[at:at + len(pcm) // 2] += np.frombuffer(pcm, dtype="<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(np.clip(mix, -32768, 32767).astype("<i2").tobytes())


async def selftest(args):
    """Real loopback WebSocket and mocked Kokoro, with no models or paid calls."""
    import tempfile
    from websockets.asyncio.server import serve

    texts = [QUESTIONS[i % len(QUESTIONS)] for i in range(args.turns)] + [COUNT, STOP]
    unique = list(dict.fromkeys(texts))
    markers = {text: 1000 + i for i, text in enumerate(unique)}
    renders, frames, played = [], [], []
    greeting_ack = asyncio.Event()

    def tts(request):
        payload = json.loads(request.content)
        assert payload["voice"] == "bm_george"
        text = payload["text"]
        renders.append(text)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(RATE)
            wav.writeframes(struct.pack("<h", markers[text]) * 1440)
        return httpx.Response(200, content=buf.getvalue())

    async def server(ws):
        hello = json.loads(await ws.recv())
        assert hello == {"type": "hello", "client": "bench", "session": "bench", "greet": True, "voice": args.voice}
        await ws.send(json.dumps({"type": "ready", "engine": "fake", "rate": RATE}))
        rid, turn, speaking, audible = 0, 0, None, None

        async def reply(text, duration, caption=None, cut=False):
            nonlocal rid, audible
            rid += 1
            audible = rid
            if caption is not None:
                await ws.send(json.dumps({"type": "user", "text": caption, "turn": turn}))
            # The real model sometimes sends a reply's first words before its audio.
            await ws.send(json.dumps({"type": "assistant", "id": rid, "text": "", "final": False}))
            await ws.send(json.dumps({"type": "reply", "id": rid}))
            await ws.send(b"\x01\x00" * int(RATE * duration))
            await ws.send(json.dumps({"type": "assistant", "id": rid, "text": text, "final": True}))
            if not cut:
                await ws.send(json.dumps({"type": "reply_done", "id": rid}))
                await ws.send(json.dumps({"type": "metrics", "id": rid, "eos_ms": 20,
                                          "ttfa_ms": 1, "cost": 0.0005}))

        await reply("Hello.", 0.28)
        async for raw in ws:
            if isinstance(raw, str):
                ev = json.loads(raw)
                if ev["type"] == "bye":
                    break
                if ev["type"] == "played":
                    played.append(ev)
                    if ev["id"] == 1 and not ev.get("done"):
                        greeting_ack.set()
                continue
            assert len(raw) == FRAME_BYTES
            frames.append(time.perf_counter())
            marker = struct.unpack_from("<h", raw)[0]
            if marker >= 900:
                if speaking is None:
                    speaking = texts[turn]
                    if speaking == STOP:
                        await ws.send(json.dumps({"type": "flush", "id": audible}))
                        await ws.send(json.dumps({"type": "metrics", "id": audible,
                                                  "eos_ms": 20, "ttfa_ms": 1, "cost": 0.0001}))
                        await ws.send(json.dumps({"type": "metrics", "id": audible,
                                                  "eos_ms": 20, "ttfa_ms": 1, "cost": 0.0005}))
            elif speaking is not None:
                text, speaking = speaking, None
                turn += 1
                await reply("One, two, three." if text == COUNT else "Okay, stopped." if text == STOP else "All good.",
                            1.8 if text == COUNT else 0.04, text, cut=text == COUNT)

    # A fresh cache makes both the HTTP render path and the cache-hit path testable.
    with tempfile.TemporaryDirectory(prefix="live-bench-selftest-") as cache:
        async with httpx.AsyncClient(transport=httpx.MockTransport(tts)) as http:
            async with serve(server, "127.0.0.1", 0) as fake:
                settings = argparse.Namespace(**vars(args))
                settings.url = f"ws://127.0.0.1:{fake.sockets[0].getsockname()[1]}/live"
                settings.cache = cache
                report = await run_bench(settings, http=http, gap=0.01)
                await render(texts[0], cache, http)
    assert len(renders) == len(unique), "renders were not cached"
    gaps = [b - a for a, b in zip(frames, frames[1:])]
    assert len(frames) > 10 and abs(statistics.median(gaps) - 0.02) < 0.012
    assert max(gaps) < 0.12, "mic stopped streaming during the call"
    assert greeting_ack.is_set(), "periodic playback acknowledgement missing"
    count = next(row for row in report["turns"] if row["label"] == "count")
    assert count["flushed"] and count["played_ms"] < count["audio_ms"]
    assert any(ev["id"] == count["id"] and ev.get("done") for ev in played)
    assert any(ev.get("done") and ev["id"] == report["turns"][-1]["id"] for ev in played)
    report["selftest"] = {"mic_frames": len(frames), "renders": len(renders), "playback_acks": len(played)}
    report["selftest"]["echo"] = await echo_selftest()
    return report


async def echo_selftest():
    """Feed a fake reply through receive/playback/mic, plus exact sample checks."""
    silence = bytes(FRAME_BYTES)
    source = struct.pack("<480h", *([10000] * 480))
    for db in (-30, -20):
        echo = Echo(db)
        echo.play(source, 1.0)
        for at in (1.0, 1.02, 1.04):
            assert max(abs(v) for v in struct.unpack("<480h", echo.mix(silence, at))) <= 12
        samples = struct.unpack("<480h", echo.mix(silence, 1.06))
        level = statistics.mean(samples[32:])
        assert abs(20 * math.log10(level / 10000) - db) < 0.1
        assert not echo.pending
        assert max(abs(v) for v in struct.unpack("<480h", echo.mix(silence, 1.08))) <= 12

    class FakeSocket:
        def __init__(self):
            self.frames = []

        async def send(self, raw):
            if isinstance(raw, bytes):
                self.frames.append((time.perf_counter(), raw))

        async def __aiter__(self):
            yield json.dumps({"type": "reply", "id": 1})
            yield source * 10
            yield json.dumps({"type": "reply_done", "id": 1})
            await asyncio.Future()

    ws = FakeSocket()
    bench = Bench(ws, -20)
    bench.row("fake")
    tasks = [asyncio.create_task(coro) for coro in (bench.receive(), bench.send_mic(), bench.playback())]
    try:
        await asyncio.sleep(0.36)
        assert bench.failure is None
        levels = [(at, statistics.mean(struct.unpack("<480h", pcm))) for at, pcm in ws.frames]
        first = next(at for at, level in levels if level > 100)
        delay_ms = (first - bench.rows[0]["first_audio_at"]) * 1000
        assert 50 <= delay_ms <= 110, delay_ms
        assert any(abs(level - 1000) < 20 for _, level in levels)
        assert bench.rows[0]["playback_done"]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return {"delay_ms": round(delay_ms, 1), "levels_db": [-30, -20]}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="ws://127.0.0.1:8077/live")
    p.add_argument("--turns", type=int, default=3)
    p.add_argument("--voice", default="marin")
    p.add_argument("--cache", default="/tmp/live-bench-cache")
    p.add_argument("--json", type=Path)
    p.add_argument("--record", type=Path, metavar="WAV", help="write the call as heard to a WAV")
    p.add_argument("--echo", type=float, metavar="DB", help="mix residual speaker echo into the mic (e.g. -25 dB)")
    p.add_argument("--max-eos-p50", type=float, default=1500)
    p.add_argument("--max-eos-p90", type=float, default=2500)
    p.add_argument("--max-flush-ms", type=float, default=800)
    p.add_argument("--max-turn-cost", type=float, default=0.004, help="mean USD a turn")
    p.add_argument("--gate", choices=("all", *GATES), default="all",
                   help="which checks decide the exit code (the g-levels voice gates)")
    p.add_argument("--reuse", type=float, default=0, metavar="SECONDS",
                   help=f"reuse the last real run from {LAST} if younger than this")
    p.add_argument("--selftest", action="store_true")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.turns < 0:
        p.error("--turns must be nonnegative")
    if args.echo is not None and (not math.isfinite(args.echo) or args.echo > 0):
        p.error("--echo must be finite and at most 0 dB")
    if any(not math.isfinite(v) or v < 0 for v in (args.max_eos_p50, args.max_eos_p90,
                                                    args.max_flush_ms, args.max_turn_cost)):
        p.error("thresholds must be finite and nonnegative")
    try:
        report = None if args.selftest else recent(args.reuse)
        if report is not None and report.get("echo_db") != args.echo:
            report = None
        if report is not None:
            print(f"bench: reusing the run from {LAST}")
        else:
            report = asyncio.run(selftest(args) if args.selftest else run_bench(args))
            if not args.selftest:
                remember(report)
        code = evaluate(report, args)
        if report.get("error"):
            report["checks"]["completed"], report["passed"] = False, False
            print(f"bench error: {report['error']}", file=sys.stderr)
            code = 2
        if args.json:
            args.json.write_text(json.dumps(report, indent=2) + "\n")
        print_report(report)
        if args.selftest:
            print("selftest: continuous mic, cached renders, playback acknowledgements and flush verified")
            print(f"selftest: echo delay {report['selftest']['echo']['delay_ms']} ms; -30/-20 dB levels verified")
        return code
    except (Exception, KeyboardInterrupt) as error:
        message = str(error) or type(error).__name__
        print(f"bench error: {message}", file=sys.stderr)
        if args.json:
            try:
                args.json.write_text(json.dumps({"error": message, "passed": False}, indent=2) + "\n")
            except OSError:
                pass
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
