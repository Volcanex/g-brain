"""The reasoning loop: OpenRouter (OpenAI-compatible) with tool calling.

Per-sender history is persisted to VOICE_TRANSCRIPT_DIR (.pulse/state/voice/ in the
brain) as JSONL transcript files so a service restart no longer wipes context.
The in-memory _HISTORY dict is still used as a write-through cache; on first
access for a sender the last _MAX_TURNS events are rehydrated from disk.
"""
import base64
import datetime
import json
import logging

import httpx

from . import config, tools, transcript
from .brief import build_brief
from .prompt import GUEST_SYSTEM, SYSTEM

log = logging.getLogger("voice")

# Tool schemas advertised to the model (OpenAI function-calling format).
TOOLS = [
    {"type": "function", "function": {
        "name": "brain_search",
        "description": f"Search the brain (notes) by text. Use before answering factual questions about {config.OWNER_NAME}'s world.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "brain_read",
        "description": "Read one note's full body by path, e.g. 'designs/brain-voice'.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "recent_activity",
        "description": "List recent changes in the brain.",
        "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "list_todos",
        "description": f"List {config.OWNER_NAME}'s todos. status open|done|all.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string"}, "priority": {"type": "string"}, "due_before": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "create_todo",
        "description": "Create an L6 todo. priority P0-P3. due is YYYY-MM-DD only if clearly stated. Always set a category - it picks the calendar colour.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"}, "priority": {"type": "string"},
            "due": {"type": "string", "description": "All-day deadline date YYYY-MM-DD (no specific time)."},
            "body": {"type": "string"},
            "category": {"type": "string", "enum": ["work", "chore", "social", "family", "health", "finance", "study", "deadline", "travel", "personal", "errand"]},
            "remind_min": {"type": "integer", "description": "Minutes before to notify. Night before ~9pm=180, day before 9am=900, a week=10080. Omit for default."},
            "start": {"type": "string", "description": "ISO start for a TIMED event, e.g. 2026-06-22T09:00:00. Use instead of due when a time is given."},
            "end": {"type": "string", "description": "ISO end for a timed event, e.g. 2026-06-22T12:00:00."},
            "all_day": {"type": "boolean", "description": "true for an all-day/multi-day event (start/end are dates)."},
            "rrule": {"type": "string", "description": "iCalendar recurrence, e.g. FREQ=WEEKLY;BYDAY=MO or FREQ=DAILY. Omit for one-off."},
            "location": {"type": "string"}},
            "required": ["title", "category"]}}},
    {"type": "function", "function": {
        "name": "pulse_note",
        "description": f"Append one short undated snippet of current context to the pulse page (short-term memory). Use when {config.OWNER_NAME} states a currently-true fact in passing ('working from the library this week', 'leaning toward X'). Dated/actionable things -> create_todo instead. Confirm you noted it.",
        "parameters": {"type": "object", "properties": {
            "snippet": {"type": "string", "description": "One concise sentence, no date."}},
            "required": ["snippet"]}}},
    {"type": "function", "function": {
        "name": "pulse_page",
        "description": "Re-read the pulse page (short-term memory + upcoming dated todos + service health). Your system prompt already has a snapshot; call this only if you need it refreshed mid-conversation.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "propose_commit",
        "description": "Open a durable-change plan for brain notes. Returns plan_id. Writes nothing yet.",
        "parameters": {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}}},
    {"type": "function", "function": {
        "name": "execute_commit",
        "description": f"Apply a proposed plan. ONLY after {config.OWNER_NAME} has said yes in chat.",
        "parameters": {"type": "object", "properties": {
            "plan_id": {"type": "string"}, "operations": {"type": "array", "items": {"type": "object"}}},
            "required": ["plan_id", "operations"]}}},
]

DISPATCH = {
    "brain_search": tools.brain_search,
    "brain_read": tools.brain_read,
    "recent_activity": tools.recent_activity,
    "list_todos": tools.list_todos,
    "create_todo": tools.create_todo,
    "pulse_note": tools.pulse_note,
    "pulse_page": tools.pulse_page,
    "propose_commit": tools.propose_commit,
    "execute_commit": tools.execute_commit,
}

# sender -> list[message]; in-memory only.
_HISTORY: dict[str, list] = {}
_MAX_TURNS = 20  # keep last N messages per sender


def _call_model(messages, use_tools=True):
    headers = {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "X-Title": "brain-voice",
    }
    if config.PUBLIC_URL:
        headers["HTTP-Referer"] = config.PUBLIC_URL
    payload = {"model": config.MODEL, "messages": messages}
    if use_tools:
        payload["tools"] = TOOLS
        payload["tool_choice"] = "auto"
    with httpx.Client(timeout=60) as client:
        r = client.post(config.OPENROUTER_URL, headers=headers, json=payload)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]


def _run_tool(name, args_json):
    fn = DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool {name}"}
    try:
        args = json.loads(args_json or "{}")
    except json.JSONDecodeError:
        return {"error": "bad tool arguments"}
    try:
        return fn(**args)
    except Exception as e:  # surface tool errors back to the model, don't crash
        return {"error": f"{type(e).__name__}: {e}"}


def _build_user_content(text, images):
    """Build the user message content field for a vision-capable turn.

    Returns a plain string when there are no images (cheap path), or an
    OpenAI-format list with text + base64 image_url parts. In history we store
    only the text + "[photo]" placeholder, never base64.
    """
    if not images:
        return text or ""
    parts = [{"type": "text", "text": text or "(photo)"}]
    for img_bytes, ct in images:
        b64 = base64.b64encode(img_bytes).decode()
        parts.append({"type": "image_url",
                       "image_url": {"url": f"data:{ct};base64,{b64}"}})
    return parts


def handle(sender: str, text: str, owner: bool = True, images=None) -> str:
    """Process one inbound message and return the reply text. Non-owner (guest)
    senders get a fun, brain-LESS conversation: no tools, no brief, no access to
    the owner's private notes/todos. images is an optional list of (bytes,
    content_type) pairs; only accepted for owner turns."""
    # Rehydrate from disk on first access so a restart doesn't lose context.
    if sender not in _HISTORY:
        _HISTORY[sender] = transcript.load_tail(sender, _MAX_TURNS)
    history = _HISTORY[sender]

    # Guests: drop any media, text only, keep surface contained.
    if not owner:
        images = None

    # Build the content (list for vision turns, plain str otherwise).
    content = _build_user_content(text, images)

    # Store in history WITHOUT the base64 payload: replace image parts with
    # a lightweight placeholder so later turns don't re-send image tokens.
    if isinstance(content, list):
        history_text = (text or "") + " [photo]"
    else:
        history_text = content
    history.append({"role": "user", "content": history_text})
    transcript.append(sender, "user", history_text)

    if owner:
        today = datetime.date.today().isoformat()
        dateline = (f"\n\nToday's date is {today}. When a date is given without a year "
                    "or month, assume the nearest FUTURE occurrence - never schedule in "
                    "the past. Always store dates as YYYY-MM-DD.\n")
        # Tell it what it is actually running on, so "what model are you?" has a
        # truthful answer available instead of a guess from training data.
        modelline = f"\nYou are currently running on the model: {config.MODEL}\n"
        system = (SYSTEM + dateline + modelline
                  + "\n## Right now (your short-term memory)\n" + build_brief())
    else:
        system = GUEST_SYSTEM
    history_slice = history[-_MAX_TURNS:]
    messages = [{"role": "system", "content": system}] + history_slice

    # If images were provided, the last history entry has a text placeholder.
    # Swap in the real vision content for the live model call only.
    if isinstance(content, list):
        messages[-1] = {"role": "user", "content": content}

    called_any = False
    for _ in range(config.MAX_TOOL_ITERATIONS):
        msg = _call_model(messages, use_tools=owner)
        messages.append(msg)
        calls = msg.get("tool_calls")
        if not calls:
            reply = msg.get("content") or "..."
            if owner and not called_any:
                # The model settled on an answer without touching a tool. Often
                # right (a question), sometimes a silent no-op on "remind me to
                # X". Logged so the second case is greppable.
                log.info("tool sender=%s none-called reply_len=%d", sender, len(reply))
            history.append({"role": "assistant", "content": reply})
            transcript.append(sender, "assistant", reply)
            return reply
        for call in calls:
            called_any = True
            name = call["function"]["name"]
            raw_args = call["function"].get("arguments")
            result = _run_tool(name, raw_args)
            # One journal line per tool call. Without this a turn where the
            # model answered "Done." having called nothing is indistinguishable
            # from one that wrote a todo -- which is how a week of missed
            # reminders went unnoticed.
            err = result.get("error") if isinstance(result, dict) else None
            log.info("tool sender=%s name=%s args=%s -> %s", sender, name,
                     str(raw_args)[:300], f"ERROR {err}" if err else "ok")
            messages.append({"role": "tool", "tool_call_id": call["id"],
                             "content": json.dumps(result, default=str)[:6000]})

    fallback = "Sorry, I got stuck working through that. Try rephrasing?"
    history.append({"role": "assistant", "content": fallback})
    transcript.append(sender, "assistant", fallback)
    return fallback
