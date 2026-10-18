"""brain-todos: TODO list as a first-class kind in the brain.

Todos live in ``todos/`` parallel to designs/, findings/, etc. They get
their own MCP tools (create/list/update/mark_done) and REST endpoints
(GET/POST/PATCH under /api/todos). They are NOT versioned like designs;
mutation is in-place — by design, since the lifecycle is open->done.

Default reads exclude ``status: done`` so a long backlog of completed
items doesn't drown the live ones; pass status="all" or status="done"
to see them.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path
from typing import Any

import frontmatter
from fastapi import APIRouter, HTTPException
from fastmcp import FastMCP
from pydantic import BaseModel

from g_todos.text import normalize_text, provenance_for
from g_todos.storage import atomic_write, store_lock
from g_todos.tickets import create_tickets_router, register_tools

BRAIN_ROOT = Path(os.environ.get("BRAIN_ROOT", Path.home() / "brain")).expanduser()
TODOS_DIR = BRAIN_ROOT / "todos"

VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}
# "failed" is not "cancelled": cancelled means it stopped mattering, failed
# means it mattered and did not happen. Keeping them apart is the difference
# between a list you can learn from and one that quietly forgets.
VALID_STATUSES = {"open", "done", "failed", "blocked", "cancelled", "expired"}
# The statuses that close a todo by hand. Setting one stamps `resolved_on`.
RESOLVED_STATUSES = {"done", "failed", "cancelled"}

# How long an open TODO stays live before the sweep retires it. Most TODOs
# here are voice-captured reminders ("remind me to X"), which are perishable:
# 77 were open on 2026-09-08 against 30 ever done, 24 of them from June/July.
# Nothing closed the loop, so the list stopped being readable. Real work is
# P0/P1 and never auto-expires; a recurring TODO never expires either.
EXPIRY_DAYS = {"P0": None, "P1": None, "P2": 60, "P3": 21}
# A low-priority dated thing still open a month past its date is dead.
EXPIRY_AFTER_DUE_DAYS = 30

mcp = FastMCP("brain-todos")


def today() -> str:
    return dt.date.today().isoformat()


_today = today  # Compatibility for existing callers.


_MACHINE_ALERT = re.compile(
    r"\b(?:dependabot|CVE(?:-\d+)*|secret[- ]scan|GitGuardian)\b|"
    r"\b(?:CI|workflow|build|deploy(?:ment)?|service|uptime|cron)\b[^\n.!?]{0,80}"
    r"\b(?:fail(?:ed|ure|ing)?|down|outage|alert)\b|"
    r"\b(?:fail(?:ed|ure|ing)?|down)\b[^\n.!?]{0,60}"
    r"\b(?:CI|workflow|build|deploy(?:ment)?|service|uptime|cron)\b", re.I)
# Project status and agent work, tuned against the real list: a work verb that
# opens the title and names engineering work, repository mechanics, or a job
# handed to an agent. Personal errands that merely start with "Finish" or name
# a vendor called Claude pass.
_PROJECT_WORK = re.compile(
    r"^(?:refactor|implement|merge|deploy|debug)\b|"
    r"^(?:finish|ship|integrate|wire)\b[^\n]*"
    r"\b(?:integration|renderer|pipeline|feature|release|backlog|tickets?|pass|migration|"
    r"readiness|rebuild|campaign|suite|probe|fix(?:es)?|regression|gameplay|scraper|"
    r"dashboard|e2e|tests?|statusline|onboarding)\b|"
    r"\b(?:git (?:branch|commit|push)|pull request|PR #?\d+|repo(?:sitory)?)\b|"
    r"\b(?:(?:an |the )?(?:agent|Claude|Codex|Sol) should|(?:ask|have|get|tell) (?:Claude|Codex|Sol|an agent)|subagent)\b|"
    r"\b(?:code changes?|fix(?:ing)? (?:a |the )?bug|auth module)\b", re.I)


def _prepare(title, body, provenance, policy=True):
    title = normalize_text(title, one_line=True)
    body = normalize_text(body)
    provenance = provenance_for(**{k: (provenance or {}).get(k)
                                  for k in ("requester", "composer", "source")})
    if not title:
        raise ValueError("title is required")
    text = title + "\n" + body
    if not policy:
        return title, body, provenance
    if _MACHINE_ALERT.search(text):
        raise ValueError("Machine alerts belong in the alert spine (brain_alert), or nowhere; not in todos")
    if provenance["requester"] != "gabriel":
        if _PROJECT_WORK.search(text):
            raise ValueError("Project status and agent work belong in create_ticket")
        if len(body) > 800:
            raise ValueError("Please supply one short line for the body (at most 800 characters)")
    return title, body, provenance


# ----------------------------------------------------- reminder ladders
# What a todo is worth being warned about, and how far out. Delivery lives in
# outlets/remind; the policy lives here, with the field it reads, so the
# calendar can show when a thing will ping without keeping a second copy of
# the table that actually fires.

_HOUR = 60
_DAY = 24 * _HOUR
_WEEK = 7 * _DAY


# Lead times in minutes before the moment, longest first, keyed on a todo's
# `category`.
LADDERS = {
    "chore":    [_HOUR, 5],
    "health":   [_DAY, 2 * _HOUR],
    "errand":   [_DAY, 2 * _HOUR],
    "social":   [_DAY, 2 * _HOUR],
    "personal": [_DAY, 2 * _HOUR],
    "family":   [3 * _DAY, _DAY],
    "finance":  [3 * _DAY, _DAY],
    "work":     [3 * _DAY, _DAY],
    "travel":   [3 * _WEEK, _WEEK, _DAY],
    "study":    [4 * _WEEK, 2 * _WEEK, _WEEK, 3 * _DAY, _DAY],
    "deadline": [4 * _WEEK, 2 * _WEEK, _WEEK, 3 * _DAY, _DAY],
}
DEFAULT_LADDER = [_DAY, 2 * _HOUR]
# A recurring todo cannot use rungs longer than its own gap — a 4-week warning
# for something that happens daily is 28 pings about the same habit. Rungs at
# or beyond the interval are dropped; a ladder emptied that way falls back to
# the short one, because the point of a daily thing is the nudge, not the lead.
RECURRING_LADDER = [_HOUR, 5]
# Below this, a rung on an all-day thing is pointing at midnight.
_SNAP_THRESHOLD = 12 * _HOUR

# The last moment a warning still leaves time to do something about it. Money
# needs a day to move; a dishwasher needs five minutes. This is the rung that
# is never allowed to be trimmed away, because a warning that arrives too late
# to act on is the same as no warning.
REACTION = {
    "chore":    5,
    "health":   2 * _HOUR,
    "errand":   2 * _HOUR,
    "social":   2 * _HOUR,
    "personal": 2 * _HOUR,
    "family":   _DAY,
    "finance":  _DAY,
    "work":     _DAY,
    "travel":   _DAY,
    "study":    _DAY,
    "deadline": _DAY,
}
DEFAULT_REACTION = 2 * _HOUR
# Two warnings is the floor: one far enough out to act on, one at the last
# useful moment. One warning is a single point of failure against a missed
# notification; zero is what the old code did to anything it could not fit.
# A todo marked `vital` gets three, and its last rung goes out at max ntfy
# priority instead of quietly joining the rest of the day's pings. Vital is
# not the same axis as priority: P0 is "this matters most", vital is "missing
# this has consequences I cannot undo".
MIN_RUNGS = 2
VITAL_MIN_RUNGS = 3
# When the floor has to invent a longer rung, it sits this far above the
# reaction rung. Eight is roughly "the day before" against a two-hour
# reaction, and "a week" against a day.
ESCALATION = 8


def ladder_for(todo: dict, all_day: bool = False,
               interval_min: int | None = None,
               lead_minutes: float | None = None) -> list[int]:
    """Lead times in minutes for one todo, longest first.

    Two halves, deliberately. The **automatic** half is the category ladder
    plus a guaranteed floor, so every dated todo gets at least a long warning
    and a last-useful-moment one without anybody configuring anything. The
    **dynamic** half is `reminders` (and legacy `remind_min`), which an agent
    or a human adds to that: extra rungs are merged in, never swapped for it.
    An override would let a caller silently reduce a todo to one warning, which
    is exactly the failure the floor exists to prevent.

    The trims live here rather than in the reminder outlet because the calendar
    page shows this ladder to a human. If the page renders the untrimmed
    ladder while the outlet fires a trimmed one, the page states a schedule
    that never happens.
    """
    cat = str(todo.get("category") or "").lower()
    rungs = set(LADDERS.get(cat, DEFAULT_LADDER))
    # A recurring charge gets one ping, the day before: Gabriel asked for
    # exactly one per charge (2026-10-13), and a day is what money needs to
    # move. Explicit `reminders` (rent's week) still merge in below.
    if interval_min and cat == "finance":
        rungs = {REACTION["finance"]}
    for extra in (todo.get("reminders") or []):
        try:
            rungs.add(int(extra))
        except (TypeError, ValueError):
            continue
    if todo.get("remind_min") is not None:
        rungs.add(int(todo["remind_min"]))

    # A recurring thing cannot use rungs longer than its own gap, or a 4-week
    # warning about a daily habit fires 28 times about the same habit.
    if interval_min:
        rungs = {r for r in rungs if r < interval_min} or set(RECURRING_LADDER)

    reaction = REACTION.get(cat, DEFAULT_REACTION)
    if interval_min and reaction >= interval_min:
        reaction = min(RECURRING_LADDER)

    if all_day:
        # A date means midnight, so anything under half a day lands the evening
        # before at a useless hour. Day-scale rungs, plus one on the day.
        rungs = {r for r in rungs if r >= _SNAP_THRESHOLD}
        rungs.add(0)
    else:
        rungs.add(reaction)

    # The floor is for one-off commitments. A recurring todo carries its own
    # redundancy (miss today's nudge and tomorrow's comes), and forcing a
    # second rung onto a daily habit invents useless ones: sixteen hours before
    # a 16:00 walk is midnight.
    floor = VITAL_MIN_RUNGS if todo.get("vital") else MIN_RUNGS
    if not interval_min:
        while len(rungs) < floor:
            # Escalate off the smallest *real* rung. An all-day ladder ends in
            # a 0 meaning "on the day", and multiplying zero got the old loop
            # nowhere, so a vital all-day todo silently kept only two rungs.
            base_rung = min([r for r in rungs if r > 0] or [reaction])
            longer = max(base_rung * ESCALATION, _DAY if all_day else _HOUR)
            while longer in rungs:
                longer *= 2
            if longer > 8 * _WEEK:
                break
            rungs.add(longer)

    # Trim to the notice the todo actually had. A thing set up this afternoon
    # cannot have been warned about four weeks ago, and a ladder that claims
    # otherwise is a ladder the calendar will render as a lie.
    if lead_minutes is not None:
        rungs = {r for r in rungs if r <= lead_minutes}
        # Topping up is for one-offs only, for the same reason the floor is:
        # a recurring todo's ladder is bounded by its own gap, and a rung
        # derived from the lead ignores that bound. Live for a day in Sep
        # 2026, this warned about a *daily* walk four days ahead and about a
        # weekday standup ten weeks ahead, because the lead to a far-off
        # occurrence is long even when the gap is one day.
        if not interval_min:
            step = 1
            while len(rungs) < floor and step <= 6:
                cand = max(1, int(lead_minutes / (2 ** step)))
                # All-day rungs all snap to the same morning, so a half-day
                # candidate is not a second warning, it is the same ping
                # twice. Round to whole days and let the loop find nothing if
                # there is genuinely no room for another distinct warning.
                if all_day:
                    cand = (cand // _DAY) * _DAY
                    if cand <= 0:
                        break
                rungs.add(cand)
                step += 1
        if not rungs:
            # Made with less notice than even its shortest rung. Warn at half
            # the time left rather than not at all.
            rungs = {max(1, int(lead_minutes // 2))}

    return sorted(rungs, reverse=True)


# --------------------------------------------------------- recurrence
# `rrule` is a todo field, so the todo module owns what it means. The reminder
# outlet and the calendar API both expand it through here rather than each
# carrying its own parser: two implementations of a recurrence rule drift, and
# the drift shows up as a missed reminder nobody can explain.

def _parse_until(raw):
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            return dt.datetime.strptime(str(raw), fmt)
        except ValueError:
            continue
    return None


_WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _rrule_parts(rule):
    parts = {}
    for chunk in str(rule).replace("RRULE:", "").split(";"):
        if "=" in chunk:
            k, _, v = chunk.partition("=")
            parts[k.strip().upper()] = v.strip().upper()
    return parts


def expand_rrule(start, rule, until):
    """Yield occurrences of `start` under `rule` up to `until`.

    Deliberately small: FREQ DAILY/WEEKLY/MONTHLY/YEARLY with INTERVAL and
    BYDAY, which is everything the voice agent and MCP can currently emit.
    An unrecognised rule degrades to the single `start`, never to silence.
    """
    parts = _rrule_parts(rule)
    freq = parts.get("FREQ")
    interval = int(parts.get("INTERVAL") or 1)
    count_cap = int(parts.get("COUNT") or 0) or None
    hard_until = _parse_until(parts.get("UNTIL")) if parts.get("UNTIL") else None
    if hard_until:
        until = min(until, hard_until)
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        yield start
        return
    byday = [_WEEKDAYS[d] for d in (parts.get("BYDAY") or "").split(",")
             if d in _WEEKDAYS]
    emitted = 0
    cur = start
    guard = 0
    while cur <= until and guard < 2000:
        guard += 1
        if freq == "WEEKLY" and byday:
            # Walk the week containing `cur` and emit its selected days.
            week_start = cur - dt.timedelta(days=cur.weekday())
            for wd in sorted(byday):
                occ = week_start + dt.timedelta(days=wd)
                if occ >= start and occ <= until:
                    yield occ
                    emitted += 1
                    if count_cap and emitted >= count_cap:
                        return
            cur = week_start + dt.timedelta(weeks=interval)
            continue
        yield cur
        emitted += 1
        if count_cap and emitted >= count_cap:
            return
        if freq == "DAILY":
            cur += dt.timedelta(days=interval)
        elif freq == "WEEKLY":
            cur += dt.timedelta(weeks=interval)
        elif freq == "MONTHLY":
            month = cur.month - 1 + interval
            year = cur.year + month // 12
            month = month % 12 + 1
            day = min(cur.day, [31, 29 if year % 4 == 0 and (year % 100 or
                      year % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30,
                      31, 30, 31][month - 1])
            cur = cur.replace(year=year, month=month, day=day)
        else:
            cur = cur.replace(year=cur.year + interval)


_FREQ_MINUTES = {"DAILY": _DAY, "WEEKLY": _WEEK, "MONTHLY": 30 * _DAY,
                 "YEARLY": 365 * _DAY}


def rrule_interval_minutes(rule):
    """Roughly how long between occurrences, for trimming the ladder."""
    parts = _rrule_parts(rule)
    base = _FREQ_MINUTES.get(parts.get("FREQ"))
    if not base:
        return None
    gap = base * int(parts.get("INTERVAL") or 1)
    byday = [d for d in (parts.get("BYDAY") or "").split(",") if d in _WEEKDAYS]
    if parts.get("FREQ") == "WEEKLY" and len(byday) > 1:
        gap = max(_DAY, gap // len(byday))
    return gap


def _slugify(title: str) -> str:
    s = title.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s or "todo"


def _allocate_slug(base: str) -> str:
    TODOS_DIR.mkdir(parents=True, exist_ok=True)
    candidate = base
    i = 2
    while (TODOS_DIR / f"{candidate}.md").exists():
        candidate = f"{base}-{i}"
        i += 1
    return candidate


def _path_for(slug: str) -> Path:
    # Refuse slugs that escape the todos dir.
    if "/" in slug or ".." in slug or slug.startswith("."):
        raise ValueError(f"invalid slug: {slug!r}")
    return TODOS_DIR / f"{slug}.md"


def _load(slug: str) -> frontmatter.Post:
    p = _path_for(slug)
    if not p.exists():
        raise FileNotFoundError(slug)
    return _effective_post(frontmatter.load(p))


def _save(slug: str, post: frontmatter.Post) -> None:
    p = _path_for(slug)
    atomic_write(p, frontmatter.dumps(post) + "\n")


def _serialize(slug: str, post: frontmatter.Post) -> dict[str, Any]:
    md = _effective_post(post).metadata
    out = {
        "slug": slug,
        "permalink": str(md.get("permalink") or f"brain/todos/{slug}"),
        "title": str(md.get("title") or slug),
        "kind": "todo",
        "status": str(md.get("status") or "open"),
        "priority": str(md.get("priority") or "P2"),
        "created": str(md.get("created") or ""),
        "due": md.get("due") or None,
        "relates_to": list(md.get("relates_to") or []),
        "summary": str(md.get("summary") or ""),
        "category": md.get("category") or None,
        "remind_min": md.get("remind_min"),
        "reminders": list(md.get("reminders") or []),
        "vital": bool(md.get("vital")),
        "urgent": bool(md.get("urgent")),
        "done_dates": [str(d) for d in (md.get("done_dates") or [])],
        "failed_dates": [str(d) for d in (md.get("failed_dates") or [])],
        "start": md.get("start") or None,
        "end": md.get("end") or None,
        "all_day": md.get("all_day"),
        "rrule": md.get("rrule") or None,
        "location": md.get("location") or None,
        "expires": md.get("expires") or None,
        "resolved_on": str(md["resolved_on"]) if md.get("resolved_on") else None,
        "body": post.content,
        "provenance": provenance_for(**{k: (md.get("provenance") or {}).get(k)
                                        for k in ("requester", "composer", "source")}),
        "source_key": md.get("source_key"),
        "source_keys": list(md.get("source_keys") or []),
    }
    for _k in ("due", "expires"):
        if out[_k] is not None:
            out[_k] = str(out[_k])
    return out


def _iter_todos():
    if not TODOS_DIR.exists():
        return
    for p in sorted(TODOS_DIR.glob("*.md")):
        slug = p.stem
        try:
            yield slug, _effective_post(frontmatter.load(p))
        except Exception:
            continue


def _filter(
    todos,
    status: str | None,
    priority: str | None,
    due_before: str | None,
    relates_to: str | None,
):
    for slug, post in todos:
        s = str(_effective_post(post).metadata.get("status") or "open")
        if status and status != "all" and s != status:
            continue
        if priority and str(post.metadata.get("priority") or "") != priority:
            continue
        if due_before:
            d = post.metadata.get("due")
            if not d or str(d) >= due_before:
                continue
        if relates_to:
            rel = [str(x) for x in (post.metadata.get("relates_to") or [])]
            if relates_to not in rel:
                continue
        yield slug, post


def _default_expires(priority: str, due: str | None, rrule: str | None) -> str | None:
    """When an open TODO should retire itself if nothing happens to it.

    Explicit beats derived; a recurring TODO never expires; a dated one dies a
    month after its date; otherwise priority decides.
    """
    if rrule:
        return None
    days = EXPIRY_DAYS.get(priority)
    # Priority wins over the date. A P0 a month past its due date is *overdue*,
    # which is the loudest thing a list can say - retiring it would delete the
    # signal. Only things already marked as low-stakes are allowed to rot.
    if not days:
        return None
    if due:
        try:
            return (dt.date.fromisoformat(str(due))
                    + dt.timedelta(days=EXPIRY_AFTER_DUE_DAYS)).isoformat()
        except ValueError:
            return None
    return (dt.date.today() + dt.timedelta(days=days)).isoformat()


def _effective_post(post, on=None):
    """A read derives expiry without mutating the stored note."""
    post = frontmatter.Post(post.content, **post.metadata)
    md = post.metadata
    if str(md.get("status") or "open") != "open":
        return post
    exp = md.get("expires")
    if not exp:
        exp = _default_expires(str(md.get("priority") or "P2"), md.get("due"), md.get("rrule"))
        days = EXPIRY_DAYS.get(str(md.get("priority") or "P2"))
        if exp and days and not md.get("due") and md.get("created"):
            try:
                exp = (dt.date.fromisoformat(str(md["created"])) + dt.timedelta(days=days)).isoformat()
            except ValueError:
                pass
    if exp:
        post["expires"] = str(exp)
        try:
            if dt.date.fromisoformat(str(exp)) < dt.date.fromisoformat(on or today()):
                post["status"] = "expired"
        except ValueError:
            pass
    return post


def iter_todos():
    """Yield (slug, Post) pairs with expiry applied at read time."""
    yield from _iter_todos()


def serialize(slug, post):
    return _serialize(slug, post)


def get(slug):
    """Read a todo Post, raising FileNotFoundError when absent."""
    return _load(slug)


def _by_source_key(source_key):
    if source_key:
        for slug, post in iter_todos():
            if post.get("source_key") == source_key or source_key in (post.get("source_keys") or []):
                return serialize(slug, post)
    return None


def upsert(slug, fields, body, provenance, source_key=None):
    """Write a known todo through the same policy as create.

    The same slug/key may be reconciled by its inlet; a key on another slug
    dedupes without writing. Use create for event-style, write-once ingestion.
    """
    with store_lock(TODOS_DIR):
        return _upsert(slug, fields, body, provenance, source_key)


def _upsert(slug, fields, body, provenance, source_key=None, lifecycle=False):
    _path_for(slug)
    existing = _by_source_key(source_key)
    if existing and existing["slug"] != slug:
        return {**existing, "deduped": True}
    try:
        post = get(slug)
        unchanged = (fields.get("title", post.get("title")) == post.get("title")
                     and body == post.content)
    except FileNotFoundError:
        unchanged = False
        post = frontmatter.Post("", kind="todo", permalink=f"brain/todos/{slug}",
                                status="open", priority="P2", created=today())
    for key, value in fields.items():
        if key in ("kind", "permalink", "created", "provenance", "source_key"):
            continue
        if key == "source_keys":
            keys = list(post.get("source_keys") or [])
            for extra in value or []:
                other = _by_source_key(extra)
                if other and other["slug"] != slug:
                    raise ValueError("source_key already belongs to another todo")
                if extra not in keys:
                    keys.append(extra)
            post["source_keys"] = keys
            continue
        if value is None:
            post.metadata.pop(key, None)
        else:
            post[key] = value
    title, body, provenance = _prepare(post.get("title"), body, provenance,
                                      policy=not (lifecycle and unchanged))
    post.metadata.setdefault("priority", "P2")
    post.metadata.setdefault("status", "open")
    if post.get("priority") not in VALID_PRIORITIES:
        raise ValueError(f"priority must be one of {sorted(VALID_PRIORITIES)}")
    if post.get("status") not in VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
    post["title"], post.content, post["provenance"] = title, body, provenance
    post["summary"] = normalize_text(post.get("summary") or title, one_line=True)
    if source_key:
        post["source_key"] = source_key
    _save(slug, post)
    return serialize(slug, post)


def create(
    title: str,
    priority: str = "P2",
    due: str | None = None,
    relates_to: list[str] | None = None,
    body: str | None = None,
    summary: str | None = None,
    category: str | None = None,
    remind_min: int | None = None,
    reminders: list[int] | None = None,
    vital: bool | None = None,
    urgent: bool | None = None,
    start: str | None = None,
    end: str | None = None,
    all_day: bool | None = None,
    rrule: str | None = None,
    location: str | None = None,
    expires: str | None = None,
    requester: str | None = None,
    composer: str | None = None,
    source: str | None = None,
    source_key: str | None = None,
) -> dict[str, Any]:
    """Create an owner action, recording origin and deduping all statuses."""
    with store_lock(TODOS_DIR):
        return _create(title, priority, due, relates_to, body, summary, category, remind_min,
                       reminders, vital, urgent, start, end, all_day, rrule, location, expires,
                       requester, composer, source, source_key)


def _create(title, priority, due, relates_to, body, summary, category, remind_min,
            reminders, vital, urgent, start, end, all_day, rrule, location, expires,
            requester, composer, source, source_key):
    existing = _by_source_key(source_key)
    if existing:
        return {**existing, "deduped": True}
    title, body, provenance = _prepare(title, body, provenance_for(requester, composer, source))
    if priority not in VALID_PRIORITIES:
        raise ValueError(f"priority must be one of {sorted(VALID_PRIORITIES)}")
    fields = dict(title=title, priority=priority, due=due, relates_to=relates_to,
                  summary=summary or title, category=category, remind_min=remind_min,
                  reminders=[int(m) for m in reminders] if reminders else None,
                  vital=vital, urgent=urgent, start=start, end=end, all_day=all_day,
                  rrule=rrule, location=location,
                  expires=expires if expires is not None else _default_expires(priority, due, rrule))
    return _upsert(_allocate_slug(_slugify(title)), fields, body, provenance, source_key)


def _update(slug: str, fields: dict[str, Any]) -> dict[str, Any]:
    with store_lock(TODOS_DIR):
        return _update_locked(slug, fields)


def _update_locked(slug, fields):
    post = _load(slug)
    original_keys = set(post.metadata)
    fields = fields.copy()
    body = fields.pop("body", None)

    # A recurring todo is one note covering many days, so "done" has to attach
    # to a date rather than to the note: marking today's walk done must not
    # retire every future walk. `done_date` ticks one occurrence off,
    # `undone_date` puts it back.
    for key, field in (("done_date", "done_dates"), ("failed_date", "failed_dates")):
        mark = fields.pop(key, None)
        clear = fields.pop("un" + key, None)
        if not (mark or clear):
            continue
        dates = {str(d) for d in (post.metadata.get(field) or [])}
        if mark:
            dt.date.fromisoformat(str(mark))  # reject anything unparseable
            dates.add(str(mark))
            # An occurrence is one or the other, never both.
            other = "failed_dates" if field == "done_dates" else "done_dates"
            rest = {str(d) for d in (post.metadata.get(other) or [])} - {str(mark)}
            if rest:
                post[other] = sorted(rest)
            else:
                post.metadata.pop(other, None)
        if clear:
            dates.discard(str(clear))
        if dates:
            post[field] = sorted(dates)
        else:
            post.metadata.pop(field, None)

    # Reschedule: push the whole thing back by N days. One-offs only, because
    # moving a single occurrence of a recurring todo means storing a per-date
    # exception map, and "skip it, the next one is tomorrow" is the honest
    # answer for a habit.
    # Absolute reschedule beats a relative nudge once something is properly
    # late: shifting an 80-day-late todo "by one day" leaves it 79 days late,
    # which is not what anyone means by "push it back".
    to = fields.pop("reschedule_to", None)
    postpone = fields.pop("postpone_days", None)
    if to:
        target = dt.date.fromisoformat(str(to))
        if post.metadata.get("rrule"):
            raise ValueError("cannot reschedule a recurring todo; skip the occurrence instead")
        anchor = None
        for key in ("start", "due"):
            if post.metadata.get(key):
                raw = str(post[key])
                anchor = (dt.datetime.fromisoformat(raw).date() if "T" in raw
                          else dt.date.fromisoformat(raw))
                break
        postpone = (target - anchor).days if anchor else None
        if postpone is None:
            post["due"] = target.isoformat()
    if postpone:
        days = dt.timedelta(days=int(postpone))
        if post.metadata.get("rrule"):
            raise ValueError("cannot postpone a recurring todo; skip the occurrence instead")
        for key in ("due", "expires"):
            if post.metadata.get(key):
                post[key] = (dt.date.fromisoformat(str(post[key])) + days).isoformat()
        for key in ("start", "end"):
            if post.metadata.get(key):
                post[key] = (dt.datetime.fromisoformat(str(post[key])) + days).isoformat()

    # Validate any fields we recognise; let unknown keys through (frontmatter
    # is a free-form mapping). Block changes to kind/permalink/created which
    # would break identity.
    for k in ("kind", "permalink", "created"):
        fields.pop(k, None)

    if "priority" in fields and fields["priority"] is not None:
        if fields["priority"] not in VALID_PRIORITIES:
            raise ValueError(f"priority must be one of {sorted(VALID_PRIORITIES)}")
    if "status" in fields and fields["status"] is not None:
        if fields["status"] not in VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")

    # Closing a todo records the day it was closed. The calendar needs it: a
    # late thing closed today shows struck through on today, and from tomorrow
    # lives only on its own date. Without the stamp "closed this morning" and
    # "closed a month ago" look the same.
    new_status = fields.get("status")
    if new_status and new_status != post.metadata.get("status"):
        if new_status in RESOLVED_STATUSES:
            fields["resolved_on"] = _today()
        else:
            post.metadata.pop("resolved_on", None)

    for k, v in fields.items():
        if v is None or (k in ("urgent", "vital") and v is False):
            post.metadata.pop(k, None)
        else:
            post[k] = v

    if body is not None:
        post.content = str(body).rstrip() + "\n"

    # Status changes, ticks and reschedules on a stored todo never re-judge it:
    # a legacy item must stay closable. A changed title or body is new wording
    # and goes back through the owner-action check.
    updated = {key: post.metadata.get(key) for key in original_keys | set(post.metadata)}
    return _upsert(slug, updated, post.content, post.get("provenance"), post.get("source_key"),
                  lifecycle=True)


def _mark_done(slug: str) -> dict[str, Any]:
    return _update(slug, {"status": "done", "completed": _today()})


def _resolve_slug(permalink_or_slug: str) -> str:
    """Accept either a bare slug or a permalink like brain/todos/<slug>."""
    s = permalink_or_slug.strip()
    for prefix in ("brain/todos/", "todos/"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return s


# --------------------------------------------------------------------- MCP

@mcp.tool()
def create_todo(
    title: str,
    priority: str,
    requester: str,
    due: str | None = None,
    relates_to: list[str] | None = None,
    body: str | None = None,
    category: str | None = None,
    remind_min: int | None = None,
    reminders: list[int] | None = None,
    vital: bool | None = None,
    start: str | None = None,
    end: str | None = None,
    all_day: bool | None = None,
    rrule: str | None = None,
    location: str | None = None,
    expires: str | None = None,
    composer: str = "agent",
    source: str = "mcp",
    source_key: str | None = None,
) -> dict:
    """Create a TODO note under todos/<slug>.md.

    Args:
      title: human-readable task title.
      priority: one of P0, P1, P2, P3.
      due: optional ISO date (YYYY-MM-DD).
      relates_to: optional list of permalinks this TODO relates to.
      body: optional markdown body.
      category: optional category driving the calendar colour AND the automatic
        reminder ladder (work, chore, social, family, health, finance, study,
        deadline, travel, personal, errand). Setting it right is how a todo
        gets warned about proportionally, so set it.
      vital: set True when missing this has consequences that cannot be undone.
        Vital todos get three warnings instead of two, and their final warning
        goes out at max ntfy priority so it breaks through. It is a separate
        axis from priority: P0 means "this matters most", vital means "missing
        it is not recoverable". Do not set it on everything or it means
        nothing.
      reminders: optional extra lead times in minutes, e.g. [10080] for an
        additional week's notice. These are ADDED to the category ladder, never
        substituted for it, so you cannot accidentally leave a todo with fewer
        warnings than the floor of two. Use it when something needs more notice
        than its category gives by default.
      requester: who asked for this owner action (gabriel, agent, or automation id).
      composer: who wrote the words; defaults to agent.
      source: originating event; defaults to mcp.
      source_key: optional stable event key; retries return the original todo.
      expires: optional ISO date after which this TODO reads as expired. Omit
        to take the default for its priority (see EXPIRY_DAYS); pass None-able
        explicit dates only when the caller genuinely knows the shelf life.

    Returns the created todo as a dict.
    """
    return create(title, priority, due, relates_to, body,
                  requester=requester, composer=composer, source=source, source_key=source_key,
                  category=category, remind_min=remind_min, reminders=reminders, vital=vital,
                  start=start, end=end, all_day=all_day, rrule=rrule, location=location, expires=expires)


@mcp.tool()
def list_todos(
    status: str = "open",
    priority: str | None = None,
    due_before: str | None = None,
    relates_to: str | None = None,
) -> list[dict]:
    """List TODOs. Defaults to status=open; pass status='all' to include done."""
    out = []
    for slug, post in _filter(_iter_todos(), status, priority, due_before, relates_to):
        out.append(_serialize(slug, post))
    return out


@mcp.tool()
def update_todo(
    permalink: str,
    title: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    due: str | None = None,
    relates_to: list[str] | None = None,
    summary: str | None = None,
    body: str | None = None,
) -> dict:
    """Patch frontmatter fields and/or body on a TODO.

    `permalink` accepts a bare slug or full permalink. Pass `body=...`
    to replace the markdown body. Pass `status='done'` to complete.
    Only fields you pass are touched; omitted fields are left as-is.
    """
    slug = _resolve_slug(permalink)
    fields = {
        k: v for k, v in {
            "title": title, "status": status, "priority": priority,
            "due": due, "relates_to": relates_to, "summary": summary,
            "body": body,
        }.items() if v is not None
    }
    return _update(slug, fields)


@mcp.tool()
def mark_done(permalink: str) -> dict:
    """Shortcut: set a TODO's status to done."""
    slug = _resolve_slug(permalink)
    return _mark_done(slug)


register_tools(mcp)


# --------------------------------------------------------------------- REST

class _TodoCreate(BaseModel):
    title: str
    priority: str
    due: str | None = None
    relates_to: list[str] | None = None
    body: str | None = None
    category: str | None = None
    reminders: list[int] | None = None
    vital: bool | None = None
    urgent: bool | None = None
    start: str | None = None
    end: str | None = None
    all_day: bool | None = None
    rrule: str | None = None
    summary: str | None = None
    remind_min: int | None = None
    location: str | None = None
    expires: str | None = None
    requester: str | None = None
    composer: str | None = None
    source: str | None = None
    source_key: str | None = None


class _TodoPatch(BaseModel):
    title: str | None = None
    status: str | None = None
    priority: str | None = None
    due: str | None = None
    relates_to: list[str] | None = None
    summary: str | None = None
    body: str | None = None
    urgent: bool | None = None
    vital: bool | None = None
    done_date: str | None = None
    undone_date: str | None = None
    failed_date: str | None = None
    unfailed_date: str | None = None
    postpone_days: int | None = None
    reschedule_to: str | None = None
    source_keys: list[str] | None = None

    model_config = {"extra": "allow"}



def occurrences(date_from: str, date_to: str, status: str = "open") -> list[dict]:
    """Every dated todo laid out on a calendar between two dates, inclusive.

    One entry per *occurrence*, so a recurring todo appears on each of its days
    rather than once on the day it was first set. Undated todos are absent by
    design: the calendar shows time, the todo list shows work.
    """
    lo = dt.date.fromisoformat(date_from)
    hi = dt.date.fromisoformat(date_to)
    horizon = dt.datetime.combine(hi, dt.time(23, 59))
    out = []
    # "live" is open plus everything closed by hand: the calendar has to keep
    # showing what you ticked off, or the day empties itself as you work
    # through it and you lose the record of having done anything. Cancelled
    # is in so a late thing you dropped can stay on its own day as missed.
    wanted = {"open", "done", "failed", "cancelled"} if status == "live" else None
    today = dt.date.today()
    carried = []
    for slug, post in _filter(_iter_todos(), None if wanted else status, None, None, None):
        if wanted and str(post.metadata.get("status") or "open") not in wanted:
            continue
        t = _serialize(slug, post)
        done_dates = set(t["done_dates"])
        failed_dates = set(t["failed_dates"])
        note_done = t["status"] == "done"
        note_failed = t["status"] == "failed"
        note_dropped = t["status"] == "cancelled"
        if note_dropped and t.get("rrule"):
            continue  # a dropped habit has no days left to show
        resolved_on = t["resolved_on"]

        def _carries(when):
            # Open work comes back onto today until it is closed. Closed work
            # comes back only on the day it was closed, struck through, so the
            # day you are looking at still shows what you just dealt with.
            return when < today and (t["status"] == "open"
                                     or resolved_on == today.isoformat())

        def _on_own_day(when):
            # A dropped thing stays on its date, marked missed, only if it was
            # already late when it was dropped. Dropping a plan before its day
            # means it never was going to happen, which is not a miss.
            return not note_dropped or bool(resolved_on and resolved_on > when.isoformat())

        base = {k: t[k] for k in ("slug", "permalink", "title", "summary",
                                  "category", "priority", "status", "location")}
        # The ladder travels with the occurrence so the calendar can say when
        # a thing will ping without re-deriving the policy.
        base["ladder"] = None  # filled per occurrence, it depends on all_day
        base["vital"] = t["vital"]
        base["urgent"] = t["urgent"]
        start_raw = t.get("start")
        start = None
        if start_raw:
            try:
                start = dt.datetime.fromisoformat(str(start_raw))
            except ValueError:
                start = None
        all_day = bool(t.get("all_day")) or (
            start is not None and start.hour == 0 and start.minute == 0
            and not t.get("end"))

        created = None
        if t.get("created"):
            try:
                created = dt.datetime.combine(
                    dt.date.fromisoformat(str(t["created"])), dt.time(0, 0))
            except ValueError:
                created = None

        def _lead(moment):
            return None if created is None else (moment - created).total_seconds() / 60

        if start and t.get("rrule"):
            gap = rrule_interval_minutes(t["rrule"])
            ladder = ladder_for(t, all_day, gap)
            for occ_dt in expand_rrule(start, t["rrule"], horizon):
                if lo <= occ_dt.date() <= hi:
                    day = occ_dt.date().isoformat()
                    out.append({**base, "ladder": ladder, "date": day,
                                "time": None if all_day else occ_dt.strftime("%H:%M"),
                                "all_day": all_day, "recurring": True,
                                "done": note_done or day in done_dates,
                                "failed": note_failed or day in failed_dates,
                                "dropped": False,
                                "late_days": 0, "original_date": None})
            continue
        if start:
            when = start.date()
            if _carries(when):
                carried.append((t, ladder_for(t, all_day), when,
                                None if all_day else start.strftime("%H:%M"), all_day))
            if lo <= when <= hi and _on_own_day(when):
                out.append({**base, "ladder": ladder_for(t, all_day, None, _lead(start)),
                            "date": start.date().isoformat(),
                            "time": None if all_day else start.strftime("%H:%M"),
                            "all_day": all_day, "recurring": False,
                            "done": note_done, "failed": note_failed or note_dropped,
                            "dropped": note_dropped,
                            "late_days": 0, "original_date": None})
            continue
        if t.get("due"):
            try:
                d = dt.date.fromisoformat(str(t["due"]))
            except ValueError:
                continue
            if _carries(d):
                carried.append((t, ladder_for(t, True), d, None, True))
            if lo <= d <= hi and _on_own_day(d):
                out.append({**base,
                            "ladder": ladder_for(t, True, None, _lead(
                                dt.datetime.combine(d, dt.time(0, 0)))),
                            "date": d.isoformat(), "time": None,
                            "all_day": True, "recurring": False,
                            "done": note_done, "failed": note_failed or note_dropped,
                            "dropped": note_dropped,
                            "late_days": 0, "original_date": None})
    # Everything unresolved reappears on today, labelled with how late it is,
    # so the day you are actually looking at holds everything outstanding.
    #
    # Closed work does not carry, except on the day it was closed (see
    # `_carries`). Failed used to carry forever as "a record, not a
    # resolution", which left a missed dishwasher from three weeks ago on
    # every today with no way to clear it but Drop, which then made it vanish
    # without trace. The record now lives on the thing's own date instead.
    #
    # Recurring todos are deliberately excluded: the next occurrence *is* the
    # carry, and a fortnight of missed walks would bury the real work.
    if lo <= today <= hi:
        for t, ladder, when, time_s, all_day in carried:
            out.append({
                **{k: t[k] for k in ("slug", "permalink", "title", "summary",
                                     "category", "priority", "status", "location",
                                     "vital", "urgent")},
                "ladder": ladder, "date": today.isoformat(), "time": time_s,
                "all_day": all_day, "recurring": False,
                "done": t["status"] == "done",
                "failed": t["status"] in ("failed", "cancelled"),
                "dropped": t["status"] == "cancelled",
                "late_days": (today - when).days,
                "original_date": when.isoformat(),
            })
    # Within a day: starred first, then vital, then by time, all-day last.
    # Ordering by time alone buried the things that matter most at the bottom
    # of a cell, which is exactly where you stop reading. Every view shares
    # this order because they all read this list.
    out.sort(key=lambda e: (e["date"], not e.get("urgent"), not e.get("vital"),
                            e["time"] is None, e["time"] or "", e["title"]))
    return out


def create_router() -> APIRouter:
    r = APIRouter(prefix="/todos", tags=["todos"])

    @r.get("")
    async def list_(
        status: str = "open",
        priority: str | None = None,
        due_before: str | None = None,
        relates_to: str | None = None,
    ):
        return [
            _serialize(slug, post)
            for slug, post in _filter(_iter_todos(), status, priority, due_before, relates_to)
        ]

    @r.get("/calendar")
    async def calendar_(
        date_from: str | None = None,
        date_to: str | None = None,
        status: str = "live",
    ):
        today = dt.date.today()
        lo = date_from or (today - dt.timedelta(days=today.weekday())).isoformat()
        hi = date_to or (dt.date.fromisoformat(lo) + dt.timedelta(days=41)).isoformat()
        try:
            return occurrences(lo, hi, status)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @r.get("/{slug}")
    async def get_(slug: str):
        try:
            post = _load(slug)
        except FileNotFoundError:
            raise HTTPException(404, "todo not found")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return _serialize(slug, post)

    @r.post("")
    async def create_(payload: _TodoCreate):
        try:
            return create(**payload.model_dump())
        except ValueError as e:
            raise HTTPException(400, str(e))

    @r.patch("/{slug}")
    async def patch_(slug: str, payload: _TodoPatch):
        fields = payload.model_dump(exclude_unset=True)
        try:
            return _update(slug, fields)
        except FileNotFoundError:
            raise HTTPException(404, "todo not found")
        except ValueError as e:
            raise HTTPException(400, str(e))

    return r


def expire_stale(today: str | None = None, apply: bool = False) -> dict:
    """Report derived expiry; the legacy apply argument no longer writes.

    Keep the report shape for manual/old timer callers. `backfilled` counts
    legacy expiry dates derived in memory, and `applied` is always false.
    """
    now = dt.date.fromisoformat(today) if today else dt.date.today()
    retired, backfilled = [], 0
    for path in sorted(TODOS_DIR.glob("*.md")):
        slug, post = path.stem, frontmatter.load(path)
        if str(post.get("status") or "open") != "open":
            continue
        effective = _effective_post(post, on=now.isoformat())
        if not post.get("expires") and effective.get("expires"):
            backfilled += 1
        if effective.get("status") == "expired":
            retired.append({"slug": slug, "title": str(post.get("title") or slug),
                            "priority": str(post.get("priority") or "P2"),
                            "expires": str(effective["expires"])})
    return {"checked_at": now.isoformat(), "backfilled": backfilled,
            "retired": len(retired), "retired_detail": retired, "applied": False}


def main() -> int:
    import json as _json
    # Both the old timer invocation and --dry-run are now read-only reports.
    print(_json.dumps(expire_stale(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
