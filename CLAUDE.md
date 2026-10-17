# g-brain

Personal knowledge base tooling: MCP server, REST API, brain-commit, codebase search, and layer 3/4/5 sync.
GitHub `Volcanex/g-brain` (was `gbrain`). An instance checks it out beside its instance repo.
Older units may run it from a compat symlink named `brain-tools`.

**Host:** the instance's brain host (`BRAIN_HOST`), systemd service `brain-mcp` on port 8766
**Data root:** `~/brain/` (private, not in this repo)

## Components

The repo holds six components and one shared config module. Each is a package, a module or a
directory, and each may import only what its last column lists. `tests/test_boundaries.py`
checks every import against that column, so a new cross-component import fails the suite
until the table changes with it.

| Component | What it does | Code | Units | May import |
|---|---|---|---|---|
| shared config | Says where things live: the data root, state dirs, the `.env` chain, the manifest source. Values are read from the environment when used, not fixed at import | `g_config.py` | none | nothing local |
| g-spine | Gets alerts to the owner's phone: `send()` to ntfy with dedup, the hook that pages when a unit fails, and the L5 watcher that probes every service and dependency | `g_spine/` | `brain-status.timer`, `g-alert@.service` | config |
| g-todos | Dated commitments (L6): storage in the vault's `todos/`, recurrence, the reminder ladder, the expiry sweep. Not packaged yet, so it is still flat | `brain_todos.py`, `outlets/` | `brain-todo-expire.timer`, `outlet-remind.timer` | g-spine, config |
| g-pulse | The live layer, what is true now: the pulse page, inlet records, briefs, the pantry, and the inlets that bring outside data in | `g_pulse/`, `inlets/`, `pulse` | `inlet-subs.timer` and `pulse-cron-dispatch.timer` (both defined in g-env-linux) | g-todos, g-spine, config |
| g-brain corpus | The long-term notes: versioned writes, indexes, the reference graph, sources, code search, the verify and condense sweeps, nerves and lineage from GitHub, the session miner, vault sync | `g_brain/` (not the host files), `sync_vault.sh` | `brain-vault-sync`, `brain-gmap`, `brain-miner`, `brain-verify` and `brain-condense` timers, and the crontab's `brain_refs.py all` | g-spine, config, and only `append_page` and `PAGE_PATH` from g-pulse |
| g-brain host | The one app on :8766. `/mcp` serves agents (FastMCP, OAuth, Access) and `/api` serves web UIs. It mounts the tools of every other component | `g_brain/server.py`, `api.py`, `oauth.py`, `access.py`, `surface.py` | `brain-mcp.service` | everything |
| g-voice | The WhatsApp and web assistant. It calls the corpus, todos and pulse in process rather than through the MCP | `voice/` | `voice.service` | g-brain corpus, g-todos, g-pulse, g-spine, config |

Five private todos names are still used from g-pulse until g-todos grows a public API. They
are listed in `ALLOWLIST` in the boundary test, which fails when an entry stops being used.
A component can move to its own repo only once nothing on that list points at it.

### Old module names still work

Each moved module left a short alias at its old path, so `brain_alerts` is `g_spine.alerts`,
`brain_pulse` is `g_pulse.pulse`, `brain_reader` is `g_brain.reader` (and so on for every
`brain_*` corpus module), and `brain` is `g_brain.server`. The alias swaps itself for the real
module in `sys.modules`. Both names give the same object, so `patch.object` on either works,
and `python brain_status.py`, `python -m brain_alerts`, `uvicorn brain:app` and
`-m unit_alert` run unchanged. That is why no unit, cron, skill or wrapper had to change.

Code inside the repo still imports the old names. Switch those first, then the callers
outside (the units and crontab, the `brain-condense` skill, the `pulse` wrapper, g-env-linux's
pulse-cron-dispatch, push-secrets and the vault's `.pulse/crons`), and only then delete an
alias. `voice/`, `inlets/` and `outlets/` stay where they are: push-secrets, a g-env-linux
unit and the vault crons name those paths.

The disabled legacy `brain.service` runs `fastmcp run brain.py:mcp`. fastmcp loads that file
directly, so it gets the alias module, which has no `mcp`. Point it at
`g_brain/server.py:mcp` before ever starting it again.

## Modules

| File | Role |
|------|------|
| `g_config.py` | Shared paths and the `.env` chain, anchored on the repo root so a module's location never changes what it resolves. It finds the instance (manifest and passport) as gp-core beside the repo, or wherever `G_INSTANCE` points, and names the instance repo from that checkout's origin remote unless `G_INSTANCE_REPO` does |
| `g_brain/server.py` | ASGI entry point: FastMCP at `/mcp`, REST API at `/api`, mounts all sub-MCPs, locks write surface |
| `g_brain/api.py` | REST face: `create_api()` builds the `/api/*` FastAPI app for web UIs |
| `g_brain/reader.py` | Shared read-only data layer over the notes filesystem + refs DB |
| `g_brain/commit.py` | Versioned note writes: `brain_note` (the front door) + propose/execute/supersede/mark_broken, and the `stale_dep` blast-radius stamp |
| `g_brain/verify.py` | Rewrite-safety sweep: resolves `stale_dep` flags on a daily timer, escalates the rest |
| `g_brain/condense.py` | Condensation detector: finds outdated, overlapping, accreted and orphaned notes. Reports only, never writes |
| `g_brain/codex.py` | `search_code` tool: semble semantic+lexical search across the repos mapped into `nerves/` |
| `g_brain/gmap.py` | Layer 3+4 sync: crawls GitHub → brain/nerves/ and brain/lineage/ |
| `g_spine/status.py` | Layer 5: probes the manifest's services + the dependency catalog, forwards service `/health` checks, alerts the spine on transitions, writes brain/status/snapshot.json |
| `g_spine/alerts.py` | The alert spine: `send()`/`brain_alert` → ntfy, dedup/cooldown; the one channel for things that must reach the owner's phone |
| `g_spine/unit_alert.py` | The `OnFailure=g-alert@%n.service` hook: one alert per failed unit, with its journal tail |
| `g_brain/miner.py` | Session miner: scans finished Claude transcripts (this host's `~/.claude/projects`, plus one rsync-staged tree per remote host under `MINER_STAGING`, default `~/.cache/brain-miner/`) and runs a headless pass that writes `brain_note`s with `actor=ai-auto`. systemd `brain-miner.timer` runs it every 6h, with its ledger in `.pulse/state/miner-ledger.json`. Runs as **root**. Root's claude login is the one interactive use keeps fresh |
| `g_brain/capture.py` | What the miner's headless pass calls (`-m brain_capture`) to search and write notes, and to add one line to the pulse page |
| `g_pulse/pulse.py` | The pulse layer: page, inlet records, briefs, pantry, `/api/pulse/*` and the `pulse_*` MCP tools. Other components use only `append_page`, `record_state` and `composed_payload` |
| `inlets/gmail_fetch.py` | Dumb IMAP reader behind the gmail pulse inlet (app password in `outlets/.env`, BODY.PEEK read-only); judgement lives in `.pulse/crons/pulse.gmail.distil.md` |
| `outlets/remind.py` | ntfy reminders for dated todos (day-before + morning-of for a `due`; `remind_min` before a timed `start`, defaulting to 30 min, **not** gcal's all-day 900), `outlet-remind.timer`; exact-once state in `.pulse/state/reminders.json`. A missed stage is sent late rather than dropped, until the event it points at has passed |
| `brain_todos.py` | Layer 6 commitments. Also the expiry sweep (`python brain_todos.py [--dry-run]`, `brain-todo-expire.timer` daily 07:25): open TODOs past `expires` become `status: expired`, never deleted. `expires` defaults at creation from priority - P0/P1 never expire, P2 60d, P3 21d, a dated one 30d past its `due` - because most TODOs arrive from the WhatsApp agent as perishable reminders and nothing closed the loop (77 open vs 30 ever done on 2026-09-08). **Priority beats the date**: a P0 a month overdue is the loudest thing the list can say, so retiring it would delete the signal |
| `g_brain/index.py` | Index rebuilder |
| `g_brain/refs.py` | Reference graph maintenance |
| `g_brain/source.py` | Source ingestion |
| `g_brain/oauth.py` | OAuth provider for MCP auth |
| `g_brain/surface.py` | What `/mcp` exposes of basic-memory: the proxy, pinned to the `initialize` handshake because fastmcp 4 otherwise mirrors a stateless 2026-07-28 caller onto a backend that may not speak it, and the write-surface lock (`server.disable`, no try/except: a lock that silently fails exposes raw `write_note`). basic-memory 0.23's `search` refuses non-OpenAI clients; point models at `search_notes` |
| `g_brain/access.py` | Signed Cloudflare Access assertion verification for explicitly enrolled headless clients; hosted clients retain OAuth |

## Run

```bash
cd ~/g-brain
source .env   # GITHUB_TOKEN, BRAIN_ROOT
.venv/bin/uvicorn brain:app --host 127.0.0.1 --port 8766
```

Managed by systemd. Restart with `systemctl restart brain-mcp`. One process serves both faces: `/mcp` (agents, via the cloudflared tunnel) and `/api/*` (web UIs, via nginx at the instance's API hostname).

`deploy/systemd/` holds example units for a new install: the brain host, voice, the
probes and reminders with their timers, the `g-alert@` failure pager, and the timed sweeps
(condense, gmap, miner, todo expiry, vault sync, verify). They use the
package module names and `@NAME@` placeholders, which each file's header explains. An
instance that already runs older copies with real paths should not install these over them.

## Tests

`tests/` runs under pytest, from the `test` extra in `pyproject.toml`. The live venv does not
have pytest, and nothing gets installed into it, so put pytest on `PYTHONPATH` from a scratch
directory. `tests/conftest.py` points `BRAIN_ROOT` and `VOICE_TRANSCRIPT_DIR` at a temp dir
before any test module loads, and fails every test while an imported repo module still holds
a path inside the live vault. `tests/test_guard.py` proves it trips, in a separate pytest run
over a copy of the conftest.

That guard exists because modules fix their paths when first imported. A test once imported
a voice module at collection, which bound `brain_todos` to the live vault, and the todos
tests then wrote 14 fixtures into the live vault's `todos/`. Import brain and voice
modules inside a test or fixture, never at the top of a new test file. Never set
`BRAIN_ALERTS_DRY_RUN` for the whole suite. The alert tests patch it where they need it.

## Layer sync

```bash
GITHUB_TOKEN=$GITHUB_TOKEN BRAIN_ROOT=~/brain .venv/bin/python3 brain_gmap.py
```

Cron: `@reboot` (30s delay) + daily 6am. Writes `~/brain/nerves/` and `~/brain/lineage/`.

Each `nerves/{id}/.meta.json` carries a `nerves` list: one entry per directory
holding a CLAUDE.md or an AGENTS.md, with each file's git `mode` and blob `sha`
and a derived `pair` state (`symlink`, `copy_synced`, `copy_drift`,
`claude_only`, `agents_only`). Mode `120000` is a symlink and equal shas mean no
drift, so both come free from the tree already fetched. Never add per-file
requests for this. `/api/nerves` builds the directory web from that list and
falls back to an on-disk CLAUDE.md scan (`pair: "unknown"`) for a repo synced
before the field existed, which is how repos dropped from the instance manifest
still render.

Layer 5 (live status) runs separately: `brain-status.timer` fires `brain_status.py` every 5 minutes, writing `~/brain/status/snapshot.json`. Served at `/api/status` and via the `service_status` MCP tool.

The local `basic-memory` health row also checks one representative file against
its indexed SHA-256 and checks that watcher state is readable/running. Configure
`BRAIN_MEMORY_DB`, `BRAIN_MEMORY_CANARY`, `BRAIN_MEMORY_WATCH_STATUS` and
`BRAIN_MEMORY_FRESHNESS_GRACE_SECONDS` (120s default) for a different installation.
It opens SQLite read-only with bounded queries. Mismatch outside the sync grace
or empty/stopped watcher state warns through the existing alert spine; unsupported
schemas report unknown. This detects ingestion drift, not full-corpus completeness
or embedding/ranking quality. A quiet watcher's old last_scan is not an outage.

## The front door, and why writes are cheap now

`brain_note(text, kind, target=None)` is the default write path: one call, one
fact, no plan_id. It wraps the same versioned machinery (`_apply_op` →
versioning, lineage, `.history/` snapshot, commits.log) and rebuilds the
indexes, then returns. On an append, supply `summary=` with a current summary
of the whole note. Without it, the previous summary stays visibly marked as
needing review; the response returns the persisted title.

It exists because cost, not policy, was what kept the brain empty. The manual
had said "capture autonomically, don't wait to be asked" since the start and
the brain still took **2 commits across 98 sessions** (2026-07-28 → 08-17),
while `pulse_note` (one string, one call) got used constantly. So the fix was
to make a durable capture cost what a pulse note costs.

`brain_note` can only **create or append** (a second capture on an existing
slug thickens the note rather than colliding, which is what makes it safe to
reach for reflexively). Anything that *destroys* knowledge goes through
`propose_commit` → `execute_commit`. That fence is the whole safety argument
for making the additive path frictionless.

### Provenance vs permission

`actor` used to mean both "who wrote this" (lineage) and "who may do this"
(authorisation), which left a confident agent one route to a supersede: pass
`actor="human"` and lie about the note's lineage. Split as of 2026-08-17:

- **`actor`**: provenance. `human` (the owner dictated it), `agent` (the model
  composed it) or `ai-auto` (cron, nobody watching). Sets lineage; record honestly.
- **`assent`**: authorisation for one destructive op. Set when the owner was
  asked and said yes.

`_authorise()` is the single gate: additive ops need neither field; `supersede`
and `edit_major` need `assent` from a human-in-the-loop actor; `ai-auto` is
refused outright regardless of assent, because there is nobody there to ask.
`mark_broken` is deliberately *not* fenced: flagging a note as no-longer-true
destroys nothing, and an unflagged stale note is the more expensive failure.

Reading is free too: `/api/orient?cwd=&project=&task=` renders bounded,
project/task-ranked session-start orientation
block (`render_orientation()` in `brain_api.py`), injected by g-env's
`pulse-inject.sh` hook alongside the pulse page.

## Rewrite safety: stale_dep and the verifier

Habitual, unattended writing moves the guard from before the write to after it:

1. **Blast radius**: any structural write (`supersede`, `mark_broken`,
   `edit_major`, or a wholesale `body` replacement) calls `_stamp_dependents`,
   which resolves the wikilink graph via `brain_reader.dependents()` and stamps
   each dependent with `stale_dep: [{note, since, reason, stamp}]`. Weaker than
   `status: broken`. It means *unverified*, not *wrong*. `indexes/`,
   `versions/` and `archive/` are excluded: they can't go stale.
2. **The sweep**: `brain_verify.py` (daily, `brain-verify.timer`) clears a flag
   **only** on explicit dependency-specific review evidence from
   `review_dependency(note, dependency, expected_stamp, reason, actor)`. Read
   both notes first. The stamp must match the current flag (`since` for legacy
   flags); each new dependency rewrite changes it. Unrelated edits cannot clear
   flags, and unattended actors cannot submit reviews. The sweep only updates
   flag metadata; it never judges whether a note holds. Everything else stays flagged
   and pages the spine past 14 days.
3. **The queue**: `/api/stale` serves the report; the orientation block counts
   outstanding flags.

The restraint in (2) is deliberate and load-bearing. A verifier trusted to
judge "does this still hold?" would clear everything, look green, and let the
corpus rot behind it, so a run clearing an entire non-trivial batch fires a
`suspicious_full_clear` warning at the spine. The watcher reports on itself
because nothing else is watching it.

## Condensing (the opposite pressure)

Habitual capture keeps the brain full; nothing keeps it *readable*, and an
unreadable corpus goes unread and then unwritten. `brain_condense.py`
(`brain-condense.timer`, daily) finds five kinds of debt and **never writes**:

- **`stale`**: the one that matters. A note can be well-formed, well-linked
  and unique while being simply wrong. Checked mechanically against things that
  are their own SSOT: projects the manifest marks fossil/deprecated (matched only in
  *technical* context: a bare word-boundary match on `period` flagged "a
  period of time"), paths under the home directory that are no longer on disk, and the
  manifest's `retired_terms` (a `term` and a `reason` each). Age alone is never proof; age *plus* a contradiction is.
- **`overlaps`**: pairs sharing token territory. Whole-body Jaccard runs
  median 0.041 on this corpus, so ≥0.20 is strong. It measures shared ground,
  not duplication: the top pairs are legitimately separate notes.
- **`accreted`**: ≥3 dated appends. The specific debt `brain_note`'s append
  path creates: right for capture, wrong for reading.
- **`orphans`**: no link in or out. Usually a missing wikilink, not a dead note.
- **`drift`**: non-canonical `kind`, missing `summary`. Narrow on purpose:
  flagging "no tags" caught 47 of 84 notes and would have buried the three real
  problems.

The writing half is the `brain-condense` skill, run with the owner in the
session. That split is forced by the fence and is the right shape anyway: a
detector that never writes can't damage anything, so it can run unattended and
be trusted, while the judgement happens where someone can be asked.

## Alerting & dependencies (the spine)

`brain_alerts.py` is the single alert spine: `send(level, source, msg, dedup_key=)` → ntfy, deduped so a stuck failure pings once per cooldown (`clear()` resets on recovery). Callable in-process, as the `brain_alert` MCP tool, and via CLI (`python -m brain_alerts ...`). **No REST route**: the `/api` face is public, so an open push endpoint would be a spam vector. `NTFY_URL` resolves from the live env then the known `.env` files, anchored off the module path (brain-mcp runs as **root**, so `Path.home()` is wrong). The phone must subscribe to the ntfy topic.

`brain_status.py` probes every manifest service **and** the dependency catalog (`manifest.json → dependencies`), and alerts the spine on state **transitions** (down/warn/recover), deduped, **tiered by `criticality`** (`nice` warns, vital/important down is loud). The morning brief (pulse compactor) also pushes through the spine and is the dead-man's switch: its absence is the signal.

Dependency liveness resolves in order: a catalog dep with `"health": {"service","check"}` → read that service's `/health` (creds stay in the repo, brain just forwards) → else its central `probe` → else `"declared, no probe configured"`. The catalog, the `/health` contract, and the graded-compliance rules live in the instance's `conventions.md`.

## Vault sync (the iPad)

The vault (`BRAIN_ROOT`) is a git repo pushing to a private remote the
instance chooses. Content only: `g-brain` is the code. It exists
so Obsidian on the iPad (via Working Copy) can hold a working copy and send
edits back; Excalidraw drawings land in the vault as ordinary files.

`sync_vault.sh` runs from the instance's `brain-vault-sync.timer`, which
`deploy/systemd/` has no example of (every 10min, **as the user that owns the
vault**). It commits whatever the MCP has written, rebases onto anything the
iPad pushed, and pushes. A rebase that conflicts is aborted
and reported, never resolved by guessing: losing a note to an automated merge
is worse than a loud failure.

That loud failure now reaches the phone: the unit carries
`OnFailure=g-alert@%n.service`, and the script clears the dedup key on every
clean run so the next wedge alerts immediately rather than dying in the
six-hour cooldown. Before this, a conflicted rebase stopped the vault silently
and you found out by noticing notes weren't appearing.

Gitignored, and each for a reason that will bite if undone:
`.oauth-clients.json` / `.oauth-tokens.json` are **live session-granting
credentials** and must never reach GitHub or a synced device; `.pulse/` is
rewritten continuously and would conflict on nearly every pull (read it via
gcommand's `/pulse`); `.refs-index.sqlite` (and its markdown face
`.refs-index.md`) is derived on the brain host and regenerated wholesale, so syncing it only
gives every device a diff it can never author; `.quarantine/` is supersede
machinery, not notes; `.obsidian/` is per-device UI state, and `workspace.json`
inside it is the one genuinely multi-writer file a shared vault produces.

Still tracked, deliberately: `commits.log` is the commit audit trail and is
**not** regenerable, so git is its durability: churn is the right price.
`indexes/` is derived but only the brain host writes it, so it costs diff noise rather than
conflicts, and untracking it would break wikilinks in Obsidian on the devices.

The vault is **two-writer**: the MCP here, Obsidian there. That is the standing
risk in this arrangement: the timer narrows the window to 10 minutes but cannot
close it. `gsync` does not touch this repo (its `REPOS` list holds only the
instance, framework and g-env checkouts), so there is exactly one mechanism syncing it.

Push credentials for the vault user live in that user's `.git-credentials`
(mode 600), pinned by an explicit `credential.helper store --file=` in the repo
config so it resolves regardless of `HOME`: systemd oneshots are not given one.

## Secrets

`.env` is gitignored. The instance's `push-secrets` (run by g-env's `ae-deploy`)
writes the keys it owns on every host and keeps the host's other lines, so a host key
added by hand survives a push. Required vars:
- `GITHUB_TOKEN`: GitHub PAT for private repo access (brain_gmap, brain_codex, brain_status)

The framework ships no instance values. These come from the live environment or
the `.env` chain (this repo's `.env`, `voice/.env`, then the instance passport).

Host keys go in each host's own `.env`. The passport reaches every machine, and a Mac
shell exports it, so a host path or host name there would follow it everywhere:
- `BRAIN_ROOT`: the vault (default `~/brain`). Through the chain, a root shell with
  no `BRAIN_ROOT` reaches the vault this host's `.env` names, as the units do.
- `BRAIN_MEMORY_DB`: basic-memory's index (default `~/.basic-memory/memory.db`).
- `BRAIN_HOST`: the manifest id of the host the brain runs on. Unset means every
  service is local.
- `MINER_STAGING`, `G_AGENT_RUN`: the miner's staging root (default
  `~/.cache/brain-miner`) and agent runner (default g-env's, beside this repo).

Instance-wide keys go in the passport:
- `MCP_PUBLIC_URL`: the public https URL of `/mcp` and the OAuth issuer. Unset, the
  issuer is `http://127.0.0.1:8766`, so only local clients sign in, and the server
  says so at start.
- `BRAIN_CONTACT`: a URL appended to the `g-brain/<version>` User-Agent.
- `BRAIN_REVIEW_URL`, `VOICE_PUBLIC_URL`: where stale-dependency alerts point, and
  the referer voice sends to OpenRouter. Both are left out when unset.
- `BRAIN_MEMORY_CANARY`: the vault-relative file the search-freshness check hashes.
  Unset, the check reports unknown.

`G_INSTANCE_REPO` is read from the live environment only.

Headless MCP clients use a dedicated Cloudflare service token on the instance's
Access-protected `/mcp` hostname. The origin also verifies the signed Access
assertion: `BRAIN_ACCESS_ISSUER`, `BRAIN_ACCESS_AUDIENCE` and space-separated
`BRAIN_ACCESS_CLIENT_IDS` configure its issuer, application audience and explicit
service Client ID allowlist. All three are required; otherwise only OAuth works.
Never enroll the public OAuth Worker's upstream service token: every anonymous
request through that Worker carries it. Access policy admission alone is not
origin authorization. Existing OAuth bearer headers take precedence, and invalid
bearers do not fall back to another identity. Tests: `.venv/bin/python -m unittest
tests.test_access_auth`.

Protocol eras: fastmcp 4 serves the `initialize` handshake (2025-06-18,
2025-11-25 and earlier) and the stateless 2026-07-28 envelope from the same
`/mcp`, with no flag. `tests.test_mcp_transport` holds all three, plus the
OAuth discovery documents claude.ai reads and the redirect allowlist.
