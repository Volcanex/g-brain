"""brain: unified MCP server.

Mounts brain-commit's tools natively and proxies basic-memory over HTTP.
Clients see one MCP server with both toolsets, plus the operating manual
(CLAUDE.md) injected as MCP `instructions` and exposed via a `brain_help`
tool so models know how to use the brain without the user having to brief
them every session.
"""

from __future__ import annotations

import os
import sys

from fastmcp import FastMCP

import g_config
from brain_commit import mcp as commit_mcp
from brain_codex import mcp as codex_mcp
from brain_todos import mcp as todos_mcp
from brain_pulse import mcp as pulse_mcp
from brain_alerts import mcp as alerts_mcp
from brain_oauth import build_provider as build_oauth_provider
from brain_surface import BLOCKED_TOOLS, lock_write_surface, proxy_basic_memory

BRAIN_ROOT = g_config.BRAIN_ROOT
# The OAuth issuer. Unset, the issuer is the server's own port, so only local clients sign in.
MCP_PUBLIC_URL = g_config.env("MCP_PUBLIC_URL")
OAUTH_CLIENT_STORE = BRAIN_ROOT / ".oauth-clients.json"
BASIC_MEMORY_URL = os.environ.get("BASIC_MEMORY_URL", "http://127.0.0.1:8765/mcp")
MANUAL_PATH = BRAIN_ROOT / "CLAUDE.md"
MASTER_INDEX_PATH = BRAIN_ROOT / "indexes" / "master.md"


def load_manual() -> str:
    """Read the operating manual. Falls back to a minimal stub if missing."""
    if MANUAL_PATH.exists():
        return MANUAL_PATH.read_text()
    return (
        "# Brain: operating manual missing\n\n"
        f"Expected at: {MANUAL_PATH}\n"
        "Without it, the model has no conventions for using this brain. "
        "Restore CLAUDE.md and reconnect."
    )


def load_master_index() -> str:
    """Read the master content index, or empty string if not yet compiled."""
    if MASTER_INDEX_PATH.exists():
        return MASTER_INDEX_PATH.read_text()
    return ""


# Default MCP instructions are a slim pointer, not the full manual: most
# sessions only need the routing rule + the pulse. brain_init still returns
# the full manual + master index for actual knowledge-base (cortex) work.
_POINTER = """\
# Brain: quick orientation

Memory routing rule: **has a date and is Gabriel's to do -> `create_todo`; \
machine or agent work -> `create_ticket`; true now -> `pulse_note`; \
true always -> `brain_note(text, layer, bar=...)`**.

Capture freely during the work when a future agent would be worse off without it \
and it is recorded nowhere else. Choose a layer:
- `design`: Gabriel's own designs, the main job.
- `idea`: cross-domain ideas no project owns yet.
- `about`: facts about Gabriel, preferences, people, decisions and their reasons.
- `hazard`: ops hazards and external quirks that are not gates yet.

Project canon belongs in the project's repo docs. Rules checked every run belong \
as gates in g-levels (`glevels`, `/home/gabriel/g-levels`). Standing directives get \
an advisory pointer there. `nerves/` is read-only, synced from GitHub.

Search with `search_notes` before writing. A new note needs a one-line `bar=` \
answer: would a future agent be worse off without this, and is it recorded nowhere \
else? Near-duplicates return up to three candidates. Thicken with `target=<path>` \
or use `distinct=True` if it really is separate. Appends, including an existing \
slug, need no bar. Supply `summary=` for the current whole note, otherwise an \
append marks the old summary for review. `kind=design|idea` is a deprecated alias.

`supersede` and transformative `edit_major` need the owner's yes: explain what \
changes and why, then set `assent=True`. `actor` records who wrote it, not who \
approved it. Unattended `ai-auto` cannot do either. `mark_broken` is unfenced.

Structural writes use `propose_commit` -> `execute_commit` and stamp dependents \
with `stale_dep`. Read both notes before `review_dependency` for the exact stamp. \
Unrelated edits never clear it. Call `brain_init` for the full manual before \
structural work.
"""

basic_memory_proxy = proxy_basic_memory(BASIC_MEMORY_URL)

_oauth = build_oauth_provider(base_url=MCP_PUBLIC_URL or "http://127.0.0.1:8766", store_path=OAUTH_CLIENT_STORE)
mcp: FastMCP = FastMCP("brain", instructions=_POINTER, auth=_oauth)
if not MCP_PUBLIC_URL:
    print("[brain] MCP_PUBLIC_URL is unset: the OAuth issuer is http://127.0.0.1:8766, "
          "so remote connectors cannot sign in", file=sys.stderr)
print(f"[brain] OAuth provider attached at {MCP_PUBLIC_URL or 'http://127.0.0.1:8766'} "
      f"(clients on disk: {len(_oauth.clients)})", file=sys.stderr)
mcp.mount(commit_mcp)
mcp.mount(basic_memory_proxy)
mcp.mount(codex_mcp)
mcp.mount(todos_mcp)
mcp.mount(pulse_mcp)  # live/ephemeral layer: pulse_* tools (see brain_pulse.py)
mcp.mount(alerts_mcp)  # the alert spine: brain_alert -> ntfy (see brain_alerts.py)


lock_write_surface(mcp)
print(f"[brain] write surface locked: {', '.join(sorted(BLOCKED_TOOLS))}", file=sys.stderr)


@mcp.tool()
def brain_init() -> str:
    """Return the brain's operating manual and master content index in one call.

    **Call this on first interaction with the brain.** Returns two sections:

    1. The operating manual (CLAUDE.md), conventions you must follow: the layers,
       capture bar, commit flow, versioning, sources and broken propagation.
    2. The master index, a map of every note cluster with note counts and previews,
       so you can orient immediately without further tool calls.

    MCP `instructions` give a short routing pointer at session start. This tool
    returns the full manual and index when deeper corpus work needs them.

    After brain_init, open the relevant topic index (e.g. read_note "indexes/designs")
    before diving into individual notes.
    """
    manual = load_manual()
    index = load_master_index()
    if index:
        return manual + "\n\n---\n\n" + index
    return manual


@mcp.tool()
def service_status() -> str:
    """Return live health of every service in the instance topology (Layer 5).

    Reads the latest snapshot from brain_status, which probes each service in
    the instance manifest.json using the conventions health-check resolution order.
    Returns JSON: checked_at, summary counts, and per-service state/detail.
    """
    snap = BRAIN_ROOT / "status" / "snapshot.json"
    if not snap.exists():
        return "No status snapshot yet: brain_status has not run."
    return snap.read_text()


if __name__ == "__main__":
    mcp.run()


# ---- ASGI app: MCP at /mcp + REST API at /api ----------------------------
# Exposed as `app` so uvicorn can serve both surfaces from one process.
# The systemd service runs: uvicorn brain:app --host 127.0.0.1 --port 8766

from brain_api import create_api as _create_api

app = mcp.http_app()
_api = _create_api()  # includes the todos and pulse routers itself
app.mount("/api", _api)

from brain_access import AccessAssertionMiddleware

app.add_middleware(AccessAssertionMiddleware)
