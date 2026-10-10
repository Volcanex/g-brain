"""Tests for the todo logic the reminder system stands on.

Run:  .venv/bin/python -m tests.test_todos

No pytest: it is not in the venv and this does not need one. A file of plain
asserts that exits non-zero is enough to catch the class of bug these cover,
which is the only kind that matters here.

**Why this exists.** Every case below is a bug that shipped. The reminder
ladder is exactly the sort of code that fails silently: nothing errors, a ping
just never arrives, or arrives about the wrong thing, and nobody notices for a
week. Twice now that happened in production. Assertions are cheaper than
noticing.

Each test runs against a throwaway BRAIN_ROOT so it can never touch the real
todos.
"""
import datetime as dt
import os
import shutil
import sys
import tempfile
import traceback

_TMP = tempfile.mkdtemp(prefix="brain-todos-test-")
os.environ["BRAIN_ROOT"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brain_todos as B  # noqa: E402  (must follow the BRAIN_ROOT override)

HOUR, DAY, WEEK = 60, 1440, 10080
_failures = []


def check(name, got, want):
    if got != want:
        _failures.append(f"{name}\n      got  {got}\n      want {want}")


def make(slug, **md):
    md.setdefault("title", slug)
    md.setdefault("status", "open")
    md.setdefault("created", dt.date.today().isoformat())
    post = __import__("frontmatter").Post(content="")
    post["kind"] = "todo"
    post["permalink"] = f"brain/todos/{slug}"
    for k, v in md.items():
        post[k] = v
    B._save(slug, post)
    return B._serialize(slug, B._load(slug))


# ------------------------------------------------------------------ ladders

def test_category_ladders():
    check("finance", B.ladder_for({"category": "finance"}), [3 * DAY, DAY])
    check("chore", B.ladder_for({"category": "chore"}), [HOUR, 5])
    check("travel", B.ladder_for({"category": "travel"}), [3 * WEEK, WEEK, DAY])
    # An unknown category must still get a usable ladder, not an empty one.
    check("unknown category", B.ladder_for({"category": "nonsense"}), [DAY, 2 * HOUR])
    check("no category", B.ladder_for({}), [DAY, 2 * HOUR])


def test_extra_reminders_are_additive():
    """`reminders` and `remind_min` add to the ladder, never replace it.

    An override let a caller silently drop a todo below the two-warning floor,
    which is the failure the floor exists to prevent.
    """
    check("reminders merge",
          B.ladder_for({"category": "finance", "reminders": [WEEK]}),
          [WEEK, 3 * DAY, DAY])
    check("remind_min merges, does not replace",
          B.ladder_for({"category": "travel", "remind_min": 15}),
          [3 * WEEK, WEEK, DAY, 15])
    check("junk in reminders is ignored, not fatal",
          B.ladder_for({"category": "chore", "reminders": ["x", None]}),
          [HOUR, 5])


def test_floor():
    """Two rungs for a one-off, three if vital, and a reaction rung always."""
    check("one-off keeps two", len(B.ladder_for({"category": "chore"})), 2)
    check("vital gets three", len(B.ladder_for({"category": "chore", "vital": True})), 3)
    # deadline's ladder is already long; vital must not shorten it.
    check("vital never truncates a long ladder",
          B.ladder_for({"category": "deadline", "vital": True}),
          B.ladder_for({"category": "deadline"}))
    # finance's reaction rung is a day: money needs a day to move.
    check("reaction rung present", B.ladder_for({"category": "finance"})[-1], DAY)


def test_all_day_rungs():
    """A date means midnight, so sub-day rungs would fire the evening before
    at a useless hour. Day-scale only, plus one on the day."""
    check("all-day keeps day scale plus 0",
          B.ladder_for({"category": "finance"}, all_day=True), [3 * DAY, DAY, 0])
    check("all-day chore is not left empty",
          B.ladder_for({"category": "chore"}, all_day=True)[-1], 0)
    # A vital all-day todo must gain a real extra rung, not a second zero.
    vital = B.ladder_for({"category": "errand", "vital": True}, all_day=True)
    check("vital all-day has distinct rungs", len(vital), len(set(vital)))
    check("vital all-day reaches three", len(vital), 3)


def test_recurring_is_bounded_by_its_own_gap():
    """THE BUG (live, Sep 2026): a daily walk was warned about four days
    ahead, and a weekday standup ten weeks ahead.

    Two separate paths invented those rungs. The interval filter bounds the
    ladder, but the floor top-up and then the lead-trim top-up both added
    longer rungs back, and the lead to a far-off occurrence is long even when
    the gap is one day. Neither top-up may run for a recurring todo.
    """
    daily = {"category": "health"}                  # ladder [1d, 2h]
    check("daily keeps only sub-day rungs",
          B.ladder_for(daily, False, DAY), [2 * HOUR])
    check("daily is not topped up from a long lead",
          B.ladder_for(daily, False, DAY, 7 * DAY), [2 * HOUR])
    check("weekday standup is not topped up from a long lead",
          B.ladder_for({"category": "work"}, False, 2016, 70 * DAY), [DAY])
    # A ladder wholly longer than the gap falls back rather than emptying.
    check("study daily falls back to the short ladder",
          B.ladder_for({"category": "study"}, False, DAY), [HOUR, 5])
    check("vital does not raise a recurring floor",
          B.ladder_for({"category": "health", "vital": True}, False, DAY), [2 * HOUR])


def test_lead_trim():
    """A todo made this afternoon cannot have been warned about four weeks
    ago, and a ladder claiming otherwise is one the calendar renders as a lie.
    """
    check("rungs beyond the lead are dropped",
          B.ladder_for({"category": "study"}, False, None, 10 * DAY),
          [WEEK, 3 * DAY, DAY])
    # Made with two hours' notice: something must still fire.
    short = B.ladder_for({"category": "study"}, False, None, 120)
    check("short lead still warns", len(short) >= 1, True)
    check("short lead fits the time available", max(short) <= 120, True)
    # Vital with a day's notice gets three warnings inside that day.
    vital = B.ladder_for({"category": "study", "vital": True}, False, None, 2 * DAY)
    check("vital tops up inside the lead", len(vital), 3)
    check("vital top-ups fit the lead", max(vital) <= 2 * DAY, True)


# --------------------------------------------------------------- recurrence

def test_expand_rrule():
    start = dt.datetime(2026, 9, 21, 10, 0)          # a Monday
    until = start + dt.timedelta(days=13)
    check("daily count", len(list(B.expand_rrule(start, "FREQ=DAILY", until))), 14)
    check("weekly byday",
          [d.strftime("%a %d") for d in B.expand_rrule(start, "FREQ=WEEKLY;BYDAY=MO,FR", until)],
          ["Mon 21", "Fri 25", "Mon 28", "Fri 02"])
    check("interval", len(list(B.expand_rrule(start, "FREQ=DAILY;INTERVAL=3", until))), 5)
    # An unrecognised rule degrades to the single start, never to silence.
    check("unknown rule still yields once",
          list(B.expand_rrule(start, "FREQ=FORTNIGHTLY", until)), [start])
    # A monthly charge on the 31st must not fall off the end of February.
    jan31 = dt.datetime(2027, 1, 31, 9, 0)
    got = [d.date().isoformat() for d in
           B.expand_rrule(jan31, "FREQ=MONTHLY", dt.datetime(2027, 3, 31, 9, 0))]
    check("monthly clamps to month length", got,
          ["2027-01-31", "2027-02-28", "2027-03-28"])


def test_rrule_interval_minutes():
    check("daily gap", B.rrule_interval_minutes("FREQ=DAILY"), DAY)
    check("weekly gap", B.rrule_interval_minutes("FREQ=WEEKLY"), WEEK)
    check("weekday gap is not a week", B.rrule_interval_minutes("FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"), 2016)
    check("unknown rule has no gap", B.rrule_interval_minutes("FREQ=NEVER"), None)


# -------------------------------------------------------------- occurrences

def test_carry_forward():
    """Work that passed its date has to turn up again, or the calendar quietly
    forgets it exactly when it matters most."""
    today = dt.date.today()
    old = (today - dt.timedelta(days=5)).isoformat()
    make("carry-open", due=old, category="errand")
    make("carry-failed", due=old, category="errand", status="failed")
    make("carry-done", due=old, category="errand", status="done")
    make("carry-cancelled", due=old, category="errand", status="cancelled")
    rows = {r["slug"]: r for r in
            B.occurrences(today.isoformat(), today.isoformat(), "live")}
    check("open work carries", rows["carry-open"]["late_days"], 5)
    check("failed work carries too", rows["carry-failed"]["late_days"], 5)
    check("failed keeps its flag when carried", rows["carry-failed"]["failed"], True)
    check("done work does not carry", "carry-done" in rows, False)
    check("cancelled work does not carry", "carry-cancelled" in rows, False)
    check("carried rows name their real date",
          rows["carry-open"]["original_date"], old)


def test_recurring_does_not_carry():
    """The next occurrence is the carry. A fortnight of missed walks would
    bury the real work."""
    today = dt.date.today()
    start = (today - dt.timedelta(days=10)).isoformat() + "T08:00:00"
    make("carry-habit", start=start, rrule="FREQ=DAILY", category="health")
    rows = [r for r in B.occurrences(today.isoformat(), today.isoformat(), "live")
            if r["slug"] == "carry-habit"]
    check("habit appears once today", len(rows), 1)
    check("habit is not marked late", rows[0]["late_days"], 0)


def test_per_occurrence_completion():
    """THE BUG (live, Sep 2026): ticking one day's walk set status=done on the
    note and retired every future occurrence. Two habits were killed that way.
    """
    today = dt.date.today()
    tomorrow = (today + dt.timedelta(days=1)).isoformat()
    make("occ-habit", start=today.isoformat() + "T08:00:00",
         rrule="FREQ=DAILY", category="health")
    B._update("occ-habit", {"done_date": today.isoformat()})
    rows = {r["date"]: r for r in
            B.occurrences(today.isoformat(), tomorrow, "live")
            if r["slug"] == "occ-habit"}
    check("today is ticked", rows[today.isoformat()]["done"], True)
    check("tomorrow is untouched", rows[tomorrow]["done"], False)
    check("the note itself stays open",
          B._serialize("occ-habit", B._load("occ-habit"))["status"], "open")


def test_done_and_failed_are_exclusive():
    today = dt.date.today().isoformat()
    make("excl", start=today + "T08:00:00", rrule="FREQ=DAILY", category="health")
    B._update("excl", {"failed_date": today})
    B._update("excl", {"done_date": today})
    t = B._serialize("excl", B._load("excl"))
    check("marking done clears failed", t["failed_dates"], [])
    check("marking done records it", t["done_dates"], [today])


def test_reschedule():
    """Absolute, not relative: nudging an 80-day-late todo 'by a day' leaves
    it 79 days late, which is not what anyone means by pushing it back."""
    make("resched", due="2026-01-10", expires="2026-02-10", category="errand")
    B._update("resched", {"reschedule_to": "2026-03-01"})
    t = B._serialize("resched", B._load("resched"))
    check("due moves to the target", t["due"], "2026-03-01")
    # The shelf life travels with the work: both dates shift by the same 50
    # days, so rescheduling never shortens how long a todo has left to live.
    check("expiry shifts by the same amount", t["expires"], "2026-04-01")

    make("resched-habit", start="2026-01-10T09:00:00", rrule="FREQ=DAILY")
    try:
        B._update("resched-habit", {"reschedule_to": "2026-03-01"})
        check("recurring reschedule is refused", "no error raised", "ValueError")
    except ValueError:
        pass


def test_urgent_leaves_no_residue():
    make("star-me", due="2026-12-01", category="errand")
    B._update("star-me", {"urgent": True})
    check("starred", B._serialize("star-me", B._load("star-me"))["urgent"], True)
    B._update("star-me", {"urgent": False})
    check("unstarring removes the key",
          "urgent" in B._load("star-me").metadata, False)


def test_day_ordering():
    """Starred first, then vital, then by clock. Ordering by time alone buried
    the things that matter at the bottom of a month cell."""
    day = "2026-11-05"
    make("ord-late", start=day + "T18:00:00", category="work")
    make("ord-early", start=day + "T08:00:00", category="work")
    make("ord-vital", start=day + "T17:00:00", category="work", vital=True)
    make("ord-star", start=day + "T19:00:00", category="work", urgent=True)
    got = [r["slug"] for r in B.occurrences(day, day, "live")
           if r["slug"].startswith("ord-")]
    check("starred, vital, then clock", got,
          ["ord-star", "ord-vital", "ord-early", "ord-late"])


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        try:
            t()
        except Exception:
            _failures.append(f"{t.__name__} raised\n{traceback.format_exc()}")
    shutil.rmtree(_TMP, ignore_errors=True)
    if _failures:
        print(f"FAILED {len(_failures)} check(s) across {len(tests)} tests\n")
        for f in _failures:
            print("  " + f)
        return 1
    print(f"ok, {len(tests)} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
