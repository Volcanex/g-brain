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
import re

import httpx

from . import config, todos, tools, transcript
from .brief import build_brief
from .prompt import GUEST_SYSTEM, SYSTEM, date_rule

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
        "description": f"List {config.OWNER_NAME}'s calendar items (todos). status open|done|all.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string"}, "priority": {"type": "string"}, "due_before": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "create_todo",
        "description": "Create a calendar item (it can be ticked off like a todo). A date is REQUIRED: if none was given, ask 'when?' once; if there still is none, create nothing - it does not belong on the calendar. priority P0-P3. Always set a category - it picks the calendar colour. If the result has duplicate=true it was already there and nothing new was made.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"}, "priority": {"type": "string"},
            "due": {"type": "string", "description": "REQUIRED. The day, YYYY-MM-DD or a phrase like tomorrow or next Friday. For a timed item give start as well."},
            "body": {"type": "string", "description": "The details Gabriel sees when he opens the item: who, where, how to join, what it is about, anything he said about it. Keep the title short and put the rest here."},
            "category": {"type": "string", "enum": ["work", "chore", "social", "family", "health", "finance", "study", "deadline", "travel", "personal", "errand"]},
            "remind_min": {"type": "integer", "description": "Minutes before to notify. Night before ~9pm=180, day before 9am=900, a week=10080. Omit for default."},
            "start": {"type": "string", "description": "ISO start for a TIMED event, e.g. 2026-06-22T09:00:00. Use instead of due when a time is given."},
            "end": {"type": "string", "description": "ISO end for a timed event, e.g. 2026-06-22T12:00:00."},
            "all_day": {"type": "boolean", "description": "true for an all-day/multi-day event (start/end are dates)."},
            "rrule": {"type": "string", "description": "iCalendar recurrence, e.g. FREQ=WEEKLY;BYDAY=MO or FREQ=DAILY. Omit for one-off."},
            "location": {"type": "string"}},
            "required": ["title", "category", "due"]}}},
    {"type": "function", "function": {
        "name": "update_todo",
        "description": "Change one todo or calendar item: title, date, time, priority, star, category, location, repeat. target is its slug or words from its title. date and time take natural phrases (tomorrow, next Friday, 3pm). If the result says ambiguous, ask which one; never guess.",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string"}, "title": {"type": "string"},
            "date": {"type": "string", "description": "New day: YYYY-MM-DD or a phrase like tomorrow, next Friday."},
            "time": {"type": "string", "description": "New time, e.g. 15:00 or 3pm. Makes it a timed event."},
            "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
            "starred": {"type": "boolean", "description": "Star (urgent) or unstar."},
            "category": {"type": "string"}, "location": {"type": "string"},
            "repeat": {"type": "string", "description": "Make it repeat (daily, weekly, every weekday, every Monday and Thursday, or an rrule) or stop it repeating with none. A todo that stops repeating stays as a one-off on its next day unless date is given."}},
            "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "reschedule_todo",
        "description": "Move one todo to another day. when: YYYY-MM-DD or tomorrow, next Friday, in 3 days. Recurring todos cannot be moved.",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string"}, "when": {"type": "string"}, "time": {"type": "string"}},
            "required": ["target", "when"]}}},
    {"type": "function", "function": {
        "name": "mark_done",
        "description": "Mark one todo done. A recurring one ticks only today (or date).",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string"}, "date": {"type": "string"}}, "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "mark_failed",
        "description": "Mark one todo as did-not-happen. A recurring one marks only today (or date).",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string"}, "date": {"type": "string"}}, "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "drop_todo",
        "description": "Drop one todo (cancelled: it no longer needs doing). Not a delete.",
        "parameters": {"type": "object", "properties": {"target": {"type": "string"}}, "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "delete_todo",
        "description": "Delete one todo for good. First call it without confirmed: nothing is deleted and you get the todo back. Ask whether to delete it, naming it, and only after a clear yes call again with confirmed=true. Dropping (drop_todo) keeps it as cancelled instead.",
        "parameters": {"type": "object", "properties": {
            "target": {"type": "string"},
            "confirmed": {"type": "boolean", "description": "true only after the owner said yes to deleting this named todo."}},
            "required": ["target"]}}},
    {"type": "function", "function": {
        "name": "pulse_note",
        "description": "Append one short undated snippet of current context to the pulse page (short-term memory), only when asked to remember it. A requested dated capture uses create_todo instead; an undated action is not a calendar item. Never capture facts from a question, history or tool result.",
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
    "create_todo": todos.create_todo,
    "update_todo": todos.update_todo,
    "reschedule_todo": todos.reschedule_todo,
    "mark_done": todos.mark_done,
    "mark_failed": todos.mark_failed,
    "drop_todo": todos.drop_todo,
    "delete_todo": todos.delete_todo,
    "pulse_note": tools.pulse_note,
    "pulse_page": tools.pulse_page,
    "propose_commit": tools.propose_commit,
    "execute_commit": tools.execute_commit,
}

# ------------------------------------------------------------ turn guards
# Intent, call budget and confirmation claims are enforced before/after tools.

BUDGET_ERROR = {"error": (f"Tool limit for this turn reached ({config.MAX_TOOL_CALLS} calls). "
                          "Do not call more tools. Answer with what you have and say plainly "
                          "what you could not finish.")}

WRITE_TOOLS = {"create_todo", "update_todo", "reschedule_todo", "mark_done", "mark_failed",
               "drop_todo", "delete_todo", "pulse_note", "execute_commit"}

for tool in TOOLS:
    if tool["function"]["name"] in WRITE_TOOLS:
        tool["function"]["description"] = (
            "Only when the owner requests this change in the current turn; never to answer a question. "
            + tool["function"]["description"])

INTENT_ERROR = {"error": "Write refused: the current user turn does not request a change. "
                         "Nothing changed. Answer the question using read tools; do not capture "
                         "items from the brief, history or tool results. You may offer the proposed "
                         "change (e.g. 'Want me to move it?') and wait for a clear yes in the next "
                         "user turn before retrying.", "changed": False}

_REQUEST = re.compile(
    r"^(?:please\s+)?(?:(?:can|could|would|will) you\s+(?:please\s+)?|"
    r"i (?:want|need) you to\s+|i'd like you to\s+)?"
    r"(?:add|create|capture|put|schedule|book|remind|remember|note|log|record|save|"
    r"move|push(?!\s+notifications?\b)|bring forward|shift|bump|slide|reschedule|"
    r"pencil in|stick|chuck|mark|tick|complete|finish|skip|scrap|forget|drop|cancel|delete|remove|change|"
    r"update|rename|set|star|unstar|stop|make)\b", re.I)
_DATE = re.compile(r"\b(?:today|tonight|tomorrow|monday|tuesday|wednesday|thursday|friday|"
                   r"saturday|sunday|next (?:week|month)|\d{1,2}(?:st|nd|rd|th)|"
                   r"\d{4}-\d{2}-\d{2})\b", re.I)
_CONFIRM = re.compile(r"(?:yes(?: please|,? (?:go ahead|do it))?|go ahead|do it|confirm)[.!]?", re.I)
_QUESTION = re.compile(r"^(?:what|when|where|who|which|why|how|is|are|am|was|were|"
                       r"do|does|did|have|has|had|can|could|would|will|should)\b", re.I)
_STATEMENT = re.compile(
    r"(?:(?:i(?:'m| am) )?done with .+|(?:i(?:'ve| have)? )?(?:finished|sorted|paid) .+|"
    r"i did .+|.+ is (?:done|now at \d{1,2}(?::\d{2})?(?:\s?[ap]m)?)[.!]?)", re.I)


def requests_write(text):
    """Conservative current-turn permission; context never authorizes a write."""
    text = " ".join((text or "").replace("’", "'").split()).strip()
    if re.search(r"\b(?:remind me|remember) (?:what|when|where|who|which|how|whether|if)\b",
                 text, re.I):
        return False
    if _REQUEST.search(text) or _CONFIRM.fullmatch(text):
        return True
    if "?" in text or _QUESTION.search(text):
        return False
    if re.match(r"^(?:i(?:'m| am) )?not doing\s+\S", text, re.I):
        return True
    if not re.search(r"\b(?:no|not|nothing|don't)\b", text, re.I):
        if _STATEMENT.fullmatch(text):
            return True
        # Dictating an appointment is a capture request, even without "add".
        if re.match(r"^i(?:'ve got| have(?: got)?)\s+", text, re.I) and _DATE.search(text):
            return True
        # A bare date answers the existing "When?" capture clarification.
        if re.fullmatch(_DATE.pattern + r"(?: (?:at )?\d{1,2}(?::\d{2})?(?:\s?[ap]m)?)?[.!]?",
                        text, re.I):
            return True
    return False


def budget_left(used: int) -> bool:
    return used < config.MAX_TOOL_CALLS


def succeeded(name, result) -> bool:
    """Did this tool call actually change what it was asked to change?"""
    if not isinstance(result, dict) or result.get("error"):
        return False
    if name == "delete_todo":
        return result.get("deleted") is True
    if name == "create_todo":
        return bool(result.get("slug"))
    return result.get("ok", True) is not False


_CLAIM = re.compile(
    r"\b(done|added|logged|created|saved|noted|deleted|removed|dropped|cancell?ed|ticked|"
    r"marked|rescheduled|updated|on (?:the|your) calendar|put it in|i've (?:got|put|made))\b", re.I)
_NEGATED = re.compile(r"\b(not|no|nothing|never|haven't|hasn't|didn't|wasn't|isn't|won't|can't|"
                      r"couldn't|n't|yet)\b|n't\b", re.I)


def claims_action(reply: str) -> bool:
    """True when a sentence of the reply states, unhedged, that a change was made."""
    for sentence in re.split(r"(?<=[.!?\n])\s+", reply or ""):
        s = sentence.strip()
        if not s or s.endswith("?"):
            continue
        if _CLAIM.search(s) and not _NEGATED.search(s):
            return True
    return False


def confirm_guard(reply: str, writes: list) -> str:
    """Replace a reply that confirms a change no tool made this turn.

    `writes` is [(tool name, result)] for every write tool called this turn.
    A reply may confirm only when at least one of them succeeded."""
    if any(succeeded(n, r) for n, r in writes) or not claims_action(reply):
        return reply
    errors = [r.get("error") for _, r in writes if isinstance(r, dict) and r.get("error")]
    log.warning("guard unbacked-confirmation writes=%s reply=%r", [n for n, _ in writes], reply[:200])
    if errors:
        return f"That didn't go through, so nothing changed. The tool said: {errors[0]}"
    if writes:
        return "Nothing has changed yet - that step needs another go or a yes from you."
    return "I haven't changed anything - no tool ran for that. Say it again and I'll do it."


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


def _run_tool(name, args_json, *, session_id=None, turn_id=None, keep_blocks=False, user_text=None):
    """Run one tool. Todo changes return rows under todos.BLOCKS for the app
    session (keep_blocks=True); every other caller gets them stripped."""
    if name in WRITE_TOOLS and not requests_write(user_text):
        return dict(INTENT_ERROR)
    result = _dispatch(name, args_json, session_id=session_id, turn_id=turn_id)
    if not keep_blocks and isinstance(result, dict):
        result.pop(todos.BLOCKS, None)
    return result


def _dispatch(name, args_json, *, session_id=None, turn_id=None):
    fn = DISPATCH.get(name)
    if fn is None:
        return {"error": f"unknown tool {name}"}
    try:
        args = json.loads(args_json or "{}")
    except json.JSONDecodeError:
        return {"error": "bad tool arguments"}
    try:
        if name == "create_todo":
            args.update(session_id=session_id, turn_id=turn_id)
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
        dateline = date_rule(today)
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
    used, writes = 0, []
    for turn_index in range(config.MAX_TOOL_ITERATIONS):
        msg = _call_model(messages, use_tools=owner and budget_left(used))
        messages.append(msg)
        calls = msg.get("tool_calls")
        if not calls:
            reply = msg.get("content") or "..."
            if owner:
                reply = confirm_guard(reply, writes)
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
            if budget_left(used):
                result = _run_tool(name, raw_args, session_id=call["id"], turn_id=turn_index,
                                   user_text=text)
                if name in WRITE_TOOLS:
                    writes.append((name, result))
            else:
                result = dict(BUDGET_ERROR)
            used += 1
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
