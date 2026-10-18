"""inlets/gmail_fetch: dumb reader behind the gmail pulse inlet.

Prints recent inbox mail as plain text for the distil cron to reason over.
All judgement (what's a commitment, what's noise) lives in the cron prompt.
This just fetches. IMAP with an app password (GMAIL_APP_PASSWORD in
outlets/.env) because the OAuth app is stuck unpublishable in testing status;
BODY.PEEK + readonly so nothing is ever marked read.

Tracks the last-seen UID in .pulse/state/gmail-inlet.json so each run emits
only new mail; --days N ignores the cursor and dumps the last N days.
A fetch only records `pending_uid`; the cursor moves when the caller runs
`--ack` after it has acted on the mail. A distil run that dies midway (a todo
write failed, the model was out of quota) therefore sees the same mail again
instead of silently losing it, which is how a flight booking vanished on 2026-10-04.

Run:  .venv/bin/python -m inlets.gmail_fetch [--days N] [--max N] | --ack | --check
"""
import argparse
import email
import email.header
import imaplib
import json
import os
import re
import urllib.request

import g_config

STATE = os.path.join(g_config.STATE_DIR, "gmail-inlet.json")
ENV_FILE = os.path.join(os.path.dirname(__file__), "..", "outlets", ".env")


def _env(key):
    if key in os.environ:
        return os.environ[key]
    for line in open(ENV_FILE):
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip()
    raise KeyError(key)


def _load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def _decode(raw):
    parts = email.header.decode_header(raw or "")
    return "".join(p.decode(c or "utf-8", "replace") if isinstance(p, bytes) else p
                   for p, c in parts)


def _clean(text):
    """What the distil model reads: no quoted replies, no tracking URLs, one line."""
    lines = []
    for line in text.splitlines():
        if line.lstrip().startswith(">"):
            continue
        if re.match(r"\s*(On .{5,120} wrote:|-{2,}\s*(Original|Forwarded) message)", line):
            break
        lines.append(line)
    text = re.sub(r"https?://\S+", "[link]", " ".join(lines))
    text = re.sub(r"(\[link\]\s*)+", "[link] ", text)
    return re.sub(r"\s+", " ", text).strip()


def _body_text(msg, limit=400):
    parts = list(msg.walk()) if msg.is_multipart() else [msg]
    part = (next((p for p in parts if p.get_content_type() == "text/plain"), None)
            or next((p for p in parts if p.get_content_type() == "text/html"), None))
    if part is None:
        return "(no text part)"
    try:
        text = part.get_payload(decode=True).decode(
            part.get_content_charset() or "utf-8", "replace")
    except Exception:
        return "(undecodable)"
    if part.get_content_type() == "text/html":
        text = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", text,
                      flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    return _clean(text)[:limit]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, help="ignore cursor, fetch last N days")
    ap.add_argument("--max", type=int, default=50, help="most recent N messages")
    ap.add_argument("--check", action="store_true",
                    help="gate mode: same output, but exit 3 when there is no new mail (the dispatcher then spends no tokens)")
    ap.add_argument("--ack", action="store_true", help="commit the cursor to the last fetch")
    args = ap.parse_args()

    if args.ack:
        state = _load_state()
        if state.get("pending_uid"):
            json.dump({"last_uid": state["pending_uid"]}, open(STATE, "w"))
        print("(acked)")
        return

    m = imaplib.IMAP4_SSL("imap.gmail.com")
    m.login(_env("GMAIL_ADDR"), _env("GMAIL_APP_PASSWORD"))
    m.select("INBOX", readonly=True)

    state = _load_state()

    if args.days:
        import datetime
        since = (datetime.date.today() -
                 datetime.timedelta(days=args.days)).strftime("%d-%b-%Y")
        typ, data = m.uid("search", None, f"(SINCE {since})")
    elif state.get("last_uid"):
        typ, data = m.uid("search", None, f"(UID {int(state['last_uid']) + 1}:*)")
    else:
        typ, data = m.uid("search", None, "(SINCE {})".format(
            __import__("datetime").date.today().strftime("%d-%b-%Y")))

    uids = [int(u) for u in data[0].split()]
    uids = [u for u in uids if not state.get("last_uid") or u > int(state["last_uid"])] \
        if not args.days else uids
    uids = sorted(uids)[-args.max:]

    if not uids:
        print("(no new mail)")
    for uid in uids:
        typ, msg_data = m.uid("fetch", str(uid), "(BODY.PEEK[])")
        msg = email.message_from_bytes(msg_data[0][1])
        print("=" * 8, f"uid {uid}")
        print("source_id:", str(msg.get("Message-ID") or uid).strip())
        print("From:", _decode(msg.get("From")))
        print("Date:", msg.get("Date"))
        print("Subject:", _decode(msg.get("Subject")))
        print("Body:", _body_text(msg))
        print()

    # The distiller dedupes against these by meaning; the applier only catches
    # exact titles, which is how one DistroKid bill became six todos.
    if args.check and uids:
        print("=" * 8, "open todos (never create a second todo for any of these)")
        try:
            todos = json.load(urllib.request.urlopen(
                os.environ.get("BRAIN_API", "http://127.0.0.1:8766/api") + "/todos?status=open", timeout=30))
            for t in todos:
                print("-", t["title"])
        except OSError as e:
            print(f"(unavailable: {e})")

    if uids and not args.days:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        json.dump({"last_uid": state.get("last_uid"), "pending_uid": max(uids)}, open(STATE, "w"))
    m.logout()
    if args.check and not uids:
        raise SystemExit(3)


if __name__ == "__main__":
    main()
