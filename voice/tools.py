"""Brain tools exposed to the Voice's reasoning loop.

Thin wrappers over g-brain functions, imported in-process. The Voice runs on
h beside the brain code, so it calls the functions directly rather than through
the OAuth-gated MCP face. This is the same codepath the MCP tools use.

Contract (mirrors the pulse): read the cortex freely; write L6 todos directly;
durable note changes go through propose_commit -> (human says yes) -> execute_commit.
The Voice must never execute a durable commit without an explicit in-chat 'yes'.
"""
from uuid import uuid4

import brain_reader
import brain_todos
import brain_commit
import brain_pulse


def brain_search(query, limit=8):
    """Plain text search over note titles + bodies. MVP: semantic search via
    basic-memory is a later upgrade. Returns lightweight hits."""
    q = (query or "").lower().strip()
    if not q:
        return []
    hits = []
    for n in brain_reader.all_notes():
        if n.kind == "index" or n.path.startswith("indexes/"):
            continue  # auto-generated navigation, noise in a chat
        hay = f"{n.title}\n{n.body}".lower()
        if q in hay:
            idx = hay.find(q)
            start = max(0, idx - 80)
            snippet = n.body[start:start + 240].replace("\n", " ").strip()
            hits.append({"path": n.path, "title": n.title, "kind": n.kind, "snippet": snippet})
            if len(hits) >= limit:
                break
    return hits


def brain_read(path):
    """Read one note's full body by path (e.g. 'designs/brain-voice')."""
    n = brain_reader.get_note(path)
    if n is None:
        return {"error": f"no note at '{path}'"}
    return {"path": n.path, "title": n.title, "kind": n.kind, "status": n.status,
            "version": n.version, "tags": n.tags, "body": n.body}


def recent_activity(limit=10):
    """What changed in the brain recently."""
    return brain_reader.recent_activity(limit=limit)


def list_todos(status="open", priority=None, due_before=None):
    return brain_todos.list_todos(status=status, priority=priority, due_before=due_before)


def create_todo(title, priority="P2", due=None, body=None, relates_to=None, category=None, remind_min=None,
                start=None, end=None, all_day=None, rrule=None, location=None,
                session_id=None, turn_id=None):
    """Record an owner request with an originating voice event id."""
    if session_id is not None and turn_id is not None:
        event_id = f"{session_id}:{turn_id}"
    else:
        event_id = turn_id or session_id or str(uuid4())
    return brain_todos.create_todo(title=title, priority=priority, due=due,
                                   requester="gabriel", composer="agent", source=f"voice:{event_id}",
                                   relates_to=relates_to, body=body, category=category, remind_min=remind_min,
                                   start=start, end=end, all_day=all_day, rrule=rrule, location=location)


def pulse_note(snippet):
    """Append one undated current-context snippet to the pulse page."""
    n = brain_pulse.append_page(snippet)
    out = {"ok": True, "snippets": n}
    if n > brain_pulse.PAGE_SOFT_CAP:
        out["warning"] = f"page holds {n} snippets, compaction overdue"
    return out


def pulse_page():
    """The composed pulse payload (page + dated todos + health), refreshed."""
    return brain_pulse.composed_payload()


def propose_commit(summary):
    """Open a durable-change plan. Returns a plan_id. Does NOT write anything yet."""
    return brain_commit.propose_commit(summary=summary, mode="review")


def execute_commit(plan_id, operations):
    """Apply a proposed plan. ONLY call after the user has said yes in chat.
    actor='human' because a person approved it conversationally."""
    return brain_commit.execute_commit(plan_id=plan_id, operations=operations, actor="human")
