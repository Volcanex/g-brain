"""inlets/gmail_apply: land a distil run's decisions in one command.

The distil model reads `gmail_fetch` output once, then hands over its decisions as
JSON instead of spending a turn per todo (every Codex turn re-sends ~20K tokens):

    {"todos": [{"source_id": "<message-id>", "title": "...", "priority": "P1", "due": "YYYY-MM-DD", "body": "..."}],
     "snapshot": "one paragraph: threads awaiting a reply, upcoming bills",
     "summary": "one line"}

Every todo is a calendar item and needs `due`. One without a date is not a
calendar item: the applier drops it and counts it as `undated`, rather than
letting the API refuse the whole run.

Reads the JSON from stdin. Creates each todo over the local REST API unless an open
todo has the same title, records the gmail inlet snapshot, and only then acks the
fetch cursor. Any failure exits 1 before the ack, so the next run sees the same mail.

Run:  .venv/bin/python -m inlets.gmail_apply < plan.json
"""
import json
import os
import sys
import urllib.request

API = os.environ.get("BRAIN_API", "http://127.0.0.1:8766/api")


def _call(path, payload=None, method=None):
    req = urllib.request.Request(
        API + path, data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"}, method=method)
    return json.load(urllib.request.urlopen(req, timeout=30))


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
    made = skipped = undated = 0
    for todo in todos:
        if not str(todo.get("due") or "").strip():
            undated += 1
            continue
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
        payload.update(title=title, requester="pulse.gmail.distil", composer="pulse.gmail.distil",
                       source=source, source_key=key)
        result = _call("/todos", payload)
        if "slug" not in result:
            raise RuntimeError(f"todo not created: {title}")
        seen_keys.add(key)
        open_titles[normalized] = result
        if result.get("deduped") or result.get("duplicate"):
            skipped += 1
        else:
            made += 1
    import brain_pulse
    brain_pulse.record_state("gmail", plan.get("snapshot") or "No threads awaiting a reply.", plan.get("summary"))
    from inlets import gmail_fetch
    sys.argv = ["gmail_fetch", "--ack"]
    gmail_fetch.main()
    apply.undated = undated
    return made, skipped


def main():
    try:
        made, skipped = apply(json.load(sys.stdin))
    except Exception as e:  # noqa: BLE001 - the model needs the reason, whatever it is
        print(f"FAILED, nothing acked: {e!r}")
        return 1
    undated = getattr(apply, "undated", 0)
    print(f"applied: {made} todo(s) created, {skipped} already open, {undated} undated dropped; "
          "snapshot recorded; acked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
