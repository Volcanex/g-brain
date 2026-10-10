"""Brain reader: direct filesystem + sqlite access to the brain.

Read-only. The brain itself lives at ``BRAIN_ROOT`` (default ``~/brain``)
and is owned by the brain server (``server/brain.py``). This site only
reads (never writes) to keep concerns separated and avoid races with
the MCP write path.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterator

import frontmatter

import g_config

BRAIN_ROOT = g_config.BRAIN_ROOT
REFS_DB = BRAIN_ROOT / ".refs-index.sqlite"

WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")


@dataclass
class Note:
    path: str  # relative to BRAIN_ROOT, posix style, no .md
    title: str
    kind: str
    status: str
    version: str
    lineage: list[str]
    tags: list[str]
    updated: str
    created: str
    body: str = ""
    wikilinks: list[str] = field(default_factory=list)


# System / framework files at the brain root that aren't notes.
EXCLUDED_FILENAMES = {"CLAUDE.md", "README.md", ".refs-index.md", "commits.log"}

# Extensions / file types we treat as candidates for being a note.
# Notes can be created without a `.md` suffix (basic-memory's write_note
# tool does this); we accept them if they sniff as YAML-frontmattered
# markdown.
_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".sqlite",
    ".db", ".zip", ".tar", ".gz", ".log", ".json", ".yaml", ".yml",
}


def _looks_like_note(path: Path) -> bool:
    """Sniff: file starts with a YAML frontmatter fence (``---``)."""
    try:
        with path.open("rb") as f:
            head = f.read(4)
    except OSError:
        return False
    return head.startswith(b"---\n") or head.startswith(b"---\r")


def _iter_md_files() -> Iterator[Path]:
    if not BRAIN_ROOT.exists():
        return
    for entry in BRAIN_ROOT.rglob("*"):
        if not entry.is_file():
            continue
        rel = entry.relative_to(BRAIN_ROOT)
        if any(p.startswith(".") for p in rel.parts):
            continue
        if rel.name in EXCLUDED_FILENAMES:
            continue
        suffix = entry.suffix.lower()
        if suffix == ".md":
            yield entry
            continue
        # Non-markdown extensions we know aren't notes: skip without sniff.
        if suffix in _BINARY_EXTS:
            continue
        # Extensionless or unknown-suffix file: sniff for frontmatter.
        if _looks_like_note(entry):
            yield entry


def _parse_note(md: Path) -> Note | None:
    try:
        post = frontmatter.load(md)
    except Exception:
        return None
    rel = md.relative_to(BRAIN_ROOT).as_posix().removesuffix(".md")
    body = post.content or ""
    wikilinks = sorted(set(m.group(1).strip() for m in WIKILINK_RE.finditer(body)))
    lineage = post.get("lineage") or []
    if not isinstance(lineage, list):
        lineage = []
    return Note(
        path=rel,
        title=str(post.get("title") or md.stem),
        kind=str(post.get("kind") or "unknown"),
        status=str(post.get("status") or "active"),
        version=str(post.get("version") or "0.0.0"),
        lineage=[str(x) for x in lineage],
        tags=[str(t) for t in (post.get("tags") or [])],
        updated=str(post.get("updated") or ""),
        created=str(post.get("created") or ""),
        body=body,
        wikilinks=wikilinks,
    )


@lru_cache(maxsize=1)
def _all_notes_cached(_mtime_signature: int) -> list[Note]:
    notes: list[Note] = []
    for md in _iter_md_files():
        n = _parse_note(md)
        if n is not None:
            notes.append(n)
    return notes


def all_notes() -> list[Note]:
    """All notes in the brain. Caches per BRAIN_ROOT mtime so repeat
    reads are cheap; cache invalidates whenever any file changes."""
    sig = 0
    if BRAIN_ROOT.exists():
        for md in BRAIN_ROOT.rglob("*.md"):
            try:
                sig ^= int(md.stat().st_mtime_ns)
            except OSError:
                pass
    return _all_notes_cached(sig)


def get_note(path: str) -> Note | None:
    root = BRAIN_ROOT.resolve()
    for candidate in (BRAIN_ROOT / f"{path}.md", BRAIN_ROOT / path):
        target = candidate.resolve()
        if not str(target).startswith(str(root)):
            continue
        if target.is_file():
            return _parse_note(target)
    return None


def kind_counts() -> list[dict]:
    """Per-kind {kind, total, active, broken, draft, superseded}."""
    by_kind: dict[str, Counter] = defaultdict(Counter)
    for n in all_notes():
        by_kind[n.kind]["total"] += 1
        by_kind[n.kind][n.status] += 1
    out = []
    for kind, c in sorted(by_kind.items()):
        out.append({
            "kind": kind,
            "total": c["total"],
            "active": c["active"],
            "broken": c["broken"],
            "draft": c["draft"],
            "superseded": c["superseded"],
        })
    return out


def recent_activity(limit: int = 10) -> list[dict]:
    notes = sorted(all_notes(), key=lambda n: n.updated or n.created, reverse=True)
    return [
        {
            "path": n.path,
            "title": n.title,
            "kind": n.kind,
            "status": n.status,
            "updated": n.updated,
            "version": n.version,
        }
        for n in notes[:limit]
    ]


def refs_summary() -> dict:
    if not REFS_DB.exists():
        return {"total_urls": 0, "distinct_canonical": 0, "broken": 0}
    conn = sqlite3.connect(REFS_DB)
    try:
        total = conn.execute("SELECT COUNT(DISTINCT url) FROM refs").fetchone()[0]
        canon = conn.execute("SELECT COUNT(DISTINCT canonical_url) FROM refs").fetchone()[0]
        broken = conn.execute(
            "SELECT COUNT(DISTINCT url) FROM refs "
            "WHERE status LIKE 'http-4%' OR status LIKE 'http-5%' OR status LIKE 'error-%'"
        ).fetchone()[0]
    finally:
        conn.close()
    return {"total_urls": total, "distinct_canonical": canon, "broken": broken}


def top_refs(limit: int = 50) -> list[dict]:
    if not REFS_DB.exists():
        return []
    conn = sqlite3.connect(REFS_DB)
    try:
        rows = conn.execute(
            """
            SELECT canonical_url,
                   COUNT(DISTINCT note) AS n_notes,
                   COUNT(DISTINCT url)  AS n_variants,
                   MAX(status) AS status
            FROM refs
            GROUP BY canonical_url
            ORDER BY n_notes DESC, canonical_url ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"url": url, "notes": n, "variants": v, "status": status}
        for url, n, v, status in rows
    ]


def normalize_path(path: str) -> str:
    """Canonical note key: posix-style, relative to BRAIN_ROOT, no .md.

    Commit-side targets carry `.md` (`designs/foo.md`); reader-side paths
    don't (`designs/foo`). One converter so the two halves agree.
    """
    return path.strip().lstrip("/").removesuffix(".md")


def link_edges(notes: list[Note] | None = None) -> list[tuple[str, str]]:
    """Resolved (source_path, target_path) wikilink pairs across the brain.

    A wikilink target may be written as a full path or a bare basename;
    both resolve here so callers never re-implement the lookup. Unresolved
    targets (links to notes that don't exist) and self-links are dropped.
    """
    notes = all_notes() if notes is None else notes
    by_path = {n.path: n for n in notes}

    # Alias table for bare-basename links. Folder-shaped notes live at
    # `designs/<slug>/current.md` but everything wikilinks them as
    # [[<slug>]]: keying on the basename alone gives "current" for all
    # four of them, so they resolved to nothing and sat orphaned in the
    # graph. Map the folder name too.
    by_basename: dict[str, str] = {}
    for p in by_path:
        parts = Path(p).parts
        by_basename.setdefault(Path(p).stem, p)
        if parts and parts[-1] == "current" and len(parts) >= 2:
            by_basename[parts[-2]] = p

    edges: list[tuple[str, str]] = []
    for n in notes:
        for target_name in n.wikilinks:
            if target_name in by_path:
                tgt = target_name
            elif target_name in by_basename:
                tgt = by_basename[target_name]
            else:
                continue
            if tgt == n.path:
                continue
            edges.append((n.path, tgt))
    return edges


def dependents(note_path: str, hops: int = 1) -> list[dict]:
    """Notes that depend on `note_path`, i.e. wikilink to it, transitively.

    This is the *primary* graph (note -> note), which is what a rewrite's
    blast radius is measured in. `query_references` answers a different
    question: the secondary graph of external URLs a note cites.

    Returns [{path, title, kind, status, hops}], nearest first.
    """
    target = normalize_path(note_path)
    notes = all_notes()
    by_path = {n.path: n for n in notes}

    # Two classes of note can never "go stale" and would only add noise:
    # indexes/ are recompiled wholesale by rebuild_indexes and link to
    # everything, and versions/ + archive/ are frozen history that is
    # *supposed* to reference the state of the world when it was written.
    def _flaggable(path: str) -> bool:
        if path.startswith("indexes/"):
            return False
        return not any(p in ("versions", "archive", ".history") for p in Path(path).parts)

    incoming: dict[str, list[str]] = defaultdict(list)
    for src, tgt in link_edges(notes):
        if not _flaggable(src):
            continue
        incoming[tgt].append(src)

    found: dict[str, int] = {}
    frontier = [target]
    for hop in range(1, max(1, hops) + 1):
        nxt: list[str] = []
        for node in frontier:
            for src in incoming.get(node, []):
                if src == target or src in found:
                    continue
                found[src] = hop
                nxt.append(src)
        frontier = nxt
        if not frontier:
            break

    out = []
    for path, hop in sorted(found.items(), key=lambda kv: (kv[1], kv[0])):
        n = by_path.get(path)
        out.append({
            "path": path,
            "title": n.title if n else path,
            "kind": n.kind if n else "unknown",
            "status": n.status if n else "unknown",
            "hops": hop,
        })
    return out


def wikilink_graph() -> dict:
    """{nodes: [...], links: [...]} suitable for D3 force-directed."""
    notes = all_notes()
    nodes = [
        {"id": n.path, "title": n.title, "kind": n.kind, "status": n.status}
        for n in notes
    ]
    links = [{"source": s, "target": t} for s, t in link_edges(notes)]
    return {"nodes": nodes, "links": links}


def citations_for(note_path: str) -> list[dict]:
    """Citations (external URLs) cited by a single note.

    `note_path` is the site-style path without `.md` (e.g.
    "findings/ctr-prediction-open-source-stack"); the refs DB stores
    paths with `.md`, so we append before lookup.
    """
    if not REFS_DB.exists():
        return []
    key = note_path if note_path.endswith(".md") else f"{note_path}.md"
    conn = sqlite3.connect(REFS_DB)
    try:
        rows = conn.execute(
            """
            SELECT url, canonical_url, status, last_checked, first_seen, last_seen
            FROM refs
            WHERE note = ?
            ORDER BY first_seen ASC, url ASC
            """,
            (key,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "url": url,
            "canonical_url": canon,
            "status": status or "unknown",
            "last_checked": last_checked,
            "first_seen": first_seen,
            "last_seen": last_seen,
        }
        for url, canon, status, last_checked, first_seen, last_seen in rows
    ]
