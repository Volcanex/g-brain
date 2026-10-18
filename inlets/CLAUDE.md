# inlets: world -> brain ingestion

The mirror image of outlets. Outlets project the brain out to the world.
**inlets bring the world into the brain.** An inlet writes brain state and
never the other way round.

## gmail_fetch.py
Pulls inbox metadata for the pulse layer.

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
