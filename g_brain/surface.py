"""What brain exposes of basic-memory: an era-pinned proxy, minus the writes.

Kept apart from brain.py so tests can exercise the real configuration without
importing brain's tool modules, which bind BRAIN_ROOT at import time.
"""

from fastmcp import FastMCP
from fastmcp.server import create_proxy

# ----------------------------------------------------------------------
# Lock down the write surface.
#
# basic-memory proxies a bunch of write tools (write_note, edit_note,
# delete_note, move_note, create_memory_project, delete_project). If the
# LLM grabs these, it bypasses brain-commit entirely: no version bump,
# no lineage update, no commit log entry, no path/format validation.
# That's how we ended up with ``~/brain/reference/markupschema`` (singular
# directory, no ``.md`` extension) instead of
# ``~/brain/references/markupschema.md``.
#
# We strip them so the only blessed write paths are:
#   - propose_commit / execute_commit  (versioned creates + edits)
#   - mark_broken                      (status flip, no content rewrite)
#   - supersede_section                (chops + archives a section)
#
# Read tools (search, read_note, list_directory, recent_activity, etc.)
# stay live: basic-memory's full-text + semantic search is genuinely
# useful and can't damage anything.
# ----------------------------------------------------------------------
BLOCKED_TOOLS = {
    "write_note",
    "edit_note",
    "delete_note",
    "move_note",
    "create_memory_project",
    "delete_project",
}


def proxy_basic_memory(target):
    # fastmcp 4 proxies mirror the front client's era by default, so a
    # stateless 2026-07-28 caller would make brain speak 2026-07-28 to
    # basic-memory. Pin the initialize handshake: every basic-memory release
    # speaks it, which keeps the proxy working across a basic-memory rollback.
    return create_proxy(target, name="basic-memory", mode="legacy")


def lock_write_surface(server: FastMCP) -> None:
    # No try/except: fastmcp 4 dropped add_tool_transformation, and the old
    # blanket except turned that into a silent "disabled 0" with every raw
    # write tool exposed. A lock that cannot be applied must stop startup.
    server.disable(names=BLOCKED_TOOLS, components={"tool"})
