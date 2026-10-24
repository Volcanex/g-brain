"""inlets/gmail_apply: land a distil run's decisions in one command.

The distil model reads `gmail_fetch` output once, then hands over its decisions as
JSON instead of spending a turn per todo (every Codex turn re-sends ~20K tokens):

    {"todos": [{"source_id": "<message-id>", "title": "...", "priority": "P1", "due": "YYYY-MM-DD", "body": "..."}],
     "snapshot": "one paragraph: threads awaiting a reply, upcoming bills",
     "summary": "one line"}

Reads the JSON from stdin. Creates each todo over the local REST API unless an open
todo has the same title, records the gmail inlet snapshot, and only then acks the
fetch cursor. Any failure exits 1 before the ack, so the next run sees the same mail.

A todo the owner-action boundary refuses (HTTP 400) is a decision, not a failure.
Project and agent work becomes a ticket, a machine alert is dropped (its place is the
alert spine, or nowhere), and the snapshot names what was not made a todo. Treating a
refusal as a failure wedged the inlet: the same mail came back every hour and was
refused again, so the cursor never moved and nothing after it was read.

Run:  .venv/bin/python -m inlets.gmail_apply < plan.json
"""
import json
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("BRAIN_API", "http://127.0.0.1:8766/api")
# The boundary refuses a body over this from any requester but the owner. Trimming
# here keeps a long mail from turning a real commitment into a refusal.
BODY_LIMIT = 800


class Refused(Exception):
    """The API answered 400: the request is wrong, so sending it again cannot work."""


def _detail(error):
    try:
        body = error.read().decode("utf-8", "replace")
    except OSError:
        return str(error)
    try:
        return str(json.loads(body).get("detail") or body)
    except (ValueError, AttributeError):
        return body or str(error)


def _call(path, payload=None, method=None):
    req = urllib.request.Request(
        API + path, data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"}, method=method)
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        detail = _detail(e)
        if e.code == 400:
            raise Refused(detail) from None
        raise RuntimeError(f"HTTP {e.code} from {path}: {detail}") from None


def _refuse(title, body, source, key, reason):
    """Route one refused todo. Returns the line the snapshot carries for it."""
    if "create_ticket" in reason:
        try:
            ticket = _call("/tickets", {
                "title": title, "body": body or None, "requester": "pulse.gmail.distil",
                "composer": "pulse.gmail.distil", "source": source, "source_key": key})
        except Refused as e:
            return f"{title}: {reason}, and the ticket was refused too: {e}"
        return f"{title}: project or agent work, kept as ticket {ticket.get('slug', '')}".strip()
    return f"{title}: {reason}"


def apply(plan):
    from brain_todos import normalize_text

    todos = plan.get("todos", [])
    if any(not str(todo.get("source_id") or "").strip() for todo in todos):
        raise ValueError("Every todo needs source_id from the mail's Message-ID or IMAP UID")
    existing = _call("/todos?status=all")
    open_titles = {normalize_text(t["title"], one_line=True).casefold(): t
                   for t in existing if t.get("status", "open") == "open"}
    seen_keys = {key for todo in existing
                 for key in [todo.get("source_key"), *(todo.get("source_keys") or [])] if key}
    ordinals = {}
    refused = []
    made = skipped = 0
    for todo in todos:
        source_id = str(todo["source_id"]).strip()
        ordinals[source_id] = ordinals.get(source_id, 0) + 1
        source = f"gmail:{source_id}"
        key = f"{source}#{ordinals[source_id]}"
        title = normalize_text(todo["title"], one_line=True)
        normalized = title.casefold()
        if key in seen_keys:
            skipped += 1
            continue
        if normalized in open_titles:
            survivor = open_titles[normalized]
            _call("/todos/" + survivor["slug"], {"source_keys": [key]}, method="PATCH")
            seen_keys.add(key)
            skipped += 1
            continue
        payload = {k: todo[k] for k in ("priority", "due", "body") if todo.get(k)}
        if len(payload.get("body", "")) > BODY_LIMIT:
            payload["body"] = normalize_text(payload["body"])[:BODY_LIMIT].rstrip()
        payload.update(title=title, requester="pulse.gmail.distil", composer="pulse.gmail.distil",
                       source=source, source_key=key)
        try:
            result = _call("/todos", payload)
        except Refused as e:
            line = _refuse(title, normalize_text(payload.get("body"), one_line=True)[:BODY_LIMIT],
                           source, key, str(e))
            print(f"refused: {line}")
            refused.append(line)
            seen_keys.add(key)
            skipped += 1
            continue
        if "slug" not in result:
            raise RuntimeError(f"todo not created: {title}")
        seen_keys.add(key)
        open_titles[normalized] = result
        if result.get("deduped"):
            skipped += 1
        else:
            made += 1
    snapshot = plan.get("snapshot") or "No threads awaiting a reply."
    if refused:
        snapshot += "\n\nNot made into todos:\n" + "\n".join(f"- {line}" for line in refused)
    import brain_pulse
    brain_pulse.record_state("gmail", snapshot, plan.get("summary"))
    from inlets import gmail_fetch
    sys.argv = ["gmail_fetch", "--ack"]
    gmail_fetch.main()
    return made, skipped


def main():
    try:
        made, skipped = apply(json.load(sys.stdin))
    except Exception as e:  # noqa: BLE001 - the model needs the reason, whatever it is
        print(f"FAILED, nothing acked: {e!r}")
        return 1
    print(f"applied: {made} todo(s) created, {skipped} already open or refused; snapshot recorded; acked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
