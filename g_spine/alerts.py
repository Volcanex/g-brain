"""brain-alerts: the one alert spine.

Everything that must reach the owner's phone routes through here -> ntfy. ntfy is
the channel because it is independent of WhatsApp/Telnyx: a transport outage (or
an unpaid provider) still reaches the phone. WhatsApp can only message inside a
24h window, so it can never be the "something went down at 3am" channel.

One chokepoint, `send(level, source, msg, dedup_key=...)`, callable three ways:
  * in-process    `import brain_alerts; brain_alerts.send(...)` (crons, outlets,
                  brain_status). This is the primary path.
  * MCP tool      `brain_alert(...)`, mounted on the brain MCP (OAuth-gated).

There is deliberately NO REST route: the /api face is public and unauthenticated,
and an open "push to the phone" endpoint is a spam vector. Add one behind auth
only if a UI ever needs it.

Dedup/cooldown: a `dedup_key` pings once, then stays silent for COOLDOWN_HOURS so
a stuck failure can't ping every 5 minutes. Call `clear(dedup_key)` on recovery so
the next failure alerts immediately. Without this, "everything pushes" becomes
"everything is muted".

Levels map to ntfy priority but ALL push for now (low volume). The morning brief
carries NO tag so the notification has no emoji.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.request

from fastmcp import FastMCP

import g_config

BRAIN_ROOT = g_config.BRAIN_ROOT
STATE_DIR = BRAIN_ROOT / ".pulse" / "state"
ALERTS_STATE = STATE_DIR / "alerts.json"

# BRAIN_ALERTS_DRY_RUN=1: print what would be sent, touch neither ntfy nor state.
def dry_run() -> bool:
    return os.environ.get("BRAIN_ALERTS_DRY_RUN", "").lower() in ("1", "true", "yes")


COOLDOWN_HOURS = 6  # same dedup_key won't re-alert within this window

# Secret resolution lives in g_config: live env first, then the repo's .env files,
# anchored on the repo root so it is the same for every user and every unit.
env = g_config.env

# level -> (ntfy priority, ntfy tags). Empty tags = no emoji in the notification.
_LEVELS = {
    "critical": ("max", "rotating_light"),
    "error":    ("high", "rotating_light"),
    "warn":     ("high", "warning"),
    "info":     ("default", ""),
    "brief":    ("default", ""),
}


def load_state() -> dict:
    try:
        return json.loads(ALERTS_STATE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ALERTS_STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.rename(ALERTS_STATE)


# Old private names, kept for callers outside this module.
_env, _load_state, _save_state = env, load_state, save_state


def _within_cooldown(dedup_key: str) -> bool:
    state = load_state()
    last = state.get(dedup_key)
    if not last:
        return False
    try:
        age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(last)).total_seconds()
    except ValueError:
        return False
    return age < COOLDOWN_HOURS * 3600


def _stamp(dedup_key: str) -> None:
    state = load_state()
    state[dedup_key] = dt.datetime.now(dt.timezone.utc).isoformat()
    save_state(state)


def clear(dedup_key: str) -> None:
    """Forget a dedup_key so its next occurrence alerts immediately. Call on
    recovery (e.g. a sync that had been failing now succeeds)."""
    if not dedup_key:
        return
    state = load_state()
    if dedup_key in state:
        del state[dedup_key]
        save_state(state)


def send(level: str, source: str, msg: str, dedup_key: str | None = None,
         click: str | None = None) -> dict:
    """Route one alert to ntfy. Returns a small status dict (never raises).

    level:     critical|error|warn|info|brief (maps to ntfy priority/tags)
    source:    who's alerting, used as the notification title (e.g. "calendar-sync")
    msg:       the notification body
    dedup_key: if set, suppresses repeats within COOLDOWN_HOURS; clear() to reset
    click:     optional URL the phone opens when the notification is tapped
    """
    print(f"[brain-alert] {level} {source}: {msg}", flush=True)
    if dry_run():
        return {"ok": True, "dry_run": True}
    if dedup_key and _within_cooldown(dedup_key):
        # The line above prints for every attempt; this marks the ones that never
        # reached the phone, so the journal can be counted as pages after the fact.
        print(f"[brain-alert] suppressed by cooldown ({dedup_key})", flush=True)
        return {"ok": True, "suppressed": True}
    priority, tags = _LEVELS.get(level, ("default", ""))
    url = env("NTFY_URL")
    if not url:
        return {"ok": False, "error": "NTFY_URL unset"}
    headers = {"Title": source, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if click:
        headers["Click"] = click
    req = urllib.request.Request(url, data=msg.encode("utf-8"), headers=headers)
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[brain-alert] ntfy push failed: {e}", flush=True)
        return {"ok": False, "error": str(e)}
    if dedup_key:
        _stamp(dedup_key)
    return {"ok": True}


# --------------------------------------------------------------------- MCP

mcp = FastMCP("brain-alerts")


@mcp.tool()
def brain_alert(level: str, source: str, msg: str, dedup_key: str | None = None) -> dict:
    """Send an alert to the owner's phone via the ntfy spine, the channel for
    things they MUST see, independent of WhatsApp/Telnyx.

    Use for: a service or external dependency going down, a silent failure you
    caught, anything time-critical. NOT for routine status (that rides the
    morning brief). level is critical|error|warn|info. Pass a stable `dedup_key`
    (e.g. the service id) for anything that could fire repeatedly: it suppresses
    duplicates for a cooldown window so you don't spam the phone.
    """
    return send(level, source, msg, dedup_key=dedup_key)


# --------------------------------------------------------------------- CLI

def _cli(argv: list[str] | None = None) -> int:
    import argparse
    import sys
    ap = argparse.ArgumentParser(prog="alerts", description="Send a test alert via the spine.")
    ap.add_argument("--level", default="info")
    ap.add_argument("--source", default="cli")
    ap.add_argument("--dedup-key", default=None)
    ap.add_argument("--click", default=None, help="URL to open when the notification is tapped")
    ap.add_argument("msg", nargs="*", help="alert body; if omitted, read from stdin")
    args = ap.parse_args(argv)
    msg = " ".join(args.msg) if args.msg else sys.stdin.read().strip()
    if not msg:
        ap.error("no message given (pass as args or pipe via stdin)")
    result = send(args.level, args.source, msg, dedup_key=args.dedup_key, click=args.click)
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(_cli())
