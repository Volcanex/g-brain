"""Machine and agent work, stored separately from owner commitments.

This module never imports brain_todos. The existing todos MCP registers these
functions; the host may mount create_tickets_router independently.
"""
import datetime as dt
import os
import re
from pathlib import Path

import frontmatter
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from g_todos.text import normalize_text, provenance_for
from g_todos.storage import atomic_write, store_lock

TICKETS_DIR = Path(os.environ.get("BRAIN_ROOT", Path.home() / "brain")).expanduser() / "tickets"
VALID_STATUSES = {"open", "done", "dropped"}


def _path(slug):
    if not slug or "/" in slug or ".." in slug or slug.startswith("."):
        raise ValueError("invalid ticket slug")
    return TICKETS_DIR / f"{slug}.md"


def _serialize(slug, post):
    return {"slug": slug, **post.metadata, "body": post.content}


def _iter():
    for path in sorted(TICKETS_DIR.glob("*.md")):
        yield path.stem, frontmatter.load(path)


def _save(slug, post):
    atomic_write(_path(slug), frontmatter.dumps(post) + "\n")


def _paragraph(body):
    body = normalize_text(body, one_line=True)
    if len(body) > 800:
        raise ValueError("Please supply one short paragraph (at most 800 characters)")
    return body


def create_ticket(title: str, body: str | None = None, project: str | None = None,
                  requester: str = "agent", composer: str = "agent", source: str = "mcp",
                  source_key: str | None = None) -> dict:
    """Record machine or agent work without adding to the owner's todo list."""
    with store_lock(TICKETS_DIR):
        return _create_ticket(title, body, project, requester, composer, source, source_key)


def _create_ticket(title, body, project, requester, composer, source, source_key):
    if source_key:
        for slug, post in _iter():
            if post.get("source_key") == source_key:
                return {**_serialize(slug, post), "deduped": True}
    title = normalize_text(title, one_line=True)
    if not title:
        raise ValueError("title is required")
    body = _paragraph(body)
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "ticket"
    slug, number = base, 2
    while _path(slug).exists():
        slug, number = f"{base}-{number}", number + 1
    post = frontmatter.Post(body, title=title, kind="ticket", status="open",
                            created=dt.date.today().isoformat(),
                            provenance=provenance_for(requester, composer, source))
    if project is not None:
        post["project"] = project
    if source_key:
        post["source_key"] = source_key
    _save(slug, post)
    return _serialize(slug, post)


def list_tickets(status: str = "open", project: str | None = None) -> list[dict]:
    """List tickets, optionally including all statuses or one project."""
    if status not in VALID_STATUSES | {"all"}:
        raise ValueError("status must be open, done, dropped or all")
    return [_serialize(slug, post) for slug, post in _iter()
            if (status == "all" or post.get("status") == status)
            and (project is None or post.get("project") == project)]


def close_ticket(slug: str, status: str = "done", note: str | None = None) -> dict:
    """Close a ticket as done or dropped, retaining its origin and history."""
    with store_lock(TICKETS_DIR):
        return _close_ticket(slug, status, note)


def _close_ticket(slug, status, note):
    if status not in {"done", "dropped"}:
        raise ValueError("closed status must be done or dropped")
    post = frontmatter.load(_path(slug))
    post["status"] = status
    post["closed"] = dt.date.today().isoformat()
    if note:
        post.content = _paragraph(post.content + " " + note)
    _save(slug, post)
    return _serialize(slug, post)


def register_tools(mcp):
    for fn in (create_ticket, list_tickets, close_ticket):
        mcp.tool()(fn)


class _TicketCreate(BaseModel):
    title: str
    body: str | None = None
    project: str | None = None
    requester: str = "unknown"
    composer: str = "unknown"
    source: str = "unknown"
    source_key: str | None = None


class _TicketClose(BaseModel):
    status: str = "done"
    note: str | None = None


def create_tickets_router() -> APIRouter:
    router = APIRouter(prefix="/tickets", tags=["tickets"])

    @router.get("")
    async def list_(status: str = "open", project: str | None = None):
        try:
            return list_tickets(status, project)
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @router.post("")
    async def create_(payload: _TicketCreate):
        try:
            return create_ticket(**payload.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    @router.patch("/{slug}")
    async def close_(slug: str, payload: _TicketClose):
        try:
            return close_ticket(slug, **payload.model_dump())
        except FileNotFoundError:
            raise HTTPException(404, "ticket not found")
        except ValueError as exc:
            raise HTTPException(400, str(exc))

    return router
