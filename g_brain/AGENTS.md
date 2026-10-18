# g_brain: the corpus and the host (g-brain)

Two components share this package.

**The corpus** is the long-term brain: notes that stay true. `reader`, `commit`, `index`,
`refs`, `source`, `codex`, `verify`, `condense`, `gmap`, `miner` and `capture`. It imports
g-spine and `g_config`, and from g-pulse only `append_page` and `PAGE_PATH` (capture adds
one line to the page and checks it is not already there). It never imports the host,
todos or voice.

**The host** is the one app on :8766 that serves every component: `server` (FastMCP at
`/mcp`, FastAPI at `/api`, the mounted sub-servers and the write-surface lock), `api`,
`oauth`, `access` and `surface`. It is the composition root, so it may import anything, and
nothing else may import it.

What each module does: the Modules table in the root `CLAUDE.md`. The boundary rules are
checked by `tests/test_boundaries.py`.

## Running
- `brain-mcp.service` runs `uvicorn brain:app`, and `brain` is an alias for `g_brain.server`,
  so the unit did not change. Before restarting it after a change here, list the MCP tools
  and compare them with the previous list: clients see any rename at once.
- Corpus timers run `python brain_gmap.py` and `brain_miner.py --limit 3`, and the crontab
  runs `brain_refs.py all`. The root aliases remain available.
- `review_dependency` records evidence and clears matching stale flags in one versioned
  write through `verify.resolve_note`. Stale reports and orientation never rewrite notes
  or send alerts. `python brain_verify.py` can apply legacy evidence and write
  `status/stale.json`. Only that manual sweep warns on a suspicious full clear.
- Condensation is computed on read and cached until kept notes, the manifest or the date
  change. `python brain_condense.py` remains a manual report writer. Neither detector
  needs a timer.
- `commit.rebuild_indexes()` runs `python -m brain_index` with its working directory set to
  the repo root, so a write from any caller rebuilds the indexes.
- Every module reads `BRAIN_ROOT` when it is first imported. Set it before importing, and in
  tests let `tests/conftest.py` point it at a temp dir.

## Captures and host routes
- `brain_note(text, layer, bar=...)` captures design, idea, about or hazard knowledge.
  About notes have `kind: fact`. New notes need the one-line value and uniqueness bar.
  Appends need no bar. Duplicate candidates must name existing appendable Markdown files.
- The miner's `capture.apply` accepts legacy kinds design, idea, fact and hazard without
  a bar by supplying a session-transcript bar. Ambiguous legacy kinds are skipped.
- `create_api()` mounts todos, tickets and pulse routers. The host mounts `brain_todos.mcp`,
  which registers ticket tools from `g_todos.tickets` as well as owner commitments.
- Corpus writes refuse `todos/` and `tickets/`, including resolved path aliases.
  Use `create_todo/update_todo` or `create_ticket` for those stores. Commit plans
  check every target before writing any operation.
