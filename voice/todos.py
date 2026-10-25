"""Changing Gabriel's todos and calendar from chat.

The calendar IS the dated brain todos, so one set of tools covers both. Every
write goes through `brain_todos.update`, the function behind the brain's REST
`PATCH /api/todos/<slug>` that the dash panels and the web Day view reach through
G-Command's /api/brain proxy. Voice calls it in-process, like `list_todos`; there
is no second path. The field meanings are the Day view's: star = `urgent`, drop =
`status: cancelled`, did-not-happen = `failed` (a `failed_date` on a recurring
todo), done on a recurring todo ticks one date (`done_date`).

Rules this module enforces in code, not only in the prompt:
- A target names exactly one todo or nothing changes. Several matches come back
  as `candidates` (and a rows block of them) for the model to ask about.
- `delete_todo` removes a todo only when called with `confirmed=True`, which the
  model may send only after Gabriel has said yes to deleting that named todo.
  Unconfirmed, it writes nothing and returns the todo as a row with a Delete action.
- Every change returns the updated todo as a rows block under `_blocks`, which
  the app session draws under the reply (WhatsApp and talk just drop it).

Dates: natural phrases ("tomorrow", "next Friday", "in 3 days") resolve against
today in Europe/London, which is how the brain reads its dates.
"""
import datetime as dt
import re
from urllib.parse import quote
from zoneinfo import ZoneInfo

import brain_todos

TZ = ZoneInfo("Europe/London")
BLOCKS = "_blocks"   # result key appchat lifts into the reply's blocks
CAT_TONES = {        # g-command-web core/panels/calendar.py, so rows match the dash
    "study": "chart-1", "deadline": "chart-1", "finance": "chart-2", "bill": "chart-2",
    "family": "chart-3", "social": "chart-3", "health": "chart-4", "chore": "chart-4",
    "work": "chart-5", "travel": "chart-6", "errand": "chart-6", "personal": "chart-6",
}
_DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def today():
    return dt.datetime.now(TZ).date()


# ------------------------------------------------------------------ dates

def resolve_date(phrase, base=None):
    """A date phrase -> datetime.date, or None if it is not one we understand.
    'next Friday' and bare 'Friday' both mean the first Friday after today."""
    if not phrase:
        return None
    base = base or today()
    s = phrase.strip().lower().replace(",", " ")
    s = re.sub(r"\s+", " ", s)
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        pass
    if s in ("today", "tonight"):
        return base
    if s == "tomorrow":
        return base + dt.timedelta(days=1)
    if s in ("day after tomorrow", "the day after tomorrow"):
        return base + dt.timedelta(days=2)
    if s == "next week":
        return base + dt.timedelta(days=7)
    m = re.fullmatch(r"in (\d+|a|an|one|two|three) (day|days|week|weeks)", s)
    if m:
        n = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3}.get(m.group(1)) or int(m.group(1))
        return base + dt.timedelta(days=n * (7 if m.group(2).startswith("week") else 1))
    m = re.fullmatch(r"(?:next |this |on )?([a-z]+)", s)
    names = {d: i for i, d in enumerate(_DAYS)} | {d[:3]: i for i, d in enumerate(_DAYS)}
    if m and m.group(1) in names:
        return base + dt.timedelta(days=(names[m.group(1)] - base.weekday() - 1) % 7 + 1)
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)? (?:of )?([a-z]+)(?: (\d{4}))?", s) \
        or re.fullmatch(r"([a-z]+) (\d{1,2})(?:st|nd|rd|th)?(?: (\d{4}))?", s)
    if m:
        a, b, year = m.groups()
        day_s, mon_s = (a, b) if a.isdigit() else (b, a)
        if mon_s[:3] in _MONTHS:
            mon = _MONTHS.index(mon_s[:3]) + 1
            try:
                d = dt.date(int(year) if year else base.year, mon, int(day_s))
            except ValueError:
                return None
            if not year and d < base:   # nearest future occurrence
                d = d.replace(year=d.year + 1)
            return d
    return None


def resolve_time(phrase):
    """'15:00', '3pm', '9.30am', 'noon' -> 'HH:MM:00', or None."""
    if not phrase:
        return None
    s = phrase.strip().lower().replace(" ", "")
    if s in ("noon", "midday"):
        return "12:00:00"
    m = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?(am|pm)?", s)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if not m.group(2) and not ap:
        return None   # a bare "3" is not a time
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}:00"


# ------------------------------------------------------------------ targets

def _all():
    return brain_todos.list_todos(status="all")


def find(target):
    """(todo, None) for exactly one match, else (None, error result).
    A slug or permalink matches exactly; otherwise a case-insensitive title match,
    preferring open todos and an exact title over a substring."""
    t = (target or "").strip()
    if not t:
        return None, {"error": "say which todo"}
    slug = brain_todos.resolve_slug(t)
    items = _all()
    for it in items:
        if it["slug"] == slug:
            return it, None
    q = t.lower()
    for pool in ([i for i in items if i["status"] == "open"], items):
        exact = [i for i in pool if i["title"].lower() == q]
        hits = exact or [i for i in pool if q in i["title"].lower()]
        if len(hits) == 1:
            return hits[0], None
        if hits:
            hits = hits[:8]
            return None, {"error": "ambiguous", "ambiguous": True,
                          "note": "Several todos match. Ask which one, or use its slug. Do not guess.",
                          "candidates": [{"slug": i["slug"], "title": i["title"], "when": _when(i),
                                          "status": i["status"]} for i in hits],
                          BLOCKS: [rows_block(hits, "Which one?")]}
    return None, {"error": f"no todo matches '{t}'"}


# ------------------------------------------------------------------ rows

def _when(it):
    if it.get("start"):
        raw = str(it["start"])
        when = raw.replace("T", " ")[:16] if "T" in raw else raw[:10]
    else:
        when = str(it["due"]) if it.get("due") else ""
    if it.get("rrule"):
        rep = repeat_text(it["rrule"])
        when = f"{rep}, {when[11:]}" if "T" in str(it.get("start") or "") else rep
    return when


_BYDAY = {"MO": "Monday", "TU": "Tuesday", "WE": "Wednesday", "TH": "Thursday",
          "FR": "Friday", "SA": "Saturday", "SU": "Sunday"}
_REPEATS = {"daily": "FREQ=DAILY", "every day": "FREQ=DAILY",
            "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR", "every weekday": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
            "weekly": "FREQ=WEEKLY", "every week": "FREQ=WEEKLY",
            "fortnightly": "FREQ=WEEKLY;INTERVAL=2", "every two weeks": "FREQ=WEEKLY;INTERVAL=2",
            "monthly": "FREQ=MONTHLY", "every month": "FREQ=MONTHLY",
            "yearly": "FREQ=YEARLY", "every year": "FREQ=YEARLY", "annually": "FREQ=YEARLY"}
_NO_REPEAT = {"", "none", "no", "off", "never", "once", "one-off", "one off", "not"}


def repeat_text(rule):
    """'every weekday', 'every Monday and Thursday', 'daily': a rule as it is said."""
    parts = dict(c.partition("=")[::2] for c in str(rule).upper().replace("RRULE:", "").split(";") if "=" in c)
    freq, every = parts.get("FREQ", ""), int(parts.get("INTERVAL") or 1)
    days = [d for d in (parts.get("BYDAY") or "").split(",") if d in _BYDAY]
    if freq == "WEEKLY" and days:
        if sorted(days) == sorted(["MO", "TU", "WE", "TH", "FR"]):
            return "every weekday"
        names = [_BYDAY[d] for d in days]
        return "every " + (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1])
    unit = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}.get(freq)
    if not unit:
        return "repeats"
    return f"every {unit}" if every == 1 else f"every {every} {unit}s"


def resolve_repeat(phrase):
    """An rrule for a phrase or rule, '' to stop repeating, None if unreadable."""
    p = (phrase or "").strip()
    if p.lower() in _NO_REPEAT:
        return ""
    if p.upper().replace("RRULE:", "").startswith("FREQ="):
        return p.upper().replace("RRULE:", "")
    if p.lower() in _REPEATS:
        return _REPEATS[p.lower()]
    m = re.fullmatch(r"(?:every|each)\s+(.+)", p.lower())
    if m:
        names = [w.strip() for w in re.split(r",|\band\b", m.group(1)) if w.strip()]
        codes = [d[:2].upper() for n in names for d in _DAYS if d.startswith(n.rstrip("s")[:3])]
        if names and len(codes) == len(names):
            return "FREQ=WEEKLY;BYDAY=" + ",".join(codes)
    return None


def next_occurrence(it, day):
    """The first open day of a recurring todo on or after `day`."""
    raw = str(it.get("start") or it.get("due") or "")
    if not raw or not it.get("rrule"):
        return None
    timed = "T" in raw
    first = dt.datetime.fromisoformat(raw) if timed else dt.date.fromisoformat(raw[:10])
    horizon = day + dt.timedelta(days=800)
    until = dt.datetime.combine(horizon, dt.time.max) if timed else horizon
    marked = {str(d) for d in (it.get("done_dates") or []) + (it.get("failed_dates") or [])}
    for occ in brain_todos.expand_rrule(first, it["rrule"], until):
        d = occ.date() if timed else occ
        if d >= day and d.isoformat() not in marked:
            return d
    return None


def row(it, actions=False):
    """One todo as a panel-contract row. `actions` adds the Delete action."""
    resolved = it["status"] in ("done", "failed", "cancelled")
    detail = " · ".join(x for x in (_when(it), it.get("priority"),
                                    None if it["status"] == "open" else it["status"],
                                    it.get("location")) if x)
    out = {"id": it["slug"], "title": it["title"], "link": "/note/" + it["permalink"],
           "accent": CAT_TONES.get(it.get("category"), "muted")}
    if detail:
        out["detail"] = detail
    if resolved:
        out["struck"] = True
        out["done"] = it["status"] == "done"
    if it.get("start") and "T" in str(it["start"]):
        out["lead"] = {"text": str(it["start"])[11:16], "tone": "muted"}
    marks = []
    if it.get("urgent"):
        marks.append({"text": "", "icon": "star", "tone": "warn", "tip": "Starred"})
    if it.get("vital"):
        marks.append({"text": "!", "tone": "danger", "tip": "Vital"})
    if marks:
        out["marks"] = marks
    if actions:
        out["action"] = delete_action(it)
    return out


def delete_action(it):
    return {"label": "Delete", "method": "DELETE",
            "path": "/api/brain/api/todos/" + quote(it["slug"], safe="~!*'()"),
            "error": {"title": "Not deleted", "desc": "The brain did not delete it. Try again from the calendar."}}


def rows_block(items, title=None, actions=False):
    b = {"type": "rows", "rows": [row(i, actions) for i in items]}
    if title:
        b["title"] = title
    return b


def _changed(it, title="Updated"):
    return {"ok": True, "todo": {k: it.get(k) for k in ("slug", "title", "status", "priority", "due",
                                                          "start", "end", "urgent", "category", "rrule")},
            BLOCKS: [rows_block([it], title)]}


def _apply(target, fields, title):
    it, err = find(target)
    if err:
        return err
    try:
        return _changed(brain_todos.update(it["slug"], fields), title)
    except (ValueError, FileNotFoundError) as e:
        return {"error": str(e)}


# ------------------------------------------------------------------ tools

def update_todo(target, title=None, date=None, time=None, priority=None, starred=None,
                category=None, location=None, repeat=None):
    """Change fields on one todo. date/time take natural phrases. `repeat` sets a
    recurrence ('every weekday', 'weekly', an rrule) or stops one ('none'); a todo
    that stops repeating keeps its next open day unless a date is given."""
    it, err = find(target)
    if err:
        return err
    fields = {}
    if repeat is not None:
        rule = resolve_repeat(repeat)
        if rule is None:
            return {"error": f"could not read the repeat '{repeat}'; say daily, weekly, every weekday, "
                             "every Monday, or none to stop it"}
        if rule:
            fields["rrule"] = rule
        elif it.get("rrule"):
            fields.update(rrule=None, done_dates=None, failed_dates=None)
            if not date:
                nxt = next_occurrence(it, today())
                date = nxt.isoformat() if nxt else None
            it = {**it, "rrule": None}
    if title:
        fields["title"] = title
    if priority:
        fields["priority"] = priority.upper()
    if starred is not None:
        fields["urgent"] = bool(starred)
    if category:
        fields["category"] = category
    if location:
        fields["location"] = location
    if date or time:
        if it.get("rrule"):
            return {"error": "this todo repeats; its date and time cannot be moved from chat"}
        d = resolve_date(date) if date else None
        if date and d is None:
            return {"error": f"could not read the date '{date}'; give it as YYYY-MM-DD"}
        tm = resolve_time(time) if time else None
        if time and tm is None:
            return {"error": f"could not read the time '{time}'; give it as HH:MM"}
        start = str(it.get("start") or "")
        timed = "T" in start
        cur_date = dt.date.fromisoformat(start[:10]) if start else (
            dt.date.fromisoformat(str(it["due"])) if it.get("due") else None)
        new_date = d or cur_date or today()
        if tm or timed:
            new_start = dt.datetime.fromisoformat(f"{new_date.isoformat()}T{tm or start[11:19]}")
            fields["start"] = new_start.isoformat()
            if it.get("end") and timed:
                length = dt.datetime.fromisoformat(str(it["end"])) - dt.datetime.fromisoformat(start)
                fields["end"] = (new_start + length).isoformat()
            if it.get("due"):
                fields["due"] = None
        elif d and it.get("start"):
            fields["start"] = d.isoformat()
            if it.get("end"):
                fields["end"] = (d + (dt.date.fromisoformat(str(it["end"])[:10])
                                      - dt.date.fromisoformat(start[:10]))).isoformat()
        elif d and "rrule" in fields:
            fields["due"] = d.isoformat()
        elif d:
            fields["reschedule_to"] = d.isoformat()
    if not fields:
        return {"error": "nothing to change"}
    try:
        return _changed(brain_todos.update(it["slug"], fields))
    except (ValueError, FileNotFoundError) as e:
        return {"error": str(e)}


def reschedule_todo(target, when, time=None):
    """Move one todo to another day ('tomorrow', 'next Friday', a date)."""
    if resolve_date(when) is None:
        return {"error": f"could not read '{when}'; give it as YYYY-MM-DD"}
    out = update_todo(target, date=when, time=time)
    if out.get(BLOCKS):
        out[BLOCKS][0]["title"] = "Rescheduled"
    return out


def _day_mark(it, date, kind):
    """A recurring todo marks one date; a one-off closes."""
    if it.get("rrule"):
        d = resolve_date(date) if date else today()
        if d is None:
            raise ValueError(f"could not read the date '{date}'")
        return {f"{kind}_date": d.isoformat()}
    if kind == "done":
        return {"status": "done", "completed": today().isoformat()}
    return {"status": "failed"}


def mark_done(target, date=None):
    it, err = find(target)
    if err:
        return err
    try:
        fields = _day_mark(it, date, "done")
    except ValueError as e:
        return {"error": str(e)}
    return _apply(it["slug"], fields, "Done")


def mark_failed(target, date=None):
    """Did not happen."""
    it, err = find(target)
    if err:
        return err
    try:
        fields = _day_mark(it, date, "failed")
    except ValueError as e:
        return {"error": str(e)}
    return _apply(it["slug"], fields, "Did not happen")


def drop_todo(target):
    return _apply(target, {"status": "cancelled"}, "Dropped")


def delete_todo(target, confirmed=False):
    """Delete one todo for good, only with confirmed=True. Unconfirmed it writes
    nothing and returns the todo, with a Delete action, for the model to ask about."""
    it, err = find(target)
    if err:
        return err
    if confirmed is not True:
        return {"ok": True, "deleted": False,
                "note": (f"Nothing was deleted yet. Ask whether to delete '{it['title']}' for good. "
                         "After a clear yes, call delete_todo again with confirmed=true."),
                BLOCKS: [rows_block([it], "Delete this?", actions=True)]}
    try:
        gone = brain_todos.delete(it["slug"])
    except (ValueError, FileNotFoundError) as e:
        return {"error": str(e)}
    return {"ok": True, "deleted": True, "todo": {"slug": gone["slug"], "title": gone["title"]},
            BLOCKS: [{"type": "rows", "title": "Deleted",
                      "rows": [{**row(gone), "struck": True}]}]}


def create_todo(**kw):
    """tools.create_todo, plus a phrase `due` resolved in London and the new
    todo as a row. Creation itself stays in tools.py (the owner-request record)."""
    from . import tools
    due = kw.get("due")
    if due:
        d = resolve_date(due)
        if d is None:
            return {"error": f"could not read the date '{due}'; give it as YYYY-MM-DD"}
        kw["due"] = d.isoformat()
    out = tools.create_todo(**kw)
    if isinstance(out, dict) and out.get("slug") and not out.get("error"):
        out = {**out, BLOCKS: [rows_block([out], "Already there" if out.get("deduped") else "Added")]}
    return out
