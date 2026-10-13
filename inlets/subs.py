"""inlets/subs: recurring charges from the ledger become dated todos.

`gfin subs` knows what recurs and when it next bills. This turns each of those
into a todo, and then does nothing else: the reminder ladder gives a recurring
`finance` todo one warning, the day before, which is the notice needed to get
money into the account before a charge lands. Building a second alerting
path for subscriptions would have meant a second thing to keep working.

Idempotent by slug (`sub-<id>`), so a run rewrites the same todos rather
than accumulating. A subscription marked `cancel` in the registry gets a
one-off P1 to go and cancel it instead of a recurring charge reminder.

A subscription the ledger has never seen gets no todo at all. `gfin subs`
returns no date for those on purpose, and a reminder on a guessed billing day
is worse than none: it trains you to ignore the channel.

A subscription that missed its last charge is `lapsed`, usually because
Gabriel let it fail on purpose. Its todo is set `expired` (sub todos carry no
`expires`, so nothing else sets that), and it reopens by itself if the
charges come back; done and
cancelled are the owner's and stay closed.

Run a pass:  .venv/bin/python -m inlets.subs
"""
import json
import subprocess

import brain_todos

GFIN = "/usr/local/bin/gfin"
RRULE = {"monthly": "FREQ=MONTHLY", "quarterly": "FREQ=MONTHLY;INTERVAL=3",
         "annual": "FREQ=YEARLY"}


def _fetch():
    out = subprocess.run([GFIN, "subs", "--json"], capture_output=True,
                         text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(f"gfin subs failed: {out.stderr.strip()[:200]}")
    return json.loads(out.stdout)


def _upsert(slug, fields, body):
    """Write a todo at a known slug, leaving a done/cancelled one alone.

    Reopening something the owner has closed is the one behaviour that would make
    this inlet a nuisance rather than a tool, so status is never written back
    to open here.
    """
    try:
        post = brain_todos._load(slug)
        status = str(post.metadata.get("status") or "open")
        if status == "expired":
            post["status"] = "open"
            action = "reopened"
        elif status != "open":
            return "skipped-closed"
        else:
            action = "updated"
    except FileNotFoundError:
        import frontmatter
        post = frontmatter.Post(content="")
        post["kind"] = "todo"
        post["permalink"] = f"brain/todos/{slug}"
        post["status"] = "open"
        post["created"] = brain_todos._today()
        action = "created"
    for k, v in fields.items():
        if v is None:
            post.metadata.pop(k, None)
        else:
            post[k] = v
    post.content = body
    brain_todos._save(slug, post)
    return action


def run():
    rows = _fetch()
    counts = {}
    for sub in rows:
        slug = f"sub-{sub['id']}"
        amount = f"£{sub['amount']:.2f}" if sub["amount"] is not None else "amount unknown"

        if sub.get("lapsed"):
            counts["lapsed"] = counts.get("lapsed", 0) + 1
            try:
                post = brain_todos._load(slug)
            except FileNotFoundError:
                continue
            if str(post.metadata.get("status") or "open") == "open":
                post["status"] = "expired"
                post.content = (f"Lapsed: no charge since {sub['last_charged']}, so no "
                                f"reminder. Reopens if the charges come back.\n")
                brain_todos._save(slug, post)
            continue

        if sub["status"] == "cancel":
            action = _upsert(slug, {
                "title": f"Cancel {sub['label']} ({amount} a month)",
                "summary": f"Cancel {sub['label']}",
                "priority": "P1",
                "category": "finance",
                "due": sub["next_due"],
                "start": None, "end": None, "rrule": None, "all_day": True,
                "expires": None,
            }, f"""Marked for cancelling, not for funding. Last charge {amount} on
{sub['last_charged']}; the next one lands about {sub['next_due']} if it is
still live, which is the date this is set against so the warning arrives
before the money does.

Close this once the subscription is actually gone. The next `gfin subs` run
will keep reporting it until the charges stop appearing in the ledger.
""")
            counts[action] = counts.get(action, 0) + 1
            continue

        if not sub["seen"] or not sub["next_due"]:
            counts["no-date"] = counts.get("no-date", 0) + 1
            continue

        action = _upsert(slug, {
            "title": f"{sub['label']} {amount}",
            "summary": f"{sub['label']} {amount}",
            "priority": "P2",
            "category": "finance",
            "start": f"{sub['next_due']}T09:00:00",
            "rrule": RRULE[sub["cycle"]],
            "due": None, "all_day": None, "expires": None,
        }, f"""Recurring {sub['cycle']} charge, tracked by `gfin subs` and kept in step
with the ledger: the date corrects itself each time a real charge lands, so it
is only ever wrong until the next one.

Last seen {amount} on {sub['last_charged']}, across {sub['charges']} charge(s)
in the ledger. The finance ladder warns once, the day before, which is the
notice needed to get money into the account first.
""")
        counts[action] = counts.get(action, 0) + 1
    return counts


if __name__ == "__main__":
    print("subs:", run())
