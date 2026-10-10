# g-brain

g-brain is the tooling around a Brain, the private git repo of markdown notes that a G-System
instance keeps as its long-term memory. One process serves the Brain to agents as an MCP
server at `/mcp` and to web pages as a REST API at `/api`. The same repo holds the parts that
work on the notes behind that server: versioned writes, indexes, a reference graph, code
search, and the sweeps that flag a note when something it stands on has changed. Beside the
corpus sit the alert spine (what must reach a phone goes through ntfy), the pulse layer for
what is true now, dated todos with reminders, and a WhatsApp assistant that answers from the
Brain.

## Point it at a Brain

Settings come from the environment first, then from a chain of `.env` files: this checkout's
`.env`, `voice/.env`, then the instance's passport. Each host names its own vault in its own
`.env`, because the passport reaches every machine:

```sh
BRAIN_ROOT=/path/to/brain
BRAIN_MEMORY_DB=/path/to/.basic-memory/memory.db
```

`BRAIN_ROOT` defaults to `~/brain`. The instance repo (its `manifest.json` and `.env` passport) is
found beside this checkout under the directory name `gp-core`, or wherever `G_INSTANCE` points. Instance-wide
settings, such as the public URL of `/mcp`, go in the passport.

## Run it

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/uvicorn brain:app --host 127.0.0.1 --port 8766
```

`deploy/systemd/` has example units for the server, the voice service, the probes and
reminders, and the pager that fires when a unit fails. Fill in their `@NAME@` placeholders
before installing them. The tests run under pytest from the `test` extra, and they point
`BRAIN_ROOT` at a temp dir, so they never touch a real vault.

## Docs

`CLAUDE.md` is the full reference: the components and which may import which, every module,
the layer sync, rewrite safety, condensing, alerting, vault sync and every setting with the
file it belongs in. `g_brain/`, `g_spine/`, `g_pulse/`, `inlets/`, `outlets/` and `voice/`
each have their own `CLAUDE.md` with the detail for that part.

## Licence

Copyright (C) 2026 Gabriel Penman. Licensed under the GNU Affero General Public License v3.0 only, see `LICENSE`.
