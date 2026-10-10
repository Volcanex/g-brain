"""inlets/gmail_apply: land a distil run's decisions in one command.

The distil model reads `gmail_fetch` output once, then hands over its decisions as
JSON instead of spending a turn per todo (every Codex turn re-sends ~20K tokens):

    {"todos": [{"title": "...", "priority": "P1", "due": "YYYY-MM-DD", "body": "..."}],
     "snapshot": "one paragraph: threads awaiting a reply, upcoming bills",
     "summary": "one line"}

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


def _call(path, payload=None):
    req = urllib.request.Request(
        API + path, data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def apply(plan):
    open_titles = {t["title"].strip().lower() for t in _call("/todos?status=open")}
    made = skipped = 0
    for todo in plan.get("todos", []):
        if todo["title"].strip().lower() in open_titles:
            skipped += 1
            continue
        body = {k: todo[k] for k in ("title", "priority", "due", "body") if todo.get(k)}
        body["body"] = (body.get("body", "") + "\n\nsource: pulse/gmail").strip()
        if "slug" not in _call("/todos", body):
            raise RuntimeError(f"todo not created: {todo['title']}")
        made += 1
    import brain_pulse
    brain_pulse.record_state("gmail", plan.get("snapshot") or "No threads awaiting a reply.", plan.get("summary"))
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
    print(f"applied: {made} todo(s) created, {skipped} already open; snapshot recorded; acked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
