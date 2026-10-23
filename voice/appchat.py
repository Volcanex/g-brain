"""The app session: the G-Command iPhone and Mac chat.

Same brain, same history store, same tools as agent.handle, plus what a screen
can show that WhatsApp cannot:

- Rich replies. The model can attach blocks from g-command-web's panel contract
  (`show_panel`, `attach_blocks`), checked by blocks.py. A turn ends with
  `{"reply": text, "blocks": [...]}`; text-only turns have `blocks: []`.
- A line budget. The app measures its visible chat area and sends
  `{"lines", "chars_per_line"}`; the prompt asks for the short answer first,
  within that, going over only when the detail is worth it. A soft guide.
- Attachments. g-command-web stores uploads and hands the turn the image bytes
  (base64) and small text files; this module puts them in the user message.
- Agent activity, READ ONLY. `agent_activity` reads tmux sessions, their recent
  pane output and recent Claude Code sessions through g-command-web's
  `/api/sessions/activity`, using a short-lived grant the proxy mints per turn.
  There is deliberately no tool that types into, drafts for or drives a session:
  Gabriel does that himself from Workspaces.

`stream_turn` is the one code path: /chat/stream relays its events as NDJSON and
/chat (session "app") collects them into one JSON reply.
"""
import datetime
import json
import time

import httpx

from . import agent, blocks, config, talk, transcript
from .brief import build_brief
from .prompt import SYSTEM, date_rule

MAX_TOKENS = 1500
MAX_IMAGES = 4

APP = """

## This chat is the G-Command app
You are in the owner's G-Command app (iPhone and Mac), not WhatsApp, so the
WhatsApp formatting notes above do not apply. Your text shows as a chat bubble:
plain sentences, no markdown (no #, no ** **, no tables). Never put a list in the
text: two or more items go in a bullets or rows block, and the text says what
matters about them in a sentence.

Under your text the app can draw structured blocks, the same ones its dashboard
uses. Use them only where they genuinely help, and keep text-only for chat, a
single fact or a yes/no:
- show_panel(name): the live dashboard panel. "What's on today/this week" ->
  calendar; todos -> tasks; money; services/system health -> health; Claude and
  model quotas -> compute; tickets; crons; the morning brief -> brief. Then say in
  a sentence or two what matters. Never retype what the panel shows.
- attach_blocks(blocks): three or more parallel items -> bullets; things with a
  status -> rows with chips; secondary caveats or small print -> detail; a long
  tail the owner may not need -> a fold (collapsed) holding it; a photo from this
  chat -> image with its url. The tool says what it dropped, so fix and retry.
- agent_activity: what the coding agents are doing across hosts (tmux sessions,
  their latest pane output, recent Claude Code sessions). It is READ ONLY. You
  can never type into, answer, draft for or drive an agent session; if asked to,
  say the owner does that from Workspaces.
"""

BUDGET = """
Screen budget: the visible chat area fits about {lines} lines of about {cpl}
characters (about {chars} characters). Lead with the short answer and make it fit
that. Go over only when the extra detail is worth it, and then prefer putting it
in a collapsed fold so the answer still reads first. This is a guide, not a limit.
"""

APP_TOOLS = [
    {"type": "function", "function": {
        "name": "show_panel",
        "description": "Show a live dashboard panel under your reply, rendered exactly like the dashboard.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "enum": list(blocks.PANELS)}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "attach_blocks",
        "description": (
            "Attach structured blocks under your reply. Types: "
            "bullets {items:[{text,label?}]}; "
            "rows {title?, rows:[{title, detail?, lead?:Chip, trail?:Chip, marks?:[Chip], done?, accent?:tone}]}; "
            "line {items:[Chip]}; meter {label, value, max, text?}; "
            "detail {text} (small secondary text); image {url, alt?} (url must be /api/voice/files/<id>); "
            "fold {title, open?:false, blocks:[...]} (collapsible, one level). "
            "Chip = {text, tone?: ok|warn|danger|accent|muted, badge?:bool}."),
        "parameters": {"type": "object", "properties": {
            "blocks": {"type": "array", "items": {"type": "object"}}}, "required": ["blocks"]}}},
    {"type": "function", "function": {
        "name": "agent_activity",
        "description": ("Read-only view of agent work across hosts: tmux sessions with their latest pane "
                        "output and recent Claude Code sessions. Give host and session to read one pane deeper."),
        "parameters": {"type": "object", "properties": {
            "host": {"type": "string"}, "session": {"type": "string"},
            "lines": {"type": "integer", "description": "Pane lines to read, 1-80."}}}}},
]
TOOLS = agent.TOOLS + APP_TOOLS


def _stream(messages, model=None):
    return talk._stream_model(messages, True, tools=TOOLS, max_tokens=MAX_TOKENS, model=model)


def read_activity(activity, host=None, session=None, lines=None):
    """GET g-command-web's read-only activity route with this turn's grant."""
    if not activity or not activity.get("url") or not activity.get("grant"):
        return {"error": "agent activity is not available on this turn"}
    if not session and not lines:
        lines = 4   # the overview: a few lines per session keeps every host in view
    params = {k: v for k, v in (("host", host), ("session", session), ("lines", lines)) if v}
    try:
        r = httpx.get(activity["url"], params=params, timeout=30,
                      headers={"X-Voice-Grant": activity["grant"]})
    except httpx.HTTPError as e:
        return {"error": f"activity unreachable: {type(e).__name__}"}
    if r.status_code != 200:
        return {"error": f"activity {r.status_code}"}
    return r.json()


def _app_tool(name, args_json, state, activity):
    try:
        args = json.loads(args_json or "{}") or {}
    except json.JSONDecodeError:
        return {"error": "bad tool arguments"}
    if name == "show_panel":
        b = blocks.clean([{"type": "panel", "name": args.get("name")}])
        if not b:
            return {"error": f"unknown panel; one of {', '.join(blocks.PANELS)}"}
        state.extend(b)
        return {"ok": True, "shown": args["name"],
                "note": "The app draws this panel live under your text. Summarise what matters; do not list it."}
    if name == "attach_blocks":
        raw = args.get("blocks")
        good = blocks.clean(raw)
        room = blocks.MAX_BLOCKS - len(state)
        state.extend(good[:max(0, room)])
        out = {"ok": bool(good), "attached": len(good[:max(0, room)])}
        bad = blocks.problems(raw)
        if bad:
            out["dropped"] = bad
        return out
    if name == "agent_activity":
        return read_activity(activity, args.get("host"), args.get("session"), args.get("lines"))
    return {"error": f"unknown tool {name}"}


def user_content(text, images=None, files=None):
    """(content for the model, text for history). Images arrive as
    {"content_type", "data" (base64)}; files as {"name", "content_type", "text"?}."""
    note = []
    body = text or ""
    for f in files or []:
        if f.get("text"):
            body += f"\n\n[file {f.get('name', 'file')}]\n{f['text']}"
        else:
            body += f"\n\n[file {f.get('name', 'file')} ({f.get('content_type', '?')}): contents not readable here]"
        note.append(f"[file: {f.get('name', 'file')}]")
    images = [i for i in (images or []) if i.get("data") and i.get("content_type")][:MAX_IMAGES]
    note += ["[photo]"] * len(images)
    history_text = " ".join([text or ""] + note).strip()
    if not images:
        return body, history_text
    parts = [{"type": "text", "text": body or "(photo)"}]
    parts += [{"type": "image_url", "image_url": {"url": f"data:{i['content_type']};base64,{i['data']}"}}
              for i in images]
    return parts, history_text


def system_prompt(budget=None, model=None):
    today = datetime.date.today().isoformat()
    s = (SYSTEM + date_rule(today) + f"\nYou are currently running on the model: {model or config.MODEL}\n"
         + "\n## Right now (your short-term memory)\n" + build_brief() + APP)
    if budget and budget.get("lines") and budget.get("chars_per_line"):
        lines, cpl = int(budget["lines"]), int(budget["chars_per_line"])
        s += BUDGET.format(lines=lines, cpl=cpl, chars=lines * cpl)
    return s


def stream_turn(session, text, *, budget=None, images=None, files=None, attachments=None, activity=None):
    """Yield {"t":"delta","text"}, {"t":"tool","name"}, then
    {"t":"done","reply","blocks","tools","ms"}. `attachments` is the upload
    metadata recorded with the user's turn so history can show what was sent."""
    t0 = time.time()
    ms = lambda: int((time.time() - t0) * 1000)  # noqa: E731
    if session not in agent._HISTORY:
        agent._HISTORY[session] = transcript.load_tail(session, agent._MAX_TURNS)
    history = agent._HISTORY[session]
    content, history_text = user_content(text, images, files)
    history.append({"role": "user", "content": history_text})
    transcript.append(session, "user", history_text, attachments=attachments or None)

    # A turn with an image runs on the vision model (config.VISION_MODEL says why).
    model = config.VISION_MODEL if isinstance(content, list) else None
    messages = [{"role": "system", "content": system_prompt(budget, model)}] + history[-agent._MAX_TURNS:]
    if isinstance(content, list) or content != history_text:
        messages[-1] = {"role": "user", "content": content}

    parts, state, used = [], [], []
    for turn_index in range(config.MAX_TOOL_ITERATIONS):
        said, calls = "", []
        for kind, val in _stream(messages, model):
            if kind == "text":
                if not said and parts:
                    yield {"t": "delta", "text": "\n\n"}
                said += val
                yield {"t": "delta", "text": val}
            else:
                calls = val
        if said.strip():
            parts.append(said.strip())
        if not calls:
            break
        messages.append({"role": "assistant", "content": said or None, "tool_calls": calls})
        for call in calls:
            name = call["function"]["name"]
            args = call["function"].get("arguments")
            used.append(name)
            yield {"t": "tool", "name": name}
            if name in ("show_panel", "attach_blocks", "agent_activity"):
                result = _app_tool(name, args, state, activity)
            else:
                result = agent._run_tool(name, args, session_id=call["id"], turn_id=turn_index)
            err = result.get("error") if isinstance(result, dict) else None
            agent.log.info("tool sender=%s name=%s args=%s -> %s", session, name,
                           str(args)[:300], f"ERROR {err}" if err else "ok")
            cap = 16000 if name == "agent_activity" else 6000
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(result, default=str)[:cap]})
    else:
        parts.append("Sorry, I got stuck working through that. Try rephrasing?")

    reply = "\n\n".join(parts) or "..."
    history.append({"role": "assistant", "content": reply})
    transcript.append(session, "assistant", reply, blocks=state or None)
    yield {"t": "done", "reply": reply, "blocks": state, "tools": used, "ms": ms()}


def collect(session, text, **kw):
    """The whole turn as one {"reply", "blocks"}."""
    done = {}
    for ev in stream_turn(session, text, **kw):
        if ev["t"] == "done":
            done = ev
    return {"reply": done.get("reply", "..."), "blocks": done.get("blocks", [])}
