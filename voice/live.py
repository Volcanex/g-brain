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
import hashlib
import io
import json
import logging
import os
import random
import re
import time
import wave
from collections import deque
from zoneinfo import ZoneInfo

import httpx
import numpy as np
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
ECHO_LAG_MS = 512          # bench replay: 480 ms worst lag with a 250 ms playback stall, +32 ms VAD frame
ECHO_LEAD_MS = 64          # one 32 ms VAD frame plus the bench's 20 ms playback scheduling quantum
ECHO_DECIMATE = 4          # 6 kHz matching retains speech while bounding a lag search to ~3500 samples
ECHO_CORRELATION = 0.85    # require >=72% explained power before treating a frame as an echo-only candidate
ECHO_GAIN_FRAMES = 5       # five echo-only estimates (160 ms at 32 ms/frame) retain peaks across brief dips
ECHO_MARGIN = 1.25         # replay's worst echo-only ratio was 1.023; 25% headroom preserves quiet barge-ins
ECHO_LEARN_HOLD_MS = 300   # hold through VAD's 300 ms confirmation; similar vowels poisoned gain after 180 ms
RATE_REPLIES = 40          # per minute per session: a reply heard back through the
RATE_WINDOW = 60.0         # speakers can loop, and every reply costs a model call
STT_WAIT_S = 6.0
STT_TRIES = 2              # an OpenRouter error body once left a turn with no caption
GOODBYE_WAIT_S = 2.0       # end_call waits this long for the transcript; 6 s held a "stop" turn 9 s
ACK_WAIT_S = 0.6           # for the client's playhead after a flush
MAX_TURN_S = 120           # one turn sent to the model holds at most this much audio
FILLER_DIR = os.path.join(os.environ.get("VOICE_LIVE_MODELS")
                          or os.path.expanduser("~/.local/share/voice-live"), "fillers")
FILLERS = ("One sec.", "Let me check.", "Hang on.")
# Read once per voice like the fillers, so the call picks up at once and the
# greeting can't contradict the clock ("good morning" at midnight).
GREETINGS = {
    "morning": ("Morning, {owner}.", "Hey, morning.", "Morning. What's up?"),
    "afternoon": ("Hey, {owner}.", "Afternoon.", "Hiya, what's up?"),
    "evening": ("Evening, {owner}.", "Hey, evening.", "Hiya, what's up?"),
    "late": ("Hey, you're up late.", "Hey, {owner}. Still up?", "Hiya. What's keeping you up?"),
}
# end_call hangs up only when their words say goodbye: "Okay, stop. That's enough."
# after a long answer means stop talking, and the model hung up on it in two of
# three bench runs on 2026-10-25. Staying on the line is the safe way to be wrong.
GOODBYE_WORDS = re.compile(
    r"\b(bye|goodbye|(good ?)?night|ta-?ra|cheerio|see (you|ya)|speak soon|"
    r"talk (to you )?(soon|later)|catch you|that['’]?s all|that['’]?s it for now|hang up|"
    r"end (the )?call|i['’]?m off|got to go|gotta go)\b", re.I)
# For a goodbye given as a bare end_call: a second model call only to say "Bye."
# took most of a second.
GOODBYES = ("Bye.", "Speak soon.", "See you.")
ACKS = ("Okay.", "Sure.", "Got it.")   # a refused hang-up said nothing: one of these, not a second request
END_CALL = {"type": "function", "function": {
    "name": "end_call",
    "description": ("Hang up the call. Only when they say goodbye or that they're done with the "
                    "call (\"bye\", \"that's all, cheers\", \"speak later\"), and say a short "
                    "goodbye as well. Never for \"stop\" or \"that's enough\": those only stop "
                    "what you're saying."),
    "parameters": {"type": "object", "properties": {}}}}

LIVE = """
## This is a live voice call
You are on a phone call with {owner}. You hear their actual voice, so you can
pick up tone, hesitation and mood. Talk like a person on the phone: usually one
or two short sentences, the answer first, then stop and let them talk. British
English, relaxed and natural. It's fine to say "yeah", "right", "fair enough".
Everything you write is spoken, so write no bullets, dashes, numbered lines or
line breaks, and never read out URLs, file paths, slugs, ids, tags or categories.
Say times the way people do: "twenty to midnight", "just gone half eleven",
never "23:37". Start with the answer itself, with no run-up such as "Here's
what's on your list", "Of course" or "Sure, I'll start". For a list, the count
comes first, then two or three items in a few words each, then a short question:
"Six today. Accept the Slack invite, book the graduation place and change the
phone password. Want the rest?"
When you've answered, stop. Don't end with an offer such as "let me know if
you need anything else" or "anything else?": they'll talk when they want to.
If they tell you to stop, say "Sure." or "Okay." and nothing more.
If you need to look something up or change a todo, call the tool straight away
without announcing it: the call already plays a short "one sec" for you.
If what you heard is only noise, a cough or half a word, say a short "Sorry,
say that again?" instead of guessing.
If they cut you off, drop what you were saying and answer the new thing.
When they say goodbye, say a short goodbye back and call end_call.
"""
GREET = ("(The call has just connected and {owner} hasn't said anything yet. Pick up "
         "the way a friend answers the phone: a few words that fit the time of day. "
         "No list, no menu of things you can do.)")
STT_PROMPT = ("Transcribe the speech verbatim, British spelling. Output only the words "
              "spoken. If there is no speech, output nothing.")
READ_PROMPT = ("You are the voice of a phone assistant recording short audio clips. The user gives you a "
               "line in quotes. Speak exactly that line, word for word, in a relaxed natural tone, and "
               "say nothing else. Never reply to it or add words.")

NOW = ("If the time comes up, it's {time}. Times said earlier in this conversation are out of date. "
       "When you've done what was asked, stop, with no offer at the end.")
_RECENT: dict[str, deque] = {}
_LATE: set[asyncio.Task] = set()
_LAST_GREETING: dict[str, str] = {}


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


_HOURS = ("twelve", "one", "two", "three", "four", "five", "six", "seven", "eight",
          "nine", "ten", "eleven")
_MINUTES = ("", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
            "eleven", "twelve", "thirteen", "fourteen", "quarter", "sixteen", "seventeen",
            "eighteen", "nineteen", "twenty", "twenty-one", "twenty-two", "twenty-three",
            "twenty-four", "twenty-five", "twenty-six", "twenty-seven", "twenty-eight",
            "twenty-nine", "half")


def spoken_time(t: datetime.datetime) -> str:
    """The clock the way people say it ("twenty to midnight"): handed to the model
    ready-made, because told to say times naturally it still read out "23:42"."""
    h, m = t.hour, t.minute
    if m > 30:
        h, m, rel = (h + 1) % 24, 60 - m, "to"
    else:
        rel = "past"
    hour = "midnight" if h == 0 else "midday" if h == 12 else _HOURS[h % 12]
    if m == 0:
        return hour if h in (0, 12) else f"{hour} o'clock"
    count = _MINUTES[m] + ("" if m % 5 == 0 or m in (15, 30) else " minute" if m == 1 else " minutes")
    when = "" if h in (0, 12) else (" in the morning" if h < 12 else " in the afternoon" if h < 17
                                    else " in the evening" if h < 22 else " at night")
    return f"{count} {rel} {hour}{when}"


def _todo_digest(todos: list, start: int = 0) -> dict:
    """What a call needs from list_todos: the counts and the next three, soonest first.
    The full records (56 with bodies on 2026-10-24) were cut at 6000 characters, so the
    model could neither count them nor afford them. Given a flat list it read every
    item out, and given three to say plus more "only if asked" it still read five, so
    it gets three at a time and asking again pages on."""
    today = datetime.datetime.now(ZoneInfo("Europe/London")).date()
    soon = sorted(todos, key=lambda t: (t.get("due") or "9999", t.get("priority") or "P9"))
    page = [{"slug": t.get("slug"), "title": t.get("title"), "when": spoken_due(t.get("due"), today),
             "priority": t.get("priority")} for t in soon[start:start + 3]]
    due = sum(1 for t in todos if t.get("due") and t["due"] <= today.isoformat())
    return {"open_in_all": len(todos), "due_today_or_overdue": due, "say_these": page,
            "more_after_these": max(0, len(todos) - start - len(page))}


def _todo_query(args) -> str:
    """The query with defaults filled in: the model asks for the same list with and
    without status "open"."""
    try:
        a = json.loads(args) if isinstance(args, str) else dict(args or {})
    except (ValueError, TypeError):
        a = {}
    if not isinstance(a, dict):
        a = {}
    return json.dumps({"status": "open", **{k: v for k, v in a.items() if v is not None}}, sort_keys=True)


def spoken_due(due: str | None, today: datetime.date) -> str | None:
    """"overdue since the 10th of October", "due tomorrow", "due on Friday": read as
    an ISO date it came out "October 10", American and with no word that it was late."""
    if not due:
        return None
    try:
        d = datetime.date.fromisoformat(due[:10])
    except ValueError:
        return None
    days = (d - today).days
    n = d.day
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    date = f"the {n}{suffix} of {d:%B}" + (f" {d.year}" if d.year != today.year else "")
    if days < -6:
        return f"overdue since {date}"
    if days < -1:
        return f"overdue since {d:%A}"
    if days == -1:
        return "overdue since yesterday"
    if days == 0:
        return "due today"
    if days == 1:
        return "due tomorrow"
    if days <= 6:
        return f"due on {d:%A}"
    return f"due on {date}"


def greeting_band(hour: int) -> str:
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "late"


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
        self.hangup = False                        # it said goodbye: end the call once played
        self.todo_page = None                      # list_todos paging, kept once heard
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


class EchoGuard:
    """Adaptive double-talk detector; rejected echo becomes silence before VAD.

    Match only to learn coupling from echo-only frames. The decision compares
    mic RMS with the largest reference RMS in the playhead's jitter window.
    The filter runs on the endpointer's existing frames, adding no buffering.
    """

    def __init__(self):
        self.id = None
        self.audio = bytearray()
        self.ack_at = 0.
        self.ack_ms = 0.
        self.limit_ms = None
        self.estimates = deque(maxlen=ECHO_GAIN_FRAMES)
        self.gain = None
        self.learn_after = 0.

    def start(self, rid, now=None):
        self.id = rid
        self.audio = bytearray()
        self.ack_at = time.monotonic() if now is None else now
        self.ack_ms = 0.
        self.limit_ms = None
        self.learn_after = 0.

    def append(self, pcm):
        self.audio.extend(pcm)

    def playhead(self, now):
        return self.ack_ms + max(0., now - self.ack_at) * 1000

    def played(self, rid, ms, done=False, now=None):
        if rid != self.id or not np.isfinite(ms):
            return
        self.ack_at = time.monotonic() if now is None else now
        self.ack_ms = max(0., min(ms, len(self.audio) / BYTES_PER_MS))
        if done:
            self.limit_ms = self.ack_ms

    def stop(self, rid):
        now = time.monotonic()
        self.played(rid, self.playhead(now), done=True, now=now)

    def filter(self, pcm, now=None, in_speech=False):
        if self.id is None:
            return pcm
        now = time.monotonic() if now is None else now
        head = self.playhead(now)
        start = max(0, int((head - ECHO_LAG_MS) * RATE / 1000))
        end = int((head + ECHO_LEAD_MS) * RATE / 1000)
        if self.limit_ms is not None:
            end = min(end, int(self.limit_ms * RATE / 1000))
        raw = self.audio[start * 2:end * 2]
        if len(raw) < len(pcm):
            return pcm
        # Block averages reduce aliasing and tolerate the client's low-pass AEC.
        width = len(pcm) // 2 // ECHO_DECIMATE * ECHO_DECIMATE
        if not width:
            return pcm
        mic = np.frombuffer(pcm, '<i2')[:width].astype(np.float32)
        mic = mic.reshape(-1, ECHO_DECIMATE).mean(axis=1)
        ref = np.frombuffer(raw, '<i2').astype(np.float32)
        ref = ref[:len(ref) // ECHO_DECIMATE * ECHO_DECIMATE]
        ref = ref.reshape(-1, ECHO_DECIMATE).mean(axis=1)
        power = np.concatenate(([0.], np.cumsum(ref.astype(np.float64) ** 2)))
        power = power[len(mic):] - power[:-len(mic)]
        mic_power = float(np.dot(mic, mic))
        if mic_power == 0 or not power.size or power.max() == 0:
            return pcm
        dots = np.correlate(ref, mic, 'valid')
        correlations = np.abs(dots) / np.sqrt(np.maximum(power * mic_power, 1))
        best = int(np.argmax(correlations))
        # Once playback has stopped, only a waveform match can reject its tail;
        # a quiet new speaker must not inherit the old reply's energy threshold.
        if self.limit_ms is not None and correlations[best] < ECHO_CORRELATION:
            return pcm
        if correlations[best] >= ECHO_CORRELATION and not in_speech and now >= self.learn_after:
            self.estimates.append(float(abs(dots[best]) / power[best]))
            self.gain = max(self.estimates)
        if self.gain is not None and mic_power <= ECHO_MARGIN ** 2 * self.gain ** 2 * power.max():
            return bytes(len(pcm))
        if self.gain is not None:
            self.learn_after = now + ECHO_LEARN_HOLD_MS / 1000
        return pcm


class Call:
    """One call on the openrouter engine."""

    def __init__(self, ws: WebSocket, sender: str, voice: str, client: str):
        self.ws, self.sender, self.voice, self.client = ws, sender, voice, client
        self.ep = None
        self.echo = EchoGuard()
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
        self.todo_at = (0, 0, "")           # turn, next item and query of the last list_todos heard
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
        self.ep = await asyncio.to_thread(
            turn.Endpointer, RATE, frame_filter=lambda pcm: self.echo.filter(
                pcm, in_speech=self.ep.in_speech))

    async def run(self, greet: bool):
        if self.sender == "bench":
            # Each bench call starts clean, or it copies the last run's times and lists
            # and two runs no longer measure the same thing.
            agent._HISTORY[self.sender] = []
        elif self.sender not in agent._HISTORY:
            agent._HISTORY[self.sender] = await asyncio.to_thread(
                transcript.load_tail, self.sender, agent._MAX_TURNS)
        self.brief = await asyncio.to_thread(build_brief)
        self.spawn(self.load_fillers())
        self.spawn(self.load_lines())
        if greet:
            await self.start(Reply(self.next_id(), self.turn, "greet"))
        else:
            await self.set_state("listening")
        deadline = self.started + MAX_CALL_S
        while True:
            try:
                msg = await asyncio.wait_for(self.ws.receive(), deadline - time.time())
            except (TimeoutError, RuntimeError):
                break                         # RuntimeError: we hung up and the socket is gone
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
            self.echo.played(p.id, p.played_ms, bool(ev.get("done")))
            if p.cut:
                p.ack.set()
            if ev.get("done"):
                p.playback_done = True
                p.ended.set()
                if p is r and not r.generating() and not self.ep.in_speech and not r.hangup:
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
        self.echo.stop(r.id)
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
                + f"It's {now:%A}, {spoken_time(now)}, in London right now. Say the time like that.\n")

    def messages(self, r: Reply) -> list:
        history = agent._HISTORY[self.sender][-agent._MAX_TURNS:]
        msgs = [{"role": "system", "content": self.system_prompt()}] + history
        if history:
            # The shared history holds old calls: the bench heard "twenty to midnight"
            # copied from one at 23:51. The reminder sits last, where it wins.
            now = datetime.datetime.now(ZoneInfo("Europe/London"))
            msgs.append({"role": "system", "content": NOW.format(time=spoken_time(now))})
        if r.kind == "greet":
            msgs.append({"role": "user", "content": GREET.format(owner=config.OWNER_NAME)})
        elif r.kind == "text":
            msgs.append({"role": "user", "content": r.user_text})
        else:
            msgs.append(_audio_message(r.pcm))
        return msgs

    async def respond(self, r: Reply):
        if r.kind == "greet":
            line, pcm = self.ready_greeting()
            if pcm:
                r.text = line
                await self.send({"type": "assistant", "id": r.id, "text": line, "final": False})
                await self.speak(r, pcm)
                return
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
            work = [c for c in calls if c["function"]["name"] != "end_call"]
            if len(work) < len(calls) and GOODBYE_WORDS.search(await self.user_text(r, GOODBYE_WAIT_S)):
                r.hangup = True
                if not work and (said.strip() or await self.say_ready(r, GOODBYES)):
                    return                    # the goodbye is said: hang up once it has played
            elif len(work) < len(calls) and not work and (said.strip() or await self.say_ready(r, ACKS)):
                return                        # not a goodbye: it answered, and the call stays up
            if not calls:
                return
            messages.append({"role": "assistant", "content": said or None, "tool_calls": calls})
            if said.strip():
                # It already said "let me check" itself, so no filler, and the answer
                # that follows needs a space before it in the caption.
                if not r.text.endswith(" "):
                    r.text += " "
                    await self.send({"type": "assistant", "id": r.id, "text": " ", "final": False})
            elif work:
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
                                or t["function"]["name"] in READ_TOOLS] + [END_CALL]
            payload["tool_choice"] = "auto"
        t_req = time.time()
        try:
            return await self._stream(r, payload, t_req)
        except asyncio.CancelledError:
            # Cut off before the usage chunk: ask OpenRouter what it cost instead.
            if r.gen and not r.charged:
                t = asyncio.create_task(self.charge_late(r, r.gen))
                _LATE.add(t)                  # outlives the call: the stats can take 20 s
                t.add_done_callback(_LATE.discard)
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
            self.echo.start(r.id)
            self.held, self.held_eos = b"", None
            await self.send({"type": "reply", "id": r.id})
            await self.set_state("speaking")
        r.audio_ms += len(pcm) / BYTES_PER_MS
        self.echo.append(pcm)
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
        if name == "end_call":
            result = ({"ok": "the call ends once your goodbye has played"} if r.hangup else
                      {"error": "not a goodbye, so the call stays up. Acknowledge their last words in a "
                                "few words and stop. Do not repeat an earlier answer."})
            return {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)}
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
        if name == "list_todos" and isinstance(result, list):
            # Asked again in the next turn ("what else?"), it pages on. A different
            # query starts over, and close_out keeps the page only if it was heard.
            key = _todo_query(args)
            turn, start, last = self.todo_at
            start = start if r.turn - turn <= 1 and key == last and start < len(result) else 0
            result = _todo_digest(result, start)
            r.todo_page = (r.turn, start + len(result["say_these"]), key, r.audio_ms)
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
        if r.cut and r.started and not r.ack.is_set():
            try:
                await asyncio.wait_for(r.ack.wait(), ACK_WAIT_S)
            except TimeoutError:
                pass
        if r.todo_page and (not r.cut or r.played_ms > r.todo_page[3]):
            self.todo_at = r.todo_page[:3]    # cut before the list arrived, it was never heard
        if r.started or r.text:
            # A cut reply's final caption is what was heard, as it is remembered.
            await self.send({"type": "assistant", "id": r.id, "text": self.said(r), "final": True})
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
        if r.hangup and not r.cut and await self.hang_up(r):
            pass
        elif self.reply is r and not r.cut and not self.ep.in_speech:
            if r.started:
                left = r.first_audio_at + r.audio_ms / 1000 - time.time()
                if left > 0 and not r.playback_done:
                    try:
                        await asyncio.wait_for(r.ended.wait(), left)
                    except TimeoutError:
                        pass
            if self.reply is r and not r.cut and not self.ep.in_speech:
                await self.set_state("listening")
        await self.remember(r)

    async def hang_up(self, r: Reply) -> bool:
        """End the call from this side once the goodbye has played, unless they
        talked over it or started a new turn. The client's done ack is the signal:
        a deadline from the first audio runs short when the stream stalled."""
        if r.started and not r.playback_done:
            left = r.first_audio_at + r.audio_ms / 1000 - time.time()
            try:
                await asyncio.wait_for(r.ended.wait(), max(left, 0) + 2)
            except TimeoutError:
                pass
        async with self.send_lock:            # nothing can cut in between the check and the end
            if r.cut or self.reply is not r or self.ep.in_speech:
                return False
            log.info("live call hung up after goodbye sender=%s id=%d", self.sender, r.id)
            try:
                await self.ws.send_text(json.dumps({"type": "end", "reason": "goodbye"}))
                await self.ws.close(code=1000)
            except Exception:
                pass
        return True

    async def send_metrics(self, r: Reply):
        r.metrics_sent = True
        await self.send({"type": "metrics", "id": r.id, "eos_ms": r.eos_ms, "ttfa_ms": r.ttfa_ms,
                         "cost": round(r.cost, 6)})

    async def charge_late(self, r: Reply, gen: str):
        """OpenRouter bills a cancelled stream for what it generated; its stats appear
        5 to 20 s later. A second metrics event carries the corrected cost."""
        async with httpx.AsyncClient(timeout=10) as http:
            for delay in (2, 3, 5, 10):
                await asyncio.sleep(delay)
                try:
                    resp = await http.get(GENERATION_URL, params={"id": gen}, headers=_headers())
                    cost = (resp.json().get("data") or {}).get("total_cost") if resp.status_code == 200 else None
                except Exception:
                    cost = None
                if cost is not None:
                    self.charge(r, {"cost": cost})
                    log.info("live late cost sender=%s id=%d cost=$%.5f", self.sender, r.id, cost)
                    if r.metrics_sent:
                        await self.send_metrics(r)
                    return
        log.warning("live cost unknown for cut reply %s", gen)

    def said(self, r: Reply) -> str:
        if not r.cut:
            return r.text.strip()
        if r.started:
            return heard(r.text.strip(), self.heard_ms(r), r.audio_ms)
        return ""                             # cut before a word of it was played

    async def remember(self, r: Reply):
        if r.kind == "greet":
            return
        said = self.said(r)
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
    async def user_text(self, r: Reply, wait: float = STT_WAIT_S) -> str:
        if r.kind == "text":
            return r.user_text
        if r.stt is None:
            return ""
        try:
            return await asyncio.wait_for(asyncio.shield(r.stt), wait) or ""
        except Exception:
            return ""

    async def transcribe(self, r: Reply) -> str:
        payload = {"model": config.LIVE_STT_MODEL, "temperature": 0, "max_tokens": 400,
                   "messages": [{"role": "system", "content": STT_PROMPT}, _audio_message(r.pcm)],
                   "usage": {"include": True}}
        text = ""
        for _ in range(STT_TRIES):
            try:
                resp = await self.http.post(config.OPENROUTER_URL, headers=_headers(), json=payload)
                j = resp.json()
                self.charge(r, j.get("usage"))
                if resp.is_success and j.get("choices"):
                    text = " ".join(((j["choices"][0].get("message") or {}).get("content") or "").split())
                    break
                log.warning("live stt failed: HTTP %s %s", resp.status_code, str(j.get("error") or "")[:200])
            except Exception as e:
                log.warning("live stt failed: %s: %s", type(e).__name__, e)
            if r.discard:
                break
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

    def line_path(self, line: str) -> str:
        key = hashlib.sha1(line.encode()).hexdigest()[:12]
        return os.path.join(FILLER_DIR, self.voice, f"line-{key}.pcm")

    def greeting_lines(self, band: str) -> list[str]:
        return [g.format(owner=config.OWNER_NAME) for g in GREETINGS.get(band, ())]

    def ready_greeting(self) -> tuple[str, bytes]:
        band = greeting_band(datetime.datetime.now(ZoneInfo("Europe/London")).hour)
        ready = [g for g in self.greeting_lines(band) if os.path.exists(self.line_path(g))
                 and g != _LAST_GREETING.get(self.sender)]
        if not ready:
            return "", b""
        line = random.choice(ready)
        _LAST_GREETING[self.sender] = line
        with open(self.line_path(line), "rb") as f:
            return line, f.read()

    async def say_ready(self, r: Reply, lines) -> bool:
        ready = [g for g in lines if os.path.exists(self.line_path(g))]
        if not ready:
            return False
        line = random.choice(ready)
        with open(self.line_path(line), "rb") as f:
            pcm = f.read()
        r.text += line
        await self.send({"type": "assistant", "id": r.id, "text": line, "final": False})
        await self.speak(r, pcm)
        return True

    async def load_lines(self):
        """Read any missing greetings and goodbyes aloud, this hour's greetings first.
        Until a line is ready the model says it."""
        now = greeting_band(datetime.datetime.now(ZoneInfo("Europe/London")).hour)
        bands = sorted(GREETINGS, key=lambda b: b != now)
        lines = [g for b in bands for g in self.greeting_lines(b)] + list(GOODBYES) + list(ACKS)
        for line in dict.fromkeys(lines):
            path = self.line_path(line)
            if os.path.exists(path):
                continue
            try:
                pcm = await self.read_aloud(line)
            except Exception as e:
                log.warning("live line %r failed: %s", line, e)
                continue
            if pcm:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path + ".tmp", "wb") as f:
                    f.write(pcm)
                os.replace(path + ".tmp", path)

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
                    + f"It's {now:%A}, {spoken_time(now)}, in London right now. Say the time like that.\n"
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
            await ws.send_text(json.dumps({"type": "error", "message": "The call broke on my end. Call again?"}))
        except Exception:
            pass
    finally:
        if ws.client_state == WebSocketState.CONNECTED:
            try:
                await ws.close()
            except (RuntimeError, WebSocketDisconnect):
                pass                          # the client went first
