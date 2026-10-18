"""Spoken turns for the talk page: the same agent, streamed a sentence at a time.

agent.handle() waits for the whole reply, which is fine for WhatsApp and too slow
to speak. stream_turn() streams the model's tokens and yields each sentence as
soon as it closes, so the page can start Kokoro on sentence one while the model
is still writing sentence two.

Small talk ("hey", "thanks", "how are you") skips the tools: no tool schema is
sent, so the model cannot wander off into a search before saying hello. Anything
that sounds like a request for the brain keeps the full tool loop, streamed too;
tool turns cost one model round each before the spoken answer starts.

History and transcript are shared with agent.handle, so a talk session reads back
like any other conversation.
"""
import datetime
import json
import re
import time
from collections import deque

import httpx

from . import agent, config, transcript
from .brief import build_brief
from .prompt import SYSTEM

SPOKEN = """

## This turn is spoken aloud
You are on a live voice call, not WhatsApp. Your reply goes straight to a speech
synthesiser. Talk the way a person does on the phone: one to three short
sentences, the answer first. No markdown, no lists, no bullet points, no emoji,
no URLs, no file paths read out. Say times and dates the way people say them.
If a tool result is long, give the one or two things that matter and offer more.
"""

# Words that mean "this needs the brain". Anything short without one is small talk.
_ACTION = re.compile(
    r"\b(todo|to-do|task|remind|reminder|remember|note|add|create|schedule|book|"
    r"calendar|diary|agenda|plan|today|tonight|tomorrow|yesterday|week|weekend|month|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|when|what's on|whats on|"
    r"due|deadline|list|search|find|look up|brain|pulse|todos|open|done|finish|"
    r"move|delete|cancel|change|update|who|where|which|how many|did i|have i|do i)\b",
    re.I)


# Spoken turns are capped per session: a reply the page hears through its own
# speakers can come back as a new turn, and each turn costs model calls.
MAX_TOKENS = 400
RATE_TURNS = 6
RATE_WINDOW = 60.0
SLOW_DOWN = "Slow down a moment, give me a few seconds before the next one."
_RECENT: dict[str, deque] = {}


def _rate_limited(sender: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    q = _RECENT.setdefault(sender, deque())
    while q and now - q[0] > RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_TURNS:
        return True
    q.append(now)
    return False


def is_small_talk(text: str) -> bool:
    words = text.split()
    return len(words) <= 10 and not _ACTION.search(text)


# A sentence ends at . ! ? followed by space, or at a newline. A long run with no
# full stop is cut at a comma so the first audio does not wait on a 40-word clause.
_END = re.compile(r"(.+?[.!?…]+[\"')\]]*)(\s+|$)|(.+?)\n+", re.S)


def _pop_sentences(buf: str, final: bool, eager: bool = False):
    out = []
    while True:
        m = _END.match(buf)
        if not m or (m.end() == len(buf) and not final and not m.group(3)):
            break
        s = (m.group(1) or m.group(3) or "").strip()
        buf = buf[m.end():]
        if s:
            out.append(s)
    # Before anything is spoken, the first clause goes out on its own: Kokoro on
    # CPU takes roughly half a sentence's playing time to make it, so a short
    # opener is what gets audio started.
    limit, floor = (60, 12) if eager and not out else (140, 40)
    if len(buf) > limit or (eager and not out and re.search(r"[,;:] ", buf[floor:])):
        cut = max(buf.rfind(", ", 0, limit), buf.rfind("; ", 0, limit), buf.rfind(": ", 0, limit))
        if cut > floor:
            out.append(buf[:cut + 1].strip())
            buf = buf[cut + 1:].lstrip()
    if final and buf.strip():
        out.append(buf.strip())
        buf = ""
    return out, buf


def _clean(s: str) -> str:
    s = s.replace("**", "").replace("—", ", ").replace("–", "-")
    s = re.sub(r"^\s*[-*#]+\s*", "", s)
    return s.strip()


def _stream_model(messages, use_tools):
    """Yield ("text", delta) and finally ("calls", [tool_call, ...])."""
    headers = {"Authorization": f"Bearer {config.OPENROUTER_API_KEY}", "X-Title": "brain-voice-talk"}
    if config.PUBLIC_URL:
        headers["HTTP-Referer"] = config.PUBLIC_URL
    payload = {"model": config.MODEL, "messages": messages, "stream": True,
               "max_tokens": MAX_TOKENS}
    if use_tools:
        payload["tools"] = agent.TOOLS
        payload["tool_choice"] = "auto"
    calls: dict[int, dict] = {}
    with httpx.Client(timeout=60) as client:
        with client.stream("POST", config.OPENROUTER_URL, headers=headers, json=payload) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                for ch in chunk.get("choices") or []:
                    d = ch.get("delta") or {}
                    if d.get("content"):
                        yield "text", d["content"]
                    for tc in d.get("tool_calls") or []:
                        c = calls.setdefault(tc.get("index", 0), {
                            "id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            c["id"] = tc["id"]
                        f = tc.get("function") or {}
                        if f.get("name"):
                            c["function"]["name"] += f["name"]
                        if f.get("arguments"):
                            c["function"]["arguments"] += f["arguments"]
    yield "calls", [calls[i] for i in sorted(calls)]


def stream_turn(sender: str, text: str):
    """Yield events: {"t":"s","text":sentence,"ms":since_start},
    {"t":"tool","name":...,"ms":...}, then {"t":"done",...}."""
    t0 = time.time()
    ms = lambda: int((time.time() - t0) * 1000)  # noqa: E731

    if _rate_limited(sender):
        agent.log.warning("talk sender=%s rate limited, model not called", sender)
        yield {"t": "s", "text": SLOW_DOWN, "ms": ms()}
        yield {"t": "done", "ms": ms(), "first_ms": ms(), "small_talk": False, "tools": [], "limited": True}
        return

    if sender not in agent._HISTORY:
        agent._HISTORY[sender] = transcript.load_tail(sender, agent._MAX_TURNS)
    history = agent._HISTORY[sender]
    history.append({"role": "user", "content": text})
    transcript.append(sender, "user", text)

    small = is_small_talk(text)
    today = datetime.date.today().isoformat()
    system = (SYSTEM + f"\n\nToday's date is {today}.\nYou are currently running on the model: {config.MODEL}\n"
              + "\n## Right now (your short-term memory)\n" + build_brief() + SPOKEN)
    messages = [{"role": "system", "content": system}] + history[-agent._MAX_TURNS:]

    spoken, tools_used = [], []
    first_ms = None
    for _ in range(config.MAX_TOOL_ITERATIONS):
        buf, said, calls = "", "", []
        for kind, val in _stream_model(messages, use_tools=not small):
            if kind == "text":
                buf += val
                said += val
                out, buf = _pop_sentences(buf, final=False, eager=first_ms is None)
            else:
                calls = val
                out, buf = _pop_sentences(buf, final=True)
            for s in out:
                s = _clean(s)
                if s:
                    if first_ms is None:
                        first_ms = ms()
                    spoken.append(s)
                    yield {"t": "s", "text": s, "ms": ms()}
        if not calls:
            break
        messages.append({"role": "assistant", "content": said or None, "tool_calls": calls})
        for call in calls:
            name = call["function"]["name"]
            tools_used.append(name)
            yield {"t": "tool", "name": name, "ms": ms()}
            result = agent._run_tool(name, call["function"].get("arguments"))
            err = result.get("error") if isinstance(result, dict) else None
            agent.log.info("tool sender=%s name=%s args=%s -> %s", sender, name,
                           str(call["function"].get("arguments"))[:300], f"ERROR {err}" if err else "ok")
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(result, default=str)[:6000]})
    else:
        s = "Sorry, I got stuck on that one."
        spoken.append(s)
        yield {"t": "s", "text": s, "ms": ms()}

    reply = " ".join(spoken) or "..."
    history.append({"role": "assistant", "content": reply})
    transcript.append(sender, "assistant", reply)
    agent.log.info("talk sender=%s small=%s tools=%s first_sentence_ms=%s total_ms=%d",
                   sender, small, ",".join(tools_used) or "-", first_ms, ms())
    yield {"t": "done", "ms": ms(), "first_ms": first_ms, "small_talk": small, "tools": tools_used}
