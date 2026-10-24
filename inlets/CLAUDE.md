# inlets: world -> brain ingestion

The mirror image of outlets. Outlets project the brain out to the world.
**inlets bring the world into the brain.** An inlet writes brain state and
never the other way round.

## gmail_fetch.py
Pulls inbox metadata for the pulse layer.

## gmail_apply.py
Lands a distil run's todos, records the snapshot, then acks the fetch cursor. A
transient failure (the API down, a 5xx) exits before the ack so the mail comes back.
A refusal by the owner-action boundary (a 400) is final, so it never blocks the ack:
project and agent work becomes a ticket with the same `source_key`, a machine alert is
dropped, and the snapshot lists both under "Not made into todos". Bodies over 800
characters are trimmed before the write rather than refused. Until 2026-10-24 one
refused todo failed the whole run and the cursor never moved, so the same mail came
back and was refused every hour. That fits the inlet's `last_run` stopping on
2026-10-18, the day the boundary landed.

## subs.py: recurring charges -> dated todos
`gfin subs` (in g-finances) knows what recurs and when it next bills and this
turns each row into a todo and stops there. The `finance` reminder ladder
already warns 3 days and 1 day ahead, which is the notice needed to move money
before a charge lands, so there is no second alerting path to keep working.

- **Idempotent by slug** (`sub-<id>`). A run rewrites the same todos rather
  than accumulating, which is the whole reason the slug is derived rather than
  generated. Reads and writes use public `brain_todos.get` and `upsert`. Each write
  records `inlets.subs` as requester and composer and `gfin-sub:<id>` as both source
  and `source_key`, through the shared owner-action boundary.
- **Never reopens a closed todo.** If the owner marked one done or cancelled, the
  inlet leaves it alone. Reopening closed work is the one behaviour that would
  make this a nuisance rather than a tool.
- **No date, no todo.** A subscription the ledger has not seen gets nothing.
  `gfin subs` deliberately returns no date for those, and a reminder on a
  guessed billing day trains you to ignore the channel.
- `status: cancel` in the registry makes a one-off P1 to go and cancel the
  thing, instead of a recurring reminder to fund it.
- Lapsed subscriptions are marked expired. A real charge returning may reopen an expired
  inlet todo, while manually closed states stay closed. General todo expiry is derived
  on read and needs no expiry timer.

`inlet-subs.timer`, daily at 08:10, after the Era ingest so it reconciles
against a ledger that already holds yesterday's charges.
