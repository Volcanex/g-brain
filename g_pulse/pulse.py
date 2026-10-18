"""brain-pulse: the live/ephemeral layer beside the brain.

The brain (designs/findings/references/...) is durable, curated, explicitly
committed truth. The PULSE is the opposite: high-velocity, auto-ingested,
disposable live data (money, messages, calendar) that "takes up your head"
but does not belong in long-term memory.

Pulse lives in ~/brain/.pulse/ (dot-prefixed: invisible to the brain reader
and the secondary graph, like .quarantine/). It never goes through
brain-commit and is never versioned. Its job is to feed the brain's L6
commitment layer: a scheduled Claude agent reads an INLET, reasons over it,
and opens todos via the existing create_todo tool. Inference is the model's;
the todo write boundary checks owner action and provenance. If the Claude subscription is down, the
pulse is down; there is no degraded tier and no backup.

Two concepts:
  * INLET: a declarative source of live data (inlets/<id>.md). Frontmatter
           says what the source is, how often to look, what to distil.
  * CRON: a scheduled Claude agent (crons/<name>.md) that does the looking.
          Make one by copying crons/_example.md.

Tools are namespaced pulse_* so they never collide with the brain's cortex
tools. They are read-only plus one ephemeral-state write (pulse_record).
Mirrors the brain_todos.py module shape: a self-contained FastMCP `mcp`,
a create_router() for REST under /api/pulse, and a small argparse CLI.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any

import frontmatter
from yaml import YAMLError
from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from fastmcp import FastMCP

import g_config

BRAIN_ROOT = g_config.BRAIN_ROOT
PULSE_DIR = BRAIN_ROOT / ".pulse"
INLETS_DIR = PULSE_DIR / "inlets"
CRONS_DIR = PULSE_DIR / "crons"
STATE_DIR = PULSE_DIR / "state"
BRIEFS_DIR = PULSE_DIR / "briefs"
PAGE_PATH = PULSE_DIR / "page.md"
STATUS_SNAPSHOT = BRAIN_ROOT / "status" / "snapshot.json"

PAGE_SOFT_CAP = 60          # snippets before pulse_note starts nagging
PAYLOAD_CHAR_BUDGET = 4000  # ~1k tokens; the payload rides into every session

_PAGE_HEADER = """\
<!-- pulse page: short-term working memory. One undated snippet per line.
     has a date -> todo | true now -> here | true always -> the brain.
     Appended by pulse_note, compacted daily. Never versioned or committed. -->
"""

mcp = FastMCP("brain-pulse")


def _today() -> str:
    return dt.date.today().isoformat()


def _safe_id(inlet_id: str) -> str:
    if "/" in inlet_id or ".." in inlet_id or inlet_id.startswith("."):
        raise ValueError(f"invalid inlet id: {inlet_id!r}")
    return inlet_id


def _iter_inlets():
    if not INLETS_DIR.exists():
        return
    for p in sorted(INLETS_DIR.glob("*.md")):
        if p.stem.startswith("_"):
            continue
        try:
            yield p.stem, frontmatter.load(p)
        except Exception:
            continue


def _load_inlet(inlet_id: str) -> frontmatter.Post:
    p = INLETS_DIR / f"{_safe_id(inlet_id)}.md"
    if not p.exists():
        raise FileNotFoundError(inlet_id)
    return frontmatter.load(p)


def _state_path(inlet_id: str) -> Path:
    return STATE_DIR / f"{_safe_id(inlet_id)}.md"


def _read_state(inlet_id: str) -> str | None:
    p = _state_path(inlet_id)
    return p.read_text() if p.exists() else None


def _serialize_inlet(
    inlet_id: str, post: frontmatter.Post, include_state: bool = False
) -> dict[str, Any]:
    md = post.metadata
    out = {
        "id": inlet_id,
        "title": str(md.get("title") or inlet_id),
        "source": md.get("source") or {},
        "schedule": str(md.get("schedule") or ""),
        "distil": list(md.get("distil") or []),
        "emits": md.get("emits") or {},
        "status": str(md.get("status") or "active"),
        "summary": str(md.get("summary") or ""),
        "last_run": str(md.get("last_run") or ""),
    }
    if include_state:
        out["state"] = _read_state(inlet_id)
        out["spec_body"] = post.content
    return out


def _latest_brief() -> tuple[str, str] | None:
    if not BRIEFS_DIR.exists():
        return None
    briefs = sorted(BRIEFS_DIR.glob("*.md"))
    if not briefs:
        return None
    p = briefs[-1]
    return p.stem, p.read_text()


# --------------------------------------------------------------------- page

def _page_snippet_count() -> int:
    if not PAGE_PATH.exists():
        return 0
    return sum(1 for l in PAGE_PATH.read_text().splitlines() if l.lstrip().startswith("- "))


def append_page(snippet: str) -> int:
    """Append one snippet line. O_APPEND so a concurrent compactor rewrite
    (tmp + atomic rename) can never interleave bytes with us."""
    snippet = " ".join(snippet.split())
    if not snippet:
        raise ValueError("empty snippet")
    PULSE_DIR.mkdir(parents=True, exist_ok=True)
    line = f"- {snippet}\n"
    if not PAGE_PATH.exists():
        line = _PAGE_HEADER + "\n" + line
    fd = os.open(PAGE_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)
    return _page_snippet_count()


def _todos_soon(days: int = 7) -> list[str]:
    """Open todos starting or due within `days` (incl. overdue), one line each."""
    try:
        import brain_todos
        horizon = (dt.date.today() + dt.timedelta(days=days)).isoformat()
        out = []
        for slug, post in brain_todos.iter_todos():
            t = brain_todos.serialize(slug, post)
            if t.get("status") != "open":
                continue
            when = str(t.get("due") or t.get("start") or "")[:10]
            if when and when <= horizon:
                out.append(f"- {when} {t.get('title', slug)}")
        return sorted(out)
    except Exception:
        return []


def _health_line() -> str:
    try:
        snap = json.loads(STATUS_SNAPSHOT.read_text())
        s = snap.get("summary", {})
        line = f"Services: {s.get('up', '?')}/{s.get('total', '?')} up"
        down = [x.get("id", "?") for x in snap.get("services", []) if x.get("state") == "down"]
        if down:
            line += f" (down: {', '.join(down)})"
        checked = dt.datetime.fromisoformat(snap["checked_at"])
        age_h = (dt.datetime.now(dt.timezone.utc) - checked).total_seconds() / 3600
        if age_h > 2:
            line += f", snapshot {age_h:.0f}h stale"
        return line + "."
    except Exception:
        return ""


def _brief_age_line() -> str:
    latest = _latest_brief()
    if latest is None:
        return "No daily brief yet: the compactor has never run."
    date = latest[0]
    try:
        age = (dt.date.today() - dt.date.fromisoformat(date)).days
    except ValueError:
        return f"Latest brief: {date}."
    if age > 1:
        return f"Latest brief is {age} days old ({date}). The compactor may be dead."
    return f"Latest brief: {date}."


def _about_block() -> str:
    lines = []
    for path in sorted((BRAIN_ROOT / "about").glob("*.md")):
        try:
            post = frontmatter.load(path)
            title = " ".join(str(post.get("title") or path.stem).split())[:120]
            summary = " ".join(str(post.get("summary") or "").split())[:240]
            if post.get("kind") != "fact" or not summary:
                continue
            line = f"- {title}: {summary}"
            if sum(len(item) + 1 for item in lines) + len(line) > 1600:
                break
            lines.append(line)
        except (OSError, ValueError, YAMLError):
            continue
        if len(lines) == 40:
            break
    return "About Gabriel\n" + "\n".join(lines) if lines else ""


def composed_payload() -> str:
    """The pulse payload that rides into every session: page, near todos,
    health, brief age. Each section degrades to nothing rather than erroring."""
    about = _about_block()
    sections = [about] if about else []
    try:
        page = PAGE_PATH.read_text().strip() if PAGE_PATH.exists() else ""
    except Exception:
        page = ""
    sections.append(page or "(pulse page is empty, nothing recorded yet)")
    todos = _todos_soon()
    if todos:
        sections.append("Dated, next 7 days:\n" + "\n".join(todos))
    health = _health_line()
    if health:
        sections.append(health)
    sections.append(_brief_age_line())
    text = "\n\n".join(sections).strip() + "\n"
    if len(text) > PAYLOAD_CHAR_BUDGET:
        text = text[:PAYLOAD_CHAR_BUDGET].rstrip() + "\n[payload truncated: page needs compaction]\n"
    return text


# --------------------------------------------------------------------- MCP

@mcp.tool()
def pulse_list_inlets() -> list[dict]:
    """List the pulse's live-data inlets (money, messages, ...).

    Each inlet is a declarative source of ephemeral, auto-ingested data that
    lives OUTSIDE the brain's curated memory. Returns id, schedule, what it
    distils, last run, and status. Read this to see what live feeds exist.
    """
    return [_serialize_inlet(i, post) for i, post in _iter_inlets()]


@mcp.tool()
def pulse_read_inlet(inlet_id: str) -> dict:
    """Read one inlet: its spec, distil instruction, and latest live state.

    `state` is the current ephemeral snapshot (e.g. balance, open threads)
    written by the inlet's scheduled Claude agent. It is NOT durable truth
    and is not in the brain. May be null if the inlet hasn't run yet.
    """
    try:
        post = _load_inlet(inlet_id)
    except FileNotFoundError:
        raise ValueError(f"no such inlet: {inlet_id}")
    return _serialize_inlet(inlet_id, post, include_state=True)


@mcp.tool()
def pulse_brief() -> dict:
    """Return the latest daily brief: the 'what's on my plate right now' view.

    Ephemeral and regenerated; only the commitments inside it (as L6 todos)
    are durable. Returns {date, body}, or a note if none has been generated.
    """
    latest = _latest_brief()
    date, body = latest if latest else (None, "No brief generated yet.")
    about = _about_block()
    if about:
        body += "\n\n" + about
    return {"date": date, "body": body}


@mcp.tool()
def pulse_page() -> str:
    """Return the pulse page: short-term working memory, composed with dated
    todos for the next 7 days, service health, and brief freshness.

    This same payload is injected into sessions at start; call this to refresh
    it mid-session. Routing rule: has a date → todo, true now → pulse,
    true always → the brain.
    """
    return composed_payload()


@mcp.tool()
def pulse_note(snippet: str) -> dict:
    """Append one short snippet of current context to the pulse page.

    For undated, currently-true facts ("working from the library this week",
    "leaning toward X") heard in passing. Append liberally and tell the user you did. NOT for
    dated/actionable things (use create_todo) or durable truths (propose a
    brain commit). One sentence, no date, no markdown beyond plain text.
    """
    n = append_page(snippet)
    out: dict[str, Any] = {"ok": True, "snippets": n}
    if n > PAGE_SOFT_CAP:
        out["warning"] = (
            f"page holds {n} snippets (soft cap {PAGE_SOFT_CAP}), compaction overdue"
        )
    return out


def record_state(inlet_id: str, state: str, summary: str | None = None) -> dict:
    """Shared by the MCP tool and the CLI, so a headless cron without MCP can record too."""
    try:
        post = _load_inlet(inlet_id)
    except FileNotFoundError:
        raise ValueError(f"no such inlet: {inlet_id}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    sp = frontmatter.Post(content=state.rstrip() + "\n")
    sp["inlet"] = inlet_id
    sp["updated"] = _today()
    if summary:
        sp["summary"] = summary
    _state_path(inlet_id).write_bytes(frontmatter.dumps(sp).encode("utf-8") + b"\n")
    post["last_run"] = _today()
    (INLETS_DIR / f"{inlet_id}.md").write_bytes(
        frontmatter.dumps(post).encode("utf-8") + b"\n"
    )
    return {"id": inlet_id, "updated": _today(), "summary": summary or ""}


# Old private names, kept for callers outside this module.
_append_page, _record_state, _composed_payload = append_page, record_state, composed_payload


@mcp.tool()
def pulse_record(inlet_id: str, state: str, summary: str | None = None) -> dict:
    """Persist the current distilled live-state for an inlet (ephemeral).

    For use by an inlet's scheduled Claude agent after it reads + reasons over
    a source. Overwrites the prior snapshot: pulse state is current-only, not
    versioned. Durable commitments should ALSO be written as L6 todos via
    create_todo; this is just the live dashboard slice. Stamps last_run.
    """
    return record_state(inlet_id, state, summary)


# --------------------------------------------------------------------- REST

def create_router() -> APIRouter:
    r = APIRouter(prefix="/pulse", tags=["pulse"])

    @r.get("/inlets")
    async def list_inlets():
        return [_serialize_inlet(i, post) for i, post in _iter_inlets()]

    @r.get("/inlets/{inlet_id}")
    async def get_inlet(inlet_id: str):
        try:
            post = _load_inlet(inlet_id)
        except FileNotFoundError:
            raise HTTPException(404, "inlet not found")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return _serialize_inlet(inlet_id, post, include_state=True)

    @r.get("/brief")
    async def brief():
        return pulse_brief()

    # Read-only by design: the page is injected into every session, so a REST
    # write path on this unauthenticated face would be a prompt-injection
    # vector. Writes go through the MCP tool or in-process callers only.
    @r.get("/page", response_class=PlainTextResponse)
    async def page():
        return composed_payload()

    return r


# --------------------------------------------------------------------- CLI

def _cli(argv: list[str] | None = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(prog="pulse", description="Inspect the live pulse layer.")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("inlets", help="list inlets")
    sp = sub.add_parser("show", help="show one inlet + state")
    sp.add_argument("inlet_id")
    sub.add_parser("brief", help="print the latest daily brief")
    sub.add_parser("page", help="print the composed pulse page payload")
    np_ = sub.add_parser("note", help="append a snippet to the pulse page")
    np_.add_argument("snippet", nargs="+")
    rp = sub.add_parser("record", help="persist an inlet snapshot read from stdin (same as pulse_record)")
    rp.add_argument("inlet_id")
    rp.add_argument("--summary")
    args = ap.parse_args(argv)

    if args.cmd == "inlets":
        rows = [_serialize_inlet(i, post) for i, post in _iter_inlets()]
        if not rows:
            print("no inlets defined")
            return 0
        w = max(len(r["id"]) for r in rows)
        print(f"{'INLET':<{w}}  {'SCHEDULE':<15}  {'STATUS':<8}  SUMMARY")
        for r in rows:
            print(f"{r['id']:<{w}}  {r['schedule'] or '-':<15}  {r['status']:<8}  {r['summary']}")
        return 0
    if args.cmd == "show":
        try:
            post = _load_inlet(args.inlet_id)
        except FileNotFoundError:
            print(f"no such inlet: {args.inlet_id}")
            return 1
        print(json.dumps(_serialize_inlet(args.inlet_id, post, include_state=True), indent=2, default=str))
        return 0
    if args.cmd == "brief":
        print(pulse_brief()["body"])
        return 0
    if args.cmd == "page":
        print(composed_payload(), end="")
        return 0
    if args.cmd == "record":
        import sys
        try:
            out = record_state(args.inlet_id, sys.stdin.read(), args.summary)
        except ValueError as e:
            print(e)
            return 1
        print(json.dumps(out))
        return 0
    if args.cmd == "note":
        n = append_page(" ".join(args.snippet))
        print(f"noted ({n} snippets on page)")
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
