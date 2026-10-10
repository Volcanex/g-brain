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
- The corpus timers run `python brain_verify.py`, `brain_gmap.py`, `brain_miner.py --limit 3`
  and `brain_condense.py`, and the crontab runs `brain_refs.py all`. All are aliases at the
  repo root, so they keep working from any directory.
- `commit.rebuild_indexes()` runs `python -m brain_index` with its working directory set to
  the repo root, so a write from any caller rebuilds the indexes.
- Every module reads `BRAIN_ROOT` when it is first imported. Set it before importing, and in
  tests let `tests/conftest.py` point it at a temp dir.
