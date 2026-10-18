"""What brain exposes of basic-memory: an era-pinned proxy, minus the writes.

Kept apart from brain.py so tests can exercise the real configuration without
importing brain's tool modules, which bind BRAIN_ROOT at import time.
"""

from fastmcp import FastMCP
from fastmcp.server import create_proxy
from fastmcp.server.transforms import Transform
from fastmcp.tools import Tool
from fastmcp.tools.tool_transform import ArgTransform, forward

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


_VAULT_FOLDERS = {
    "designs", "ideas", "about", "hazards", "findings", "references", "todos",
    "tickets", "nerves", "indexes", "sources", "logs", "brain",
}


async def _search_notes(**arguments):
    query = arguments.get("query") or ""
    scoped = any(arguments.get(name) is not None for name in (
        "note_types", "entity_types", "metadata_filters", "categories", "tags",
        "status", "project", "project_id",
    ))
    words = query.split()
    path_query = len(words) == 1 and (
        words[0].startswith("memory://") or words[0].endswith(".md")
        or words[0].split("/")[0] in _VAULT_FOLDERS and "/" in words[0])
    tag_query = any(word.lower().startswith("tag:") for word in query.split())
    if not (scoped or path_query or tag_query
            or arguments.get("search_all_projects")
            or arguments.get("search_type") == "permalink"):
        arguments["metadata_filters"] = {
            "kind": {"$in": ["design", "idea", "fact", "hazard"]},
        }
    return await forward(**arguments)


def _retrieval_tool(tool):
    if tool.name == "search_notes":
        return Tool.from_tool(
            tool,
            transform_fn=_search_notes,
            description=(tool.description or "") + "\n\nBrain defaults: search the kept "
            "layers designs/, ideas/, about/ and hazards/ (frontmatter kind: design, "
            "idea, fact, hazard), with 5 results in compact text. Explicit filters, "
            "project/project_id, search_all_projects, path/permalink queries and tag: "
            "queries take precedence. Pass metadata_filters={} to search everything "
            "in the resolved project. Increase page_size or set output_format='json' "
            "for structured results.",
            transform_args={
                "page_size": ArgTransform(default=5, description="Results per page (default 5)."),
                "output_format": ArgTransform(
                    default="text",
                    description="Compact text with titles, permalinks and up to 200 characters "
                    "per matching snippet (default); json returns structured results.",
                ),
                "metadata_filters": ArgTransform(
                    description="Explicit frontmatter filters override the kept-layer default. "
                    "Pass {} to search everything in the resolved project.",
                ),
            },
        )
    if tool.name == "build_context":
        return Tool.from_tool(
            tool,
            description=(tool.description or "") + "\n\nBrain default: no recency cutoff "
            "(timeframe=null), so older related notes remain available. Set timeframe "
            "explicitly (for example '7d') to restrict recency.",
            transform_args={
                "timeframe": ArgTransform(
                    default=None,
                    description="No recency cutoff by default (null). Set e.g. '7d' "
                    "to restrict the time window.",
                ),
            },
        )
    return tool


class _RetrievalDefaults(Transform):
    async def list_tools(self, tools):
        return [_retrieval_tool(tool) for tool in tools]

    async def get_tool(self, name, call_next, *, version=None):
        tool = await call_next(name, version=version)
        return _retrieval_tool(tool) if tool is not None else None


def proxy_basic_memory(target):
    # fastmcp 4 proxies mirror the front client's era by default, so a
    # stateless 2026-07-28 caller would make brain speak 2026-07-28 to
    # basic-memory. Pin the initialize handshake: every basic-memory release
    # speaks it, which keeps the proxy working across a basic-memory rollback.
    proxy = create_proxy(target, name="basic-memory", mode="legacy")
    proxy.add_transform(_RetrievalDefaults())
    return proxy


def lock_write_surface(server: FastMCP) -> None:
    # No try/except: fastmcp 4 dropped add_tool_transformation, and the old
    # blanket except turned that into a silent "disabled 0" with every raw
    # write tool exposed. A lock that cannot be applied must stop startup.
    server.disable(names=BLOCKED_TOOLS, components={"tool"})
