# outlets: brain -> world projections

The mirror image of inlets. Inlets ingest the world into the brain; **outlets
project the brain out to the world.** One-way renders only: the brain is the
source of truth, the external surface is a derived view and never writes back.
See `designs/outlets` in the brain.

## gcal.py and watch.py: deleted 2026-11-03
The one-way Google Calendar mirror (`gcal.py`) and its event-driven watcher
(`watch.py`) were retired 2026-09-23 (units stopped and removed, events backed
up to `.pulse/state/gcal-retired-2026-09-23.json`) and deleted from the repo on
2026-11-03. The calendar is the brain's dated todos, read directly by the
command dashboard's `/calendar` and the app, and ntfy carries the reminders.
Both files are in git history (last present at `741f596`) if a mirror or a
worked example of Google service-account auth is ever wanted again.

## remind.py - dated todos -> ntfy reminders
The whole reminder system. Todos created anywhere (WhatsApp bot, agents, MCP)
get reminders with no extra wiring, because todos are the SSOT.
`outlet-remind.timer`, exact-once state in `.pulse/state/reminders.json`.

**The policy is not here.** `brain_todos.ladder_for()` owns which lead times a
todo gets, trims included, and `brain_todos.expand_rrule()` owns recurrence.
Both are properties of a todo, and the calendar page renders the same ladder to
a human, so a copy in this file would drift from the one that actually fires.
This module only decides *when the rung lands* and sends it.

**Automatic and dynamic, not either or.** The category ladder is automatic and
has a floor: a dated one-off always ends up with at least two rungs, one far
enough out to act on and a per-category *reaction* rung (`REACTION`) that no
trim is allowed to remove. Money needs a day to move; a dishwasher needs five
minutes. On top of that, a todo may carry its own `reminders: [minutes]`, which
an agent or a human adds when something needs more notice than its category
gives. Those are **merged in, never substituted**: an override would let a
caller quietly drop a todo below the floor, which is the failure the floor
exists to prevent. Recurring todos are exempt from the floor, because the
recurrence is the redundancy.

Five behaviours worth knowing, each of which was a bug first:

- **A ladder, not a lead time.** Every dated todo gets a list of rungs by
  category: `chore` 1h and 5min, `family`/`finance` 3d and 1d, `travel` 3w, 1w,
  1d, `study`/`deadline` 4w down to 1d. Anything in the todo's own `reminders`
  list is added to that, so rent gets a week's notice on top of finance's 3d
  and 1d by carrying `reminders: [10080]`.
- **Rungs are trimmed to what the todo could use.** A rung already past when
  the todo was created never fires (booking a trip two weeks out must not fire
  the three-week rung on day one), unless that empties the ladder, in which
  case the nearest rung survives so a same-day todo still pings. An all-day
  thing keeps only day-scale rungs plus a morning-of one. A recurring thing
  keeps only rungs shorter than its own gap, or a 4-week warning about a daily
  habit fires 28 times.
- **All-day rungs land in the morning, spread.** A date means midnight, which
  is a useless time to ping, so day-scale rungs snap to 09:00 plus a stable
  0-49min offset derived from the slug. Without the offset, everything due
  tomorrow arrives in one 09:00 clump, which is what the old two-stage version
  did and why the pings read as spam.
- **Vital todos get three rungs and a loud last one.** `vital: true` is a
  separate axis from priority: P0 means "this matters most", vital means
  "missing it is not recoverable". It raises the floor from two rungs to
  three, and its final rung goes out at `critical` (max ntfy priority, titled
  "Vital reminder") instead of joining the day's quiet pings. That final rung
  is always the reaction rung, the minimum warning that still leaves time to
  act, which is exactly the one worth breaking through.
- **Nothing fires in the small hours.** A rung computing to between 23:00 and
  07:00 moves to 07:30, and two rungs that collapse onto the same minute after
  snapping send once, not twice.
- **A ladder is trimmed to the notice the todo actually had.** A thing created
  this afternoon cannot have been warned about four weeks ago. Rungs longer
  than the available lead are dropped, then topped back up by halving the time
  that does exist, so a vital todo set up the day before still gets three
  warnings rather than silently dropping to one.
- **A recurring ladder is bounded by its gap, and never topped up.** Two
  separate paths broke this in Sep 2026 and both shipped: the floor top-up,
  and then the lead-trim top-up. The lead to a far-off occurrence is long even
  when the gap is one day, so the second one warned about a *daily* walk four
  days ahead and a weekday standup ten weeks ahead. Neither top-up may run
  when `interval_min` is set. `tests/test_todos.py` pins this.
- **A missed reminder is sent late, not dropped.** Each rung carries a
  `stale_after` moment; only a rung that is both stale and irrelevant (its
  event is over) is dropped. Everything else late in a pass is rolled into a
  single "Catching up" digest, not one ping per missed rung.
- **Changing the rung format replays history.** State keys are
  `{occurrence}|{rung_minutes}`, so a new ladder means new keys and every past
  rung looks unsent. Seed the state file before deploying a change to the
  ladder, as the Sep 2026 rewrite did, or the first pass fires months of
  backlog at once.

## Tests
`.venv/bin/python -m tests.test_todos` (no pytest, it is not in the venv and
this does not need one). Every case in there is a bug that shipped. This code
fails silently by nature: nothing raises, a ping just never arrives or arrives
about the wrong thing, and a week goes by. Run it after touching
`brain_todos.ladder_for`, `expand_rrule` or `occurrences`.

## A note on ownership
`.pulse` is written by `brain-mcp` running as the **vault user**, but
`pulse-cron-dispatch` runs claude as **root**, so anything a cron writes lands
root-owned and every later `pulse_note` fails silently. The dispatcher unit
carries an `ExecStartPost` chown to hand the tree back. If pulse writes start
failing, check ownership before anything else.
