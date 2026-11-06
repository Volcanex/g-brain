"""Chat-facing views of the whole G-System: one opener and one project lens.

Agents get this picture from the SessionStart hook; a claude.ai chat only has
the connector, so these tools hand it the same ground in two calls.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from fastmcp import FastMCP

import g_config
from g_pulse.pulse import composed_payload
from g_todos.tickets import list_tickets

mcp = FastMCP("g-overview")

HOME = Path("/home/gabriel")
DOC_CHARS = 2500


def _manifest() -> dict:
    try:
        return json.loads(Path(g_config.MANIFEST_PATH).read_text())
    except (OSError, ValueError):
        return {}


def _health() -> dict[str, dict]:
    snap = g_config.BRAIN_ROOT / "status" / "snapshot.json"
    try:
        return {s["id"]: s for s in json.loads(snap.read_text())["services"]}
    except (OSError, ValueError, KeyError):
        return {}


def _tickets_block(project: str | None = None, limit: int = 12) -> str:
    try:
        open_ = list_tickets("open", project)
    except Exception as e:  # noqa: BLE001
        return f"Tickets unavailable: {e}"
    if not open_:
        return "No open tickets."
    lines = [f"Open tickets ({len(open_)}):"]
    for t in open_[:limit]:
        tag = f"[{t.get('project')}] " if t.get("project") and not project else ""
        lines.append(f"- {tag}{t.get('title')} ({t.get('slug')})")
    if len(open_) > limit:
        lines.append(f"- ... {len(open_) - limit} more: list_tickets")
    return "\n".join(lines)


def _git(repo: Path, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                           text=True, timeout=5)
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


@mcp.tool()
def overview() -> str:
    """Start here in a chat: what Gabriel's world looks like right now.

    Calendar (overdue, today, this week), the pulse page of current context,
    service health, open machine tickets, and where to look for everything
    else (email, money, plans, projects). One call, plain text.
    """
    where = (
        "Where things live:\n"
        "- Email: the gmail inlet (pulse_read_inlet 'gmail') has the distilled live "
        "threads; for a full message use the Gmail connector if this chat has it.\n"
        "- Money: the money inlet (pulse_read_inlet 'money'); Era Context connector "
        "for raw transactions.\n"
        "- Plans and designs: search_notes, then read_note. Gabriel's own designs are "
        "the design layer (read_note 'indexes/designs').\n"
        "- Projects: projects() lists them; projects(name) gives one's docs, recent "
        "commits, services and tickets. search_code searches their source.\n"
        "- Daily brief: pulse_brief."
    )
    return "\n\n".join([composed_payload().strip(), _tickets_block(), where]) + "\n"


@mcp.tool()
def projects(name: str | None = None) -> str:
    """Gabriel's projects. No name: every live project with host, health and
    status. With a name: its repo doc (CLAUDE.md or README), latest commits,
    services and open tickets, so you can talk through its plans.
    """
    m = _manifest()
    projs = {p["id"]: p for p in m.get("projects", [])}
    health = _health()
    by_repo: dict[str, list[dict]] = {}
    for s in health.values():
        by_repo.setdefault(s.get("repo") or "", []).append(s)

    if not name:
        lines = []
        for pid, p in projs.items():
            status = p.get("status")
            if status in ("fossil", "deprecated"):
                continue
            svcs = by_repo.get(pid, [])
            down = [s["id"] for s in svcs if s.get("state") == "down"]
            dom = next((s["domain"] for s in svcs if s.get("domain")), None)
            bits = [p.get("host") or "?"]
            if dom:
                bits.append(dom)
            if down:
                bits.append("DOWN: " + ", ".join(down))
            elif svcs and all(s.get("state") == "paused" for s in svcs):
                bits.append("retired")
            elif svcs:
                bits.append("up")
            lines.append(f"- {pid} ({'; '.join(bits)})")
        return "Projects (fossils hidden):\n" + "\n".join(lines)

    key = name.strip().lower()
    pid = next((p for p in projs if p.lower() == key), None) or \
        next((p for p in projs if key in p.lower()), None)
    if not pid:
        return f"No project matches {name!r}. Call projects() for the list."
    p = projs[pid]
    out = [f"# {pid}", f"host {p.get('host')}, repo {p.get('url')}, status {p.get('status') or 'live'}"]
    repo = HOME / pid
    if repo.is_dir():
        for doc in ("CLAUDE.md", "AGENTS.md", "README.md"):
            f = repo / doc
            if f.is_file():
                text = f.read_text(errors="replace")
                out.append(f"## {doc}\n{text[:DOC_CHARS]}" +
                           ("\n[truncated: read_content or search_code for more]" if len(text) > DOC_CHARS else ""))
                break
        log = _git(repo, "log", "-8", "--format=%ad %s", "--date=short")
        if log:
            out.append("## Recent commits\n" + log)
    else:
        out.append("(repo not checked out on h)")
    svcs = by_repo.get(pid, [])
    if svcs:
        out.append("## Services\n" + "\n".join(
            f"- {s['id']}: {s.get('state')} ({s.get('detail')})" for s in svcs))
    out.append("## " + _tickets_block(pid))
    return "\n\n".join(out) + "\n"
