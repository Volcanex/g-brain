"""outlets/remind: ntfy reminders for dated todos, on a proportional ladder.

Todos are the SSOT, so anything that creates one (WhatsApp bot, agents, MCP)
gets reminders with no extra wiring. This outlet is the whole reminder system
now that the Google Calendar mirror is on its way out: ntfy is the only surface
a ping has to reach.

**The ladder.** A reminder is only useful at a distance proportional to the
thing. Three weeks before a flight you can still fix your passport; three weeks
before the dishwasher is noise. So each todo gets a *list* of lead times chosen
by `category` (LADDERS), not the single `remind_min` the old version had:

    chore      1h, 5min          travel   3w, 1w, 1d
    family     3d, 1d            study    4w, 2w, 1w, 3d, 1d
    finance    3d, 1d            errand   1d, 2h

A recurring finance todo (a subscription charge) gets only the 1d rung.

`remind_min`, if a todo sets one, still wins: it is an explicit instruction.

**Proportionality.** A rung whose moment is already past when the todo is made
never fires: booking Sweden two weeks out should not fire the 3-week rung on
day one. The exception is a todo created past *every* rung, which keeps its
last one: that case is a same-day reminder, and dropping it silently is the
bug this outlet was rewritten around in Sep 2026.

**No bursts.** Day-scale rungs on all-day events snap to the morning rather
than to midnight, and each todo gets a deterministic 0-49min offset from its
slug, so ten things due tomorrow do not all land at 09:00 on the dot. Anything
sent late is rolled into one digest ping instead of one ping per rung.

Run a pass:  .venv/bin/python -m outlets.remind
"""
import datetime
import hashlib
import json
import os
from zoneinfo import ZoneInfo

import brain_alerts as alerts
import brain_todos
import g_config
from brain_todos import expand_rrule, ladder_for, rrule_interval_minutes

TZ = ZoneInfo("Europe/London")
STATE = os.path.join(g_config.STATE_DIR, "reminders.json")

HOUR = 60
DAY = 24 * HOUR
WEEK = 7 * DAY

# Day-scale rungs on an all-day event fire in the morning, not at midnight.
MORNING_HOUR = 9
SNAP_THRESHOLD = 12 * HOUR
# Nothing wakes the owner up. A rung that computes to the small hours fires at
# QUIET_END instead, unless the thing it is warning about happens first, in
# which case the warning is already useless and is better sent than buried.
QUIET_START = 23
QUIET_END = 7
SPREAD_MINUTES = 50
GRACE_HOURS = 6
# How long a finished recurring occurrence stays in the sent-state file.
OCCURRENCE_MEMORY_DAYS = 7
# How far ahead to expand a recurrence. One pass only needs to see occurrences
# whose longest rung has come due, plus a day of slack.
RECUR_HORIZON_DAYS = 40


def _load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE)


def _parse_dt(raw):
    try:
        t = datetime.datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return t.replace(tzinfo=TZ) if t.tzinfo is None else t.astimezone(TZ)


def _parse_date(raw):
    try:
        return datetime.date.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def _spread(slug):
    """A stable 0-49 minute offset per todo, so simultaneous rungs fan out."""
    h = hashlib.sha1(slug.encode("utf-8")).digest()
    return h[0] % SPREAD_MINUTES


# ------------------------------------------------------------- recurrence

# ----------------------------------------------------------- the moments

def _moments(todo, now):
    """Yield (occurrence_key, moment, all_day, stale_after, interval) for one todo.

    A todo is anchored on `start` if it has one, otherwise on `due`. `due`
    alone is an all-day thing: its moment is the start of the day and it stops
    mattering at the end of it.
    """
    start = _parse_dt(todo.get("start")) if todo.get("start") else None
    all_day = bool(todo.get("all_day"))
    if start and start.hour == 0 and start.minute == 0 and not todo.get("end"):
        all_day = True

    if start and todo.get("rrule"):
        horizon = now + datetime.timedelta(days=RECUR_HORIZON_DAYS)
        gap = rrule_interval_minutes(todo["rrule"])
        for occ in expand_rrule(start, todo["rrule"], horizon):
            stale = (occ.replace(hour=23, minute=59) if all_day else occ)
            if stale < now - datetime.timedelta(days=1):
                continue
            yield (occ.date().isoformat(), occ, all_day, stale, gap)
        return

    if start:
        stale = (start.replace(hour=23, minute=59) if all_day else start)
        yield ("once", start, all_day, stale, None)
        return

    d = _parse_date(todo.get("due"))
    if d:
        moment = datetime.datetime.combine(d, datetime.time(0, 0), TZ)
        yield ("once", moment, True,
               datetime.datetime.combine(d, datetime.time(23, 59), TZ), None)


def _fire_at(moment, rung, all_day, slug):
    """An all-day thing has no time, so its rungs land in the morning of the
    day they point at rather than at the midnight the date technically means.
    Day-scale rungs also carry a per-todo offset so simultaneous ones fan out."""
    t = moment - datetime.timedelta(minutes=rung)
    if all_day:
        t = t.replace(hour=MORNING_HOUR, minute=0)
    if all_day or rung >= DAY:
        t += datetime.timedelta(minutes=_spread(slug))
    if t.hour >= QUIET_START or t.hour < QUIET_END:
        morning = t.replace(hour=QUIET_END, minute=30, second=0, microsecond=0)
        if t.hour >= QUIET_START:
            morning += datetime.timedelta(days=1)
        t = min(morning, moment) if morning > moment else morning
    return t


def _rungs(todo, moment, all_day, interval_min, created_at, slug):
    """The ladder for one moment, as fire times.

    Every trim lives in brain_todos.ladder_for, the creation-lead one included,
    so the calendar page and this sender always agree about what will fire.
    """
    lead = None if created_at is None else (moment - created_at).total_seconds() / 60
    ladder = ladder_for(todo, all_day, interval_min, lead)
    # Two rungs can land on the same minute once snapping and quiet hours have
    # moved them, and two identical pings read as a bug. Keep the longer rung,
    # whose wording ("in 2 days") is the one that matches the moment it fires.
    out, seen = [], set()
    for rung in ladder:
        at = _fire_at(moment, rung, all_day, slug)
        if at in seen:
            continue
        seen.add(at)
        out.append((rung, at))
    return out


def _lead_phrase(rung, all_day=False):
    if rung <= 0:
        return "Today" if all_day else "Now"
    if rung < HOUR:
        return f"In {rung} min"
    if rung < DAY:
        h = round(rung / HOUR)
        return f"In {h} hour" + ("s" if h != 1 else "")
    if rung < WEEK:
        d = round(rung / DAY)
        return "Tomorrow" if d == 1 else f"In {d} days"
    w = round(rung / WEEK)
    return f"In {w} week" + ("s" if w != 1 else "")


def _message(todo, rung, moment, all_day):
    title = todo["title"]
    if all_day:
        when = ""
    elif rung < HOUR:
        when = moment.strftime(" (%H:%M)")
    else:
        when = moment.strftime(" (%H:%M %a %d %b)")
    cat = todo.get("category")
    return (f"{_lead_phrase(rung, all_day)}: {title}{when}"
            + (f" [{cat}]" if cat else ""))


def run():
    now = datetime.datetime.now(TZ)
    state = _load_state()
    open_todos = {t["permalink"]: t for t in brain_todos.list_todos(status="open")}
    sent = 0
    late_digest = []

    for permalink, todo in open_todos.items():
        rec = state.setdefault(permalink, {})
        slug = permalink.rsplit("/", 1)[-1]
        created_at = None
        cd = _parse_date(todo.get("created"))
        if cd:
            created_at = datetime.datetime.combine(cd, datetime.time(0, 0), TZ)
        done_dates = set(todo.get("done_dates") or [])
        for occ_key, moment, all_day, stale_after, gap in _moments(todo, now):
            # Already ticked off for that day: a nudge about a walk you have
            # been on is the noise that gets a channel muted.
            if occ_key in done_dates:
                continue
            for rung, fire_at in _rungs(todo, moment, all_day, gap, created_at, slug):
                key = f"{occ_key}|{rung}"
                if key in rec or fire_at > now:
                    continue
                if (now - fire_at).total_seconds() > GRACE_HOURS * 3600:
                    if now >= stale_after:
                        rec[key] = "skipped-stale"
                        continue
                    late_digest.append((permalink, key,
                                        _message(todo, rung, moment, all_day)))
                    continue
                # The last rung is the reaction rung: the minimum warning that
                # still leaves time to act. On a vital todo that one goes out
                # at max priority rather than joining the day's quiet pings,
                # because it is the last chance to catch it.
                last = rung == min(r for r, _ in _rungs(todo, moment, all_day,
                                                        gap, created_at, slug))
                vital_now = bool(todo.get("vital")) and last
                r = alerts.send("critical" if vital_now else "info",
                                "Vital reminder" if vital_now else "Reminder",
                                ("VITAL: " if vital_now else "")
                                + _message(todo, rung, moment, all_day))
                if r.get("ok"):
                    rec[key] = now.isoformat()
                    sent += 1

    if late_digest:
        lines = sorted({m for _, _, m in late_digest})
        body = "\n".join(lines[:12])
        if len(lines) > 12:
            body += f"\n... and {len(lines) - 12} more"
        r = alerts.send("info", f"Catching up ({len(lines)})", body)
        if r.get("ok"):
            for permalink, key, _ in late_digest:
                state.setdefault(permalink, {})[key] = now.isoformat() + " (late)"
            sent += 1

    _prune(state, open_todos, now)
    _save_state(state)
    return sent


def _prune(state, open_todos, now):
    """Forget closed todos, and old occurrences of recurring ones, so the
    state file tracks the live list rather than growing forever."""
    cutoff = (now - datetime.timedelta(days=OCCURRENCE_MEMORY_DAYS)).date().isoformat()
    for gone in [p for p in state if p not in open_todos]:
        del state[gone]
    for rec in state.values():
        for key in [k for k in rec if k.split("|")[0] < cutoff
                    and k.split("|")[0] != "once"]:
            del rec[key]


if __name__ == "__main__":
    print("remind: sent", run())
