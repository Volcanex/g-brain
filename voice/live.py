"""Live call: one WebSocket per call, the person's voice in, the Voice's voice out.

The contract (client protocol, engines) is in CLAUDE.md, "Live call". With the
openrouter engine this module does the turn-taking itself: the mic feeds
turn.Endpointer, each finished turn goes to the speech model AS AUDIO (so it
hears tone, not a transcript), and the reply audio streams straight back to the
client while the model is still talking. When the person talks over the reply,
the reply is cut, the client is told to drop what it buffered, and history
keeps only the part that was heard.

The realtime engines (realtime.py) listen for themselves; for them this module
only relays frames and runs the tools.

History and transcript are shared with the rest of the Voice: a call made with
session "talk" reads back on the /voice page, one made with "app" in the app chat.
"""
import asyncio
import base64
import datetime
import io
import json
import logging
import os
import random
import time
import wave
from collections import deque
from zoneinfo import ZoneInfo

import httpx
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from . import agent, config, talk, transcript
from .brief import build_brief
from .prompt import SYSTEM, date_rule

log = logging.getLogger("voice.live")

RATE = 24000
GENERATION_URL = "https://openrouter.ai/api/v1/generation"
BYTES_PER_MS = RATE * 2 // 1000
SESSIONS = ("talk", "app", "bench")
# live_bench.py calls as "bench": its own history and transcript, and only tools
# that read, so a benchmark run can never write to the owner's todos or notes.
READ_TOOLS = {"brain_search", "brain_read", "recent_activity", "list_todos"}
MAX_CALL_S = 60 * 60
MAX_TOKENS = 1200          # ~20 audio tokens a second, so roughly a minute of speech
MIN_TURN_MS = 250          # shorter "speech" is a cough or a click
RATE_REPLIES = 40          # per minute per session: a reply heard back through the
RATE_WINDOW = 60.0         # speakers can loop, and every reply costs a model call
STT_WAIT_S = 6.0
ACK_WAIT_S = 0.6           # for the client's playhead after a flush
MAX_TURN_S = 120           # one turn sent to the model holds at most this much audio
FILLER_DIR = os.path.join(os.environ.get("VOICE_LIVE_MODELS")
                          or os.path.expanduser("~/.local/share/voice-live"), "fillers")
FILLERS = ("One sec.", "Let me check.", "Hang on.")

LIVE = """
## This is a live voice call
You are on a phone call with {owner}. You hear their actual voice, so you can
pick up tone, hesitation and mood. Talk like a person on the phone: usually one
or two short sentences, the answer first, then stop and let them talk. British
English, relaxed and natural. It's fine to say "yeah", "right", "fair enough".
Everything you write is spoken, so write no bullets, dashes, numbered lines or
line breaks, and never read out URLs, file paths, slugs, ids, tags or categories.
Say times the way people do: "twenty to midnight", "just gone half eleven",
never "23:37". For a list, say how many there are and name the first two or
three in a few words each, then ask if they want the rest.
When you've answered, stop. Don't end with an offer such as "let me know if
you need anything else" or "anything else?": they'll talk when they want to.
If you need to look something up or change a todo, call the tool straight away
without announcing it: the call already plays a short "one sec" for you.
If what you heard is only noise, a cough or half a word, say a short "Sorry,
say that again?" instead of guessing.
If they cut you off, drop what you were saying and answer the new thing.
"""
GREET = ("(The call has just connected and {owner} hasn't said anything yet. Pick up "
         "the way a friend answers the phone: a few words that fit the time of day. "
         "No list, no menu of things you can do.)")
STT_PROMPT = ("Transcribe the speech verbatim, British spelling. Output only the words "
              "spoken. If there is no speech, output nothing.")
READ_PROMPT = ("You are the voice of a phone assistant recording short audio clips. The user gives you a "
               "line in quotes. Speak exactly that line, word for word, in a relaxed natural tone, and "
               "say nothing else. Never reply to it or add words.")

_RECENT: dict[str, deque] = {}


def _wav(pcm: bytes) -> str:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return base64.b64encode(buf.getvalue()).decode()


def _audio_message(pcm: bytes) -> dict:
    return {"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": _wav(pcm), "format": "wav"}}]}


def _headers() -> dict:
    h = {"Authorization": f"Bearer {config.OPENROUTER_API_KEY}", "X-Title": "brain-voice-live"}
    if config.PUBLIC_URL:
        h["HTTP-Referer"] = config.PUBLIC_URL
    return h


def _words(text: str) -> list[str]:
    return "".join(c for c in text.lower() if c.isalnum() or c.isspace()).split()


def heard(text: str, played_ms: float, audio_ms: float) -> str:
    """The part of a cut-off reply the person actually heard, by playing time."""
    if audio_ms <= 0 or played_ms >= audio_ms:
        return text
    words = text.split()
    keep = int(len(words) * max(0.0, played_ms) / audio_ms)
    return " ".join(words[:keep]) + " —" if keep else ""


def _plan_id(args) -> str | None:
    try:
        return (json.loads(args or "{}") or {}).get("plan_id")
    except (json.JSONDecodeError, AttributeError):
        return None


def _trim(history: list):
    """History is shared and sliced to agent._MAX_TURNS on use; keep it bounded too."""
    if len(history) > 4 * agent._MAX_TURNS:
        del history[:-2 * agent._MAX_TURNS]


def _rate_limited(sender: str, now: float) -> bool:
    q = _RECENT.setdefault(sender, deque())
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_REPLIES:
        return True
    q.append(now)
    return False


class Reply:
    """One reply: the turn that caused it and how much of it went out and was heard."""

    def __init__(self, rid: int, turn: int, kind: str, pcm: bytes = b"", text: str = "",
                 eos_at: float | None = None):
        self.id, self.turn, self.kind = rid, turn, kind
        self.pcm, self.user_text = pcm, text      # the person's turn: audio, or typed text
        self.eos_at = eos_at or time.time()
        self.stt: asyncio.Task | None = None
        self.task: asyncio.Task | None = None
        self.started = False                       # first audio sent
        self.first_audio_at = 0.0
        self.audio_ms = 0.0
        self.played_ms = 0.0
        self.playback_done = False
        self.cut = False
        self.discard = False                       # merged into a longer turn, never persisted
        self.filler = False
        self.text = ""
        self.tools: list[str] = []
        self.cost = 0.0
        self.ttfa_ms: int | None = None
        self.eos_ms: int | None = None
        self.metrics_sent = False
        self.gen: str | None = None                # OpenRouter generation id of the latest request
        self.charged = False
        self.gturn = 0                             # talk._TURN when it started, binds commit approval
        self.confirmed = False                     # this turn's "yes" already ran a plan
        self.prev: Reply | None = None             # remembered before this one, in order
        self.cut_at = 0.0
        self.ended = asyncio.Event()               # cut, played out, or replaced
        self.ack = asyncio.Event()                 # the client's playhead after a flush
        self.remembered = asyncio.Event()

    def generating(self) -> bool:
        return self.task is not None and not self.task.done()

    def audible(self) -> bool:
        """Is the client still playing (or about to play) this reply?"""
        if not self.started or self.cut or self.playback_done:
            return False
        return self.generating() or time.time() < self.first_audio_at + self.audio_ms / 1000 + 0.3


class Call:
    """One call on the openrouter engine."""

    def __init__(self, ws: WebSocket, sender: str, voice: str, client: str):
        self.ws, self.sender, self.voice, self.client = ws, sender, voice, client
        self.ep = None
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=10))
        self.send_lock = asyncio.Lock()
        self.turn = 0
        self.rid = 0
        self.reply: Reply | None = None
        self.recent: dict[int, Reply] = {}  # playheads can arrive after the next reply starts
        self.held: bytes = b""              # a turn whose reply had not started speaking yet
        self.held_eos: float | None = None
        self.muted = False
        self.state = ""
        self.brief = ""
        self.fillers: list[bytes] = []
        self.cost = 0.0
        self.replies = 0
        self.started = time.time()
        self.background: set[asyncio.Task] = set()

    # --- sending ---------------------------------------------------------------
    async def send(self, ev: dict):
        if self.ws.client_state != WebSocketState.CONNECTED:
            return
        async with self.send_lock:
            try:
                await self.ws.send_text(json.dumps(ev))
            except (RuntimeError, WebSocketDisconnect):
                pass

    async def send_audio(self, pcm: bytes):
        if self.ws.client_state != WebSocketState.CONNECTED:
            return
        async with self.send_lock:
            try:
                await self.ws.send_bytes(pcm)
            except (RuntimeError, WebSocketDisconnect):
                pass

    async def set_state(self, state: str):
        if state != self.state:
            self.state = state
            await self.send({"type": "state", "state": state})

    def spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self.background.add(t)
        t.add_done_callback(self.background.discard)
        return t

    def charge(self, r: Reply | None, usage: dict | None):
        cost = float((usage or {}).get("cost") or 0)
        self.cost += cost
        if r:
            r.cost += cost

    # --- the call --------------------------------------------------------------
    async def open(self):
        from . import turn                  # onnxruntime loads with the first call, not at import
        self.ep = await asyncio.to_thread(turn.Endpointer, RATE)

    async def run(self, greet: bool):
        if self.sender not in agent._HISTORY:
            agent._HISTORY[self.sender] = await asyncio.to_thread(
                transcript.load_tail, self.sender, agent._MAX_TURNS)
        self.brief = await asyncio.to_thread(build_brief)
        self.spawn(self.load_fillers())
        if greet:
            await self.start(Reply(self.next_id(), self.turn, "greet"))
        else:
            await self.set_state("listening")
        deadline = self.started + MAX_CALL_S
        while True:
            try:
                msg = await asyncio.wait_for(self.ws.receive(), deadline - time.time())
            except TimeoutError:
                break
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                await self.on_audio(msg["bytes"])
            elif msg.get("text"):
                try:
                    ev = json.loads(msg["text"])
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "bye":
                    break
                await self.on_control(ev)

    async def close(self):
        """Stop the reply, let its finisher write history, then drop whatever is left."""
        r = self.reply
        if r:
            r.ended.set()
            if r.generating():
                r.task.cancel()
        end = time.time() + STT_WAIT_S + 2
        while True:
            pending = set(self.background) | ({r.task} if r and r.generating() else set())
            if not pending or time.time() >= end:
                break
            await asyncio.wait(pending, timeout=end - time.time())
        for t in list(self.background):
            t.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)
        await self.http.aclose()
        log.info("live call sender=%s client=%s replies=%d cost=$%.4f duration_s=%d",
                 self.sender, self.client, self.replies, self.cost, time.time() - self.started)

    def next_id(self) -> int:
        self.rid += 1
        return self.rid

    async def on_control(self, ev: dict):
        kind = ev.get("type")
        r = self.reply
        if kind == "played":
            p = self.recent.get(ev.get("id"))
            if p is None:
                return
            p.played_ms = float(ev.get("ms") or 0)
            if p.cut:
                p.ack.set()
            if ev.get("done"):
                p.playback_done = True
                p.ended.set()
                if p is r and not r.generating() and not self.ep.in_speech:
                    await self.set_state("listening")
        elif kind == "interrupt":
            if r and (r.audible() or r.generating()):
                await self.cut(r)
                await self.set_state("listening")
        elif kind == "mute":
            self.muted = bool(ev.get("on"))
            self.ep.reset()
        elif kind == "say":
            text = " ".join(str(ev.get("text") or "").split())[:2000]
            if text:
                if r and (r.audible() or r.generating()):
                    await self.cut(r)
                self.held, self.held_eos = b"", None
                self.turn += 1
                await self.start(Reply(self.next_id(), self.turn, "text", text=text))

    async def on_audio(self, pcm: bytes):
        if self.muted or not pcm:
            return
        for ev in await asyncio.to_thread(self.ep.feed, pcm):
            await self.on_turn_event(ev)

    async def on_turn_event(self, ev: dict):
        r = self.reply
        kind = ev["type"]
        if kind == "speech_start":
            if r and r.generating() and not r.started and r.kind == "audio":
                # They carried on before the reply began: one longer turn, asked again.
                r.discard = True
                r.task.cancel()
                await self.set_state("hearing")
            elif not (r and r.audible()):
                await self.set_state("hearing")
        elif kind == "speech_confirmed":
            if r and r.audible():
                await self.cut(r)
                await self.set_state("hearing")
        elif kind == "turn_end":
            log.info("live turn_end sender=%s reason=%s prob=%s silence_ms=%s speech_ms=%s",
                     self.sender, ev.get("reason"), ev.get("prob") and round(ev["prob"], 2),
                     ev.get("silence_ms"), ev.get("speech_ms"))
            if r and r.audible():
                return                        # the reply heard through the speaker, or a short noise
            if ev.get("speech_ms", 0) < MIN_TURN_MS and not self.held:
                await self.set_state("listening")
                return
            pcm = self.held + ev["audio"] if (r and r.discard) else ev["audio"]
            pcm = pcm[-MAX_TURN_S * RATE * 2:]
            eos = time.time() - ev.get("silence_ms", 0) / 1000
            self.held, self.held_eos = pcm, eos
            self.turn += 1
            await self.start(Reply(self.next_id(), self.turn, "audio", pcm=pcm, eos_at=eos))

    async def start(self, r: Reply):
        # Checked before anything is spent: the transcription costs as well as the reply.
        if _rate_limited(self.sender, time.time()):
            self.held, self.held_eos = b"", None
            await self.send({"type": "error", "message": "Slow down a moment, too many turns in a minute."})
            await self.set_state("listening")
            return
        old = self.reply
        if old:
            old.ended.set()
            if old.generating():
                await self.cut(old)           # a greeting or typed reply still waiting to speak
        r.prev = old
        self.reply = r
        self.recent[r.id] = r
        for rid in list(self.recent)[:-8]:
            del self.recent[rid]
        if r.kind == "audio":
            r.stt = self.spawn(self.transcribe(r))
        talk._TURN[self.sender] = talk._TURN.get(self.sender, 0) + (r.kind != "greet")
        r.gturn = talk._TURN[self.sender]
        r.task = asyncio.create_task(self.respond(r))
        r.task.add_done_callback(lambda t: self.spawn(self.finish(r)))

    async def cut(self, r: Reply):
        if r.cut:
            return
        r.cut = True
        r.ended.set()
        if r.started:
            r.cut_at = time.time()
            await self.send({"type": "flush", "id": r.id})
        if r.generating():
            r.task.cancel()

    def heard_ms(self, r: Reply) -> float:
        """How much of a cut reply was played: the client's playhead after the flush,
        else its last report plus up to one reporting interval of wall time."""
        if r.ack.is_set():
            return r.played_ms
        wall = min(r.audio_ms, (r.cut_at - r.first_audio_at) * 1000)
        return max(r.played_ms, min(wall, r.played_ms + 250)) if r.played_ms else wall

    # --- one reply -------------------------------------------------------------
    def system_prompt(self) -> str:
        now = datetime.datetime.now(ZoneInfo("Europe/London"))
        owner = config.OWNER_NAME
        # Stable parts first, so the provider's prompt cache covers them.
        return (SYSTEM + f"You are currently running on the model: {config.LIVE_MODEL}\n"
                + "\n## Right now (your short-term memory)\n" + self.brief + talk.SPOKEN
                + LIVE.format(owner=owner) + date_rule(now.date().isoformat())
                + f"It is {now:%A} {now:%H:%M} in London right now.\n")

    def messages(self, r: Reply) -> list:
        history = agent._HISTORY[self.sender][-agent._MAX_TURNS:]
        msgs = [{"role": "system", "content": self.system_prompt()}] + history
        if r.kind == "greet":
            msgs.append({"role": "user", "content": GREET.format(owner=config.OWNER_NAME)})
        elif r.kind == "text":
            msgs.append({"role": "user", "content": r.user_text})
        else:
            msgs.append(_audio_message(r.pcm))
        return msgs

    async def respond(self, r: Reply):
        await self.set_state("thinking")
        if r.prev and not r.prev.remembered.is_set():
            # The turn before, as far as it was heard, belongs in this request.
            try:
                await asyncio.wait_for(r.prev.remembered.wait(), 1.5)
            except TimeoutError:
                pass
        messages = self.messages(r)
        for _ in range(config.MAX_TOOL_ITERATIONS):
            said, calls = await self.stream(r, messages, tools=r.kind != "greet")
            if not calls:
                return
            messages.append({"role": "assistant", "content": said or None, "tool_calls": calls})
            await self.play_filler(r)
            for call in calls:
                messages.append(await self.run_tool(r, call))
        await self.send({"type": "error", "message": "Got stuck on that one."})

    async def stream(self, r: Reply, messages: list, tools: bool):
        payload = {"model": config.LIVE_MODEL, "messages": messages, "stream": True,
                   "modalities": ["text", "audio"], "audio": {"voice": self.voice, "format": "pcm16"},
                   "max_tokens": MAX_TOKENS, "usage": {"include": True}}
        if tools:
            payload["tools"] = [t for t in agent.TOOLS if self.sender != "bench"
                                or t["function"]["name"] in READ_TOOLS]
            payload["tool_choice"] = "auto"
        t_req = time.time()
        try:
            return await self._stream(r, payload, t_req)
        except asyncio.CancelledError:
            # Cut off before the usage chunk: ask OpenRouter what it cost instead.
            if r.gen and not r.charged:
                self.spawn(self.charge_late(r, r.gen))
            raise

    async def _stream(self, r: Reply, payload: dict, t_req: float):
        said, calls = "", {}
        r.gen, r.charged = None, False
        async with self.http.stream("POST", config.OPENROUTER_URL, headers=_headers(), json=payload) as resp:
            if resp.status_code != 200:
                body = (await resp.aread())[:300]
                raise RuntimeError(f"OpenRouter {resp.status_code}: {body!r}")
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if chunk.get("error"):
                    raise RuntimeError(f"OpenRouter: {chunk['error']}")
                r.gen = r.gen or chunk.get("id")
                if chunk.get("usage"):
                    self.charge(r, chunk["usage"])
                    r.charged = True
                for ch in chunk.get("choices") or []:
                    d = ch.get("delta") or {}
                    a = d.get("audio") or {}
                    if a.get("data"):
                        if r.ttfa_ms is None:
                            r.ttfa_ms = int((time.time() - t_req) * 1000)
                        await self.speak(r, base64.b64decode(a["data"]))
                    words = a.get("transcript") or d.get("content")
                    if words:
                        said += words
                        r.text += words
                        await self.send({"type": "assistant", "id": r.id, "text": words, "final": False})
                    for tc in d.get("tool_calls") or []:
                        c = calls.setdefault(tc.get("index", 0), {
                            "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            c["id"] = tc["id"]
                        f = tc.get("function") or {}
                        c["function"]["name"] += f.get("name") or ""
                        c["function"]["arguments"] += f.get("arguments") or ""
        return said, [calls[i] for i in sorted(calls)]

    async def speak(self, r: Reply, pcm: bytes):
        if r.cut:
            return
        if not r.started:
            r.started = True
            r.first_audio_at = time.time()
            self.held, self.held_eos = b"", None
            await self.send({"type": "reply", "id": r.id})
            await self.set_state("speaking")
        r.audio_ms += len(pcm) / BYTES_PER_MS
        await self.send_audio(pcm)

    async def play_filler(self, r: Reply):
        if r.filler or not self.fillers:
            return
        r.filler = True
        await self.speak(r, random.choice(self.fillers))

    async def run_tool(self, r: Reply, call: dict) -> dict:
        name = call["function"]["name"]
        args = call["function"].get("arguments")
        r.tools.append(name)
        if self.sender == "bench" and name not in READ_TOOLS:
            return {"role": "tool", "tool_call_id": call["id"],
                    "content": json.dumps({"error": "read-only session"})}
        await self.send({"type": "tool", "name": name, "status": "start"})
        # A barge-in cancels the reply, not the write: the tool and its bookkeeping
        # finish in the background either way.
        result = await asyncio.shield(self.spawn(self.use_tool(r, name, args, call["id"])))
        await self.send({"type": "tool", "name": name, "status": "done"})
        return {"role": "tool", "tool_call_id": call["id"],
                "content": json.dumps(result, default=str)[:6000]}

    async def use_tool(self, r: Reply, name: str, args, call_id: str):
        gate = None
        if name == "execute_commit":
            gate = talk._commit_gate(self.sender, args, await self.user_text(r), turn=r.gturn)
            if gate is None and r.confirmed:
                gate = {"error": "one yes covers one plan; ask again for this one"}
            if gate is None:
                r.confirmed = True
                talk._PROPOSED.get(self.sender, {}).pop(_plan_id(args), None)
        result = gate or await asyncio.to_thread(
            agent._run_tool, name, args, session_id=call_id, turn_id=r.turn)
        if name == "propose_commit" and isinstance(result, dict) and result.get("plan_id"):
            talk._PROPOSED.setdefault(self.sender, {})[result["plan_id"]] = r.gturn
        err = result.get("error") if isinstance(result, dict) else None
        log.info("tool sender=%s name=%s args=%s -> %s", self.sender, name, str(args)[:300],
                 f"ERROR {err}" if err else "ok")
        return result

    async def finish(self, r: Reply):
        """After a reply ends, is cut or fails: close it out to the client, then
        remember the turn as far as it was heard."""
        try:
            await self.close_out(r)
        finally:
            r.remembered.set()
            r.prev = None                     # the chain must not keep a whole call's audio

    async def close_out(self, r: Reply):
        t = r.task
        if t.cancelled():
            pass
        elif t.exception() is not None:
            e = t.exception()
            log.error("live reply failed sender=%s: %s: %s", self.sender, type(e).__name__, e)
            await self.send({"type": "error", "message": "That one didn't go through. Try again?"})
        if r.discard:
            return
        self.replies += 1
        if r.started and not r.cut:
            await self.send({"type": "reply_done", "id": r.id})
        if r.started or r.text:
            await self.send({"type": "assistant", "id": r.id, "text": r.text, "final": True})
        r.eos_ms = eos_ms = int((r.first_audio_at - r.eos_at) * 1000) if r.started and r.kind != "greet" else None
        if r.stt:
            try:
                await asyncio.wait_for(asyncio.shield(r.stt), STT_WAIT_S)
            except Exception:
                pass
        await self.send_metrics(r)
        log.info("live reply sender=%s id=%d kind=%s eos_ms=%s ttfa_ms=%s audio_ms=%d cut=%s tools=%s cost=$%.5f",
                 self.sender, r.id, r.kind, eos_ms, r.ttfa_ms, r.audio_ms, r.cut,
                 ",".join(r.tools) or "-", r.cost)
        if self.reply is r and not r.cut and not self.ep.in_speech:
            if r.started:
                left = r.first_audio_at + r.audio_ms / 1000 - time.time()
                if left > 0 and not r.playback_done:
                    try:
                        await asyncio.wait_for(r.ended.wait(), left)
                    except TimeoutError:
                        pass
            if self.reply is r and not r.cut and not self.ep.in_speech:
                await self.set_state("listening")
        if r.cut and r.started and not r.ack.is_set():
            try:
                await asyncio.wait_for(r.ack.wait(), ACK_WAIT_S)
            except TimeoutError:
                pass
        await self.remember(r)

    async def send_metrics(self, r: Reply):
        r.metrics_sent = True
        await self.send({"type": "metrics", "id": r.id, "eos_ms": r.eos_ms, "ttfa_ms": r.ttfa_ms,
                         "cost": round(r.cost, 6)})

    async def charge_late(self, r: Reply, gen: str):
        """OpenRouter bills a cancelled stream for what it generated; its stats appear a
        moment later. A second metrics event carries the corrected cost."""
        for delay in (1, 2, 3):
            await asyncio.sleep(delay)
            try:
                resp = await self.http.get(GENERATION_URL, params={"id": gen}, headers=_headers())
                cost = (resp.json().get("data") or {}).get("total_cost") if resp.status_code == 200 else None
            except Exception:
                cost = None
            if cost is not None:
                self.charge(r, {"cost": cost})
                if r.metrics_sent:
                    await self.send_metrics(r)
                return
        log.warning("live cost unknown for cut reply %s", gen)

    async def remember(self, r: Reply):
        if r.kind == "greet":
            return
        if not r.cut:
            said = r.text.strip()
        elif r.started:
            said = heard(r.text.strip(), self.heard_ms(r), r.audio_ms)
        else:
            said = ""                         # cut before a word of it was played
        user = await self.user_text(r) or "(inaudible)"
        if r.prev and not r.prev.remembered.is_set():
            try:
                await asyncio.wait_for(r.prev.remembered.wait(), STT_WAIT_S + 2)
            except TimeoutError:
                pass
        history = agent._HISTORY[self.sender]
        history.append({"role": "user", "content": user})
        if said:
            history.append({"role": "assistant", "content": said})
        _trim(history)
        await asyncio.to_thread(transcript.append, self.sender, "user", user)
        if said:
            await asyncio.to_thread(transcript.append, self.sender, "assistant", said)

    # --- the person's words, beside the reply ------------------------------------
    async def user_text(self, r: Reply) -> str:
        if r.kind == "text":
            return r.user_text
        if r.stt is None:
            return ""
        try:
            return await asyncio.wait_for(asyncio.shield(r.stt), STT_WAIT_S) or ""
        except Exception:
            return ""

    async def transcribe(self, r: Reply) -> str:
        payload = {"model": config.LIVE_STT_MODEL, "temperature": 0, "max_tokens": 400,
                   "messages": [{"role": "system", "content": STT_PROMPT}, _audio_message(r.pcm)],
                   "usage": {"include": True}}
        try:
            resp = await self.http.post(config.OPENROUTER_URL, headers=_headers(), json=payload)
            j = resp.json()
            self.charge(r, j.get("usage"))
            text = " ".join(((j.get("choices") or [{}])[0].get("message") or {}).get("content", "").split())
        except Exception as e:
            log.warning("live stt failed: %s: %s", type(e).__name__, e)
            return ""
        if text and not r.discard:
            await self.send({"type": "user", "text": text, "turn": r.turn})
        return text

    # --- "one sec" in the call's own voice -----------------------------------
    async def load_fillers(self):
        d = os.path.join(FILLER_DIR, self.voice)
        out = []
        for i, line in enumerate(FILLERS):
            path = os.path.join(d, f"{i}.pcm")
            if not os.path.exists(path):
                try:
                    pcm = await self.read_aloud(line)
                except Exception as e:
                    log.warning("live filler %r failed: %s", line, e)
                    continue
                if not pcm:
                    continue
                os.makedirs(d, exist_ok=True)
                with open(path + ".tmp", "wb") as f:
                    f.write(pcm)
                os.replace(path + ".tmp", path)
            with open(path, "rb") as f:
                out.append(f.read())
        self.fillers = out

    async def read_aloud(self, line: str) -> bytes:
        payload = {"model": config.LIVE_MODEL, "stream": True, "modalities": ["text", "audio"],
                   "audio": {"voice": self.voice, "format": "pcm16"}, "max_tokens": 200,
                   "messages": [{"role": "system", "content": READ_PROMPT},
                                {"role": "user", "content": f'Line: "{line}"'}],
                   "temperature": 0.3, "usage": {"include": True}}
        pcm, words = bytearray(), ""
        async with self.http.stream("POST", config.OPENROUTER_URL, headers=_headers(), json=payload) as resp:
            async for raw in resp.aiter_lines():
                if not raw.startswith("data:") or raw[5:].strip() == "[DONE]":
                    continue
                chunk = json.loads(raw[5:])
                if chunk.get("usage"):
                    self.charge(None, chunk["usage"])
                for ch in chunk.get("choices") or []:
                    a = (ch.get("delta") or {}).get("audio") or {}
                    if a.get("data"):
                        pcm += base64.b64decode(a["data"])
                    words += a.get("transcript") or ""
        # The model sometimes answers the line instead of saying it ("Sure, take your
        # time..."), and a filler that came out as a sentence would talk over the answer.
        same = _words(words) == _words(line)
        return bytes(pcm) if same and len(pcm) < 3 * RATE * 2 else b""


# --- realtime engines ------------------------------------------------------------
async def _relay(ws: WebSocket, sender: str, voice: str | None, engine: str, greet: bool):
    from .realtime import RealtimeCall
    key = config.XAI_API_KEY if engine == "xai" else config.OPENAI_API_KEY
    model = config.LIVE_XAI_MODEL if engine == "xai" else config.LIVE_OPENAI_MODEL
    if sender not in agent._HISTORY:
        agent._HISTORY[sender] = await asyncio.to_thread(transcript.load_tail, sender, agent._MAX_TURNS)
    brief = await asyncio.to_thread(build_brief)
    now = datetime.datetime.now(ZoneInfo("Europe/London"))
    history = "\n".join(f"{m['role']}: {m['content']}" for m in agent._HISTORY[sender][-10:]
                        if isinstance(m.get("content"), str))
    instructions = (SYSTEM + f"You are currently running on the model: {model}\n"
                    + "\n## Right now (your short-term memory)\n" + brief + talk.SPOKEN
                    + LIVE.format(owner=config.OWNER_NAME) + date_rule(now.date().isoformat())
                    + f"It is {now:%A} {now:%H:%M} in London right now.\n"
                    + ("\n## The conversation so far\n" + history + "\n" if history else ""))
    lock = asyncio.Lock()
    last_user = {"text": ""}

    async def send_json(ev):
        async with lock:
            try:
                await ws.send_text(json.dumps(ev))
            except (RuntimeError, WebSocketDisconnect):
                pass

    async def send_audio(pcm):
        async with lock:
            try:
                await ws.send_bytes(pcm)
            except (RuntimeError, WebSocketDisconnect):
                pass

    def on_turn(role, text):
        if role == "user":
            last_user["text"] = text
            talk._TURN[sender] = talk._TURN.get(sender, 0) + 1
        agent._HISTORY[sender].append({"role": role, "content": text})
        _trim(agent._HISTORY[sender])
        transcript.append(sender, role, text)

    async def run_tool(name, args):
        if sender == "bench" and name not in READ_TOOLS:
            return {"error": "read-only session"}
        if name == "execute_commit":
            gate = talk._commit_gate(sender, args, last_user["text"])
            if gate:
                return gate
            last_user["text"] = ""            # one yes runs one plan
            talk._PROPOSED.get(sender, {}).pop(_plan_id(args), None)
        result = await asyncio.to_thread(agent._run_tool, name, args, turn_id=talk._TURN.get(sender, 0))
        if name == "propose_commit" and isinstance(result, dict) and result.get("plan_id"):
            talk._PROPOSED.setdefault(sender, {})[result["plan_id"]] = talk._TURN.get(sender, 0)
        return result

    rc = RealtimeCall(provider=engine, api_key=key, model=model, voice=voice, instructions=instructions,
                      tools=[t for t in agent.TOOLS if sender != "bench"
                             or t["function"]["name"] in READ_TOOLS], run_tool=run_tool, send_json=send_json, send_audio=send_audio,
                      on_turn=on_turn, greet=greet)
    await rc.start()
    await send_json({"type": "ready", "engine": engine, "model": model, "voice": voice, "rate": RATE})
    pump = asyncio.create_task(rc.run())
    deadline = time.time() + MAX_CALL_S
    try:
        while not pump.done():
            recv = asyncio.create_task(ws.receive())
            done, _ = await asyncio.wait({recv, pump}, timeout=deadline - time.time(),
                                         return_when=asyncio.FIRST_COMPLETED)
            if recv not in done:
                recv.cancel()
                break
            msg = recv.result()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                await rc.feed(msg["bytes"])
                continue
            try:
                ev = json.loads(msg.get("text") or "{}")
            except json.JSONDecodeError:
                continue
            kind = ev.get("type")
            if kind == "bye":
                break
            if kind == "played":
                await rc.played(int(ev.get("id") or 0), int(ev.get("ms") or 0), bool(ev.get("done")))
            elif kind == "interrupt":
                await rc.interrupt()
            elif kind == "mute":
                await rc.mute(bool(ev.get("on")))
            elif kind == "say" and ev.get("text"):
                await rc.say(str(ev["text"])[:2000])
    finally:
        await rc.close()
        pump.cancel()


# --- entry -------------------------------------------------------------------------
async def serve(ws: WebSocket):
    """The /live WebSocket. Localhost only; g-command-web authenticates and relays."""
    await ws.accept()
    hello = None
    try:
        async with asyncio.timeout(10):
            while hello is None:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                if msg.get("text"):
                    ev = json.loads(msg["text"])
                    if ev.get("type") == "hello":
                        hello = ev
    except (TimeoutError, json.JSONDecodeError):
        await ws.close(code=4400)
        return
    sender = hello.get("session") if hello.get("session") in SESSIONS else "talk"
    client = str(hello.get("client") or "web")[:16]
    greet = hello.get("greet", True) is not False
    engine = config.LIVE_ENGINE
    if engine in ("xai", "openai") and not (config.XAI_API_KEY if engine == "xai" else config.OPENAI_API_KEY):
        log.warning("live engine %s has no key, using openrouter", engine)
        engine = "openrouter"
    log.info("live call start sender=%s client=%s engine=%s", sender, client, engine)
    try:
        if engine in ("xai", "openai"):
            await _relay(ws, sender, hello.get("voice"), engine, greet)
            return
        voice = str(hello.get("voice") or config.LIVE_VOICE)
        call = Call(ws, sender, voice, client)
        await call.open()
        await call.send({"type": "ready", "engine": "openrouter", "model": config.LIVE_MODEL,
                         "voice": voice, "rate": RATE})
        try:
            await call.run(greet)
        finally:
            await call.close()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.exception("live call failed")
        try:
            await ws.send_text(json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"}))
        except Exception:
            pass
    finally:
        if ws.client_state == WebSocketState.CONNECTED:
            try:
                await ws.close()
            except (RuntimeError, WebSocketDisconnect):
                pass                          # the client went first
