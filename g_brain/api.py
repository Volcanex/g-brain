"""brain_api: REST endpoints for web UI consumption.

Mounted at /api on the brain ASGI app alongside the MCP server at /mcp.
Both faces call the same underlying reader functions: one codepath, two interfaces.

Endpoints:
  GET /api/graph          wikilink D3 graph
  GET /api/nerves         nervous system D3 graph (layer 3): the CLAUDE.md /
                          AGENTS.md web, one node per directory carrying either
  GET /api/lineage        lineage D3 graph (layer 4)
  GET /api/notes          note listing with facets
  GET /api/note/{path}    single note with backlinks, forward links, rendered html
  GET /api/refs           refs index
  GET /api/refs/graph     refs D3 graph
  GET /api/home/stats     dashboard stats
"""

from __future__ import annotations

import configparser
import json
import re
import sqlite3
import tomllib
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse
from markdown_it import MarkdownIt

import g_config
from brain_condense import CORPUS_LAYERS, is_corpus_note, read_report as condense_report
from brain_verify import read_report as stale_report
from brain_reader import (
    BRAIN_ROOT,
    REFS_DB,
    WIKILINK_RE,
    Note,
    all_notes,
    citations_for,
    get_note,
    kind_counts,
    recent_activity,
    refs_summary,
    top_refs,
    wikilink_graph,
)

_md = MarkdownIt("commonmark", {"html": False, "linkify": True, "breaks": False})
_YAML_KEY_LINE = re.compile(r"^([a-z][a-z0-9_]*)\s*:\s*(.*)$", re.IGNORECASE)


def create_api() -> FastAPI:
    api = FastAPI(title="brain API", docs_url=None, redoc_url=None)

    # CORS: this face serves public, read-only data and is consumed by
    # browser apps on other origins (g-command, the operator's sites).
    from fastapi.middleware.cors import CORSMiddleware
    api.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["*"],
    )

    # Todos surface (CRUD-ish; the only mutable face on /api).
    from brain_todos import create_router as _todos_router
    api.include_router(_todos_router())

    # Pulse surface (live/ephemeral layer: /api/pulse/*).
    from brain_pulse import create_router as _pulse_router
    api.include_router(_pulse_router())

    # ------------------------------------------------------------------ graph
    @api.get("/graph")
    async def graph():
        return wikilink_graph()

    # ------------------------------------------------------------------ nerves
    @api.get("/nerves")
    async def nerves():
        """The nervous system as a directory web.

        One node per repo, then one per directory that carries a CLAUDE.md or
        an AGENTS.md, nested at full depth through its ancestors. Each node
        reports how its pair sits: symlinked, copied and identical, copied and
        drifted, or half missing. Top-level directories with no nerve at all
        are included so coverage gaps stay visible.
        """
        nerves_root = BRAIN_ROOT / "nerves"
        nodes, links = [], []
        summary = {"repos": 0, "nerve_dirs": 0, "symlink": 0, "copy_synced": 0,
                   "copy_drift": 0, "claude_only": 0, "agents_only": 0, "unknown": 0}
        if not nerves_root.exists():
            return {"nodes": nodes, "links": links, "summary": summary,
                    "error": "nerves not synced, run brain_gmap"}

        def first_heading(p: Path) -> str:
            try:
                for line in p.read_text(errors="replace").splitlines():
                    if line.startswith("# "):
                        return line[2:].strip()
            except OSError:
                pass
            return p.name

        def from_disk(repo_dir: Path) -> list[dict]:
            """Fallback for a repo synced before pair tracking landed.

            Only CLAUDE.md files are on disk, so the pair state is genuinely
            unknown rather than absent. Say so instead of reporting a gap.
            """
            out = []
            for f in sorted(repo_dir.rglob("CLAUDE.md")):
                rel = f.relative_to(repo_dir).parent
                out.append({"dir": "" if str(rel) == "." else str(rel),
                            "claude": {}, "agents": None, "pair": "unknown"})
            return out

        for repo_dir in sorted(nerves_root.iterdir()):
            if not repo_dir.is_dir():
                continue
            meta_file = repo_dir / ".meta.json"
            if not meta_file.exists():
                continue
            meta = json.loads(meta_file.read_text())
            repo = repo_dir.name
            repo_id = f"repo-{repo}"
            summary["repos"] += 1

            records = meta.get("nerves")
            if records is None:
                records = from_disk(repo_dir)
            by_dir = {r["dir"]: r for r in records}

            for r in records:
                summary["nerve_dirs"] += 1
                if r["pair"] in summary:
                    summary[r["pair"]] += 1

            compliant = meta.get("has_agents_md", False) and meta.get("has_pyproject", False)
            root_claude = repo_dir / "CLAUDE.md"
            root = by_dir.get("")
            nodes.append({
                "id": repo_id, "label": repo, "type": "repo", "repo": repo,
                "compliant": compliant,
                "title": first_heading(root_claude) if root_claude.exists() else repo,
                "pair": root["pair"] if root else "none",
                "nerve_dirs": len(records),
            })

            # nerve dirs plus every ancestor, so the web keeps the real shape
            drawn = set()
            for d in by_dir:
                if not d:
                    continue
                parts = d.split("/")
                for i in range(1, len(parts) + 1):
                    drawn.add("/".join(parts[:i]))
            drawn.update(meta.get("dirs", []))

            for d in sorted(drawn):
                rec = by_dir.get(d)
                parts = d.split("/")
                parent = "/".join(parts[:-1])
                nodes.append({
                    "id": f"dir-{repo}-{d}", "label": parts[-1], "type": "dir",
                    "repo": repo, "path": d, "depth": len(parts),
                    "pair": rec["pair"] if rec else "none",
                    "has_nerve": rec is not None,
                })
                links.append({
                    "source": f"dir-{repo}-{parent}" if parent else repo_id,
                    "target": f"dir-{repo}-{d}",
                })

        return {"nodes": nodes, "links": links, "summary": summary}

    # ---------------------------------------------------------------- lineage
    @api.get("/lineage")
    async def lineage():
        lineage_dir = BRAIN_ROOT / "lineage"
        nodes, links = [], []
        if not lineage_dir.exists():
            return {"nodes": nodes, "links": links,
                    "error": "lineage not synced, run brain_gmap"}

        seen_sources: dict[str, bool] = {}

        def ensure_source(name: str) -> None:
            if name not in seen_sources:
                seen_sources[name] = True
                nodes.append({"id": f"src-{name}", "label": name, "type": "source"})

        for toml_file in sorted(lineage_dir.glob("*.toml")):
            try:
                data = tomllib.loads(toml_file.read_text())
            except Exception:
                continue
            gcore = data.get("tool", {}).get("g-core") or data.get("tool", {}).get("gcore", {})
            starter = gcore.get("starter")
            design = gcore.get("design")
            if not starter and not design:
                continue
            repo_id = f"repo-{toml_file.stem}"
            nodes.append({"id": repo_id, "label": toml_file.stem, "type": "repo"})
            if starter:
                ensure_source(starter)
                links.append({"source": repo_id, "target": f"src-{starter}", "rel": "starter"})
            if design:
                ensure_source(design)
                links.append({"source": repo_id, "target": f"src-{design}", "rel": "design"})

        return {"nodes": nodes, "links": links}

    # ------------------------------------------------------------------ notes
    @api.get("/notes")
    async def list_notes():
        notes = all_notes()
        items = [
            {"path": n.path, "title": n.title, "kind": n.kind, "status": n.status,
             "version": n.version, "tags": n.tags, "updated": n.updated}
            for n in notes
        ]
        items.sort(key=lambda i: (i["kind"], i["title"].lower()))
        return {
            "notes": items,
            "facets": {
                "kinds":    sorted({i["kind"] for i in items}),
                "statuses": sorted({i["status"] for i in items}),
                "tags":     sorted({t for i in items for t in i["tags"]}),
            },
        }

    # ------------------------------------------------------------ single note
    @api.get("/note/{path:path}")
    async def note(path: str):
        n = get_note(path)
        if n is None:
            raise HTTPException(status_code=404, detail="note not found")

        notes = all_notes()
        by_path = {x.path: x for x in notes}
        by_basename = {x.path.rsplit("/", 1)[-1]: x.path for x in notes}

        pseudo_meta, body_clean = _split_pseudo_frontmatter(n.body)
        rewritten = _resolve_wikilinks(body_clean, by_path, by_basename)
        html = _md.render(rewritten)

        backlinks = [
            {"path": x.path, "title": x.title, "kind": x.kind}
            for x in notes
            if x.path != n.path and (
                n.path in set(x.wikilinks) or
                n.path.rsplit("/", 1)[-1] in set(x.wikilinks)
            )
        ]
        forward = []
        for target in n.wikilinks:
            if target in by_path:
                forward.append({"path": target, "title": by_path[target].title,
                                 "kind": by_path[target].kind})
            elif target in by_basename:
                p = by_basename[target]
                forward.append({"path": p, "title": by_path[p].title,
                                 "kind": by_path[p].kind})
            else:
                forward.append({"path": None, "title": target, "kind": "broken"})

        return {
            "path": n.path, "title": n.title, "kind": n.kind, "status": n.status,
            "version": n.version, "lineage": n.lineage, "tags": n.tags,
            "updated": n.updated, "created": n.created,
            "html": html,
            "pseudo_meta": [{"key": k, "value": v} for k, v in pseudo_meta],
            "citations": citations_for(n.path),
            "backlinks": sorted(backlinks, key=lambda b: b["path"]),
            "forward": forward,
        }

    # ------------------------------------------------------------------- refs
    @api.get("/refs")
    async def refs(limit: int = 100):
        return {"summary": refs_summary(), "top": top_refs(limit=limit)}

    @api.get("/refs/graph")
    async def refs_graph():
        if not REFS_DB.exists():
            return {"nodes": [], "links": []}
        conn = sqlite3.connect(REFS_DB)
        rows = conn.execute(
            "SELECT canonical_url, note, status FROM refs"
        ).fetchall()
        conn.close()

        seen_urls: dict[str, bool] = {}
        seen_notes: dict[str, bool] = {}
        nodes, links = [], []

        for url, note_path, status in rows:
            if url not in seen_urls:
                broken = bool(status and (
                    status.startswith("http-4") or
                    status.startswith("http-5") or
                    status.startswith("error-")
                ))
                try:
                    label = urlparse(url).netloc or url[:40]
                except Exception:
                    label = url[:40]
                seen_urls[url] = True
                nodes.append({"id": f"url-{url}", "label": label, "type": "url",
                               "url": url, "broken": broken})
            if note_path not in seen_notes:
                parts = Path(note_path).parts
                kind = parts[0] if len(parts) > 1 else "note"
                label = Path(note_path).stem.replace("-", " ")
                seen_notes[note_path] = True
                nodes.append({"id": f"note-{note_path}", "label": label,
                               "type": "note", "kind": kind})
            links.append({"source": f"note-{note_path}", "target": f"url-{url}"})

        return {"nodes": nodes, "links": links}

    # ------------------------------------------------------------- home stats
    @api.get("/home/stats")
    async def home_stats():
        return {
            "kinds":  kind_counts(),
            "recent": recent_activity(limit=10),
            "refs":   refs_summary(),
        }

    # ------------------------------------------------------------------ status
    @api.get("/status")
    async def status():
        snap = BRAIN_ROOT / "status" / "snapshot.json"
        if not snap.exists():
            raise HTTPException(503, "status snapshot not yet generated")
        return json.loads(snap.read_text())

    # ------------------------------------------------------------------- stale
    @api.get("/stale")
    def stale():
        """Notes standing on ground that moved: the rewrite review queue."""
        return stale_report(root=BRAIN_ROOT)

    # -------------------------------------------------------------- condense
    @api.get("/condense")
    def condense():
        """Candidates for condensation: overlaps, accretion, orphans, drift."""
        return condense_report(root=BRAIN_ROOT)

    # ----------------------------------------------------------------- orient
    @api.get("/orient", response_class=PlainTextResponse)
    def orient(
        cwd: str = Query("", max_length=4096), limit: int = 12,
        project: str = Query("", max_length=100),
        task: str = Query("", max_length=1000),
    ):
        """Compact brain orientation, injected into every session by the
        SessionStart hook.

        The corpus was going unread because reading it cost a deliberate
        `brain_init` call that most sessions never made. This is the
        MEMORY.md-shaped answer: the cluster map plus whatever this
        directory's project already has notes about, for free, every session.
        Keep it terse. It is paid for on every single session start.
        """
        return render_orientation(cwd=cwd, limit=limit, project=project, task=task)

    return api


ORIENTATION_MAX_CHARS = 6000
ORIENTATION_MAX_NOTES = 12
_TASK_STOPWORDS = set("a an and are as at be by can do for from help how i in is it me of on or please the this to we with fix add update implement project task".split())


def _orientation_text(value: object, limit: int) -> str:
    text = " ".join(str(value).split()).replace("`", "'")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _orientation_repo_name(root: Path) -> str:
    """Use the origin repository name for renamed checkouts and worktrees."""
    git_dir = root / ".git"
    try:
        if git_dir.is_file():
            pointer = git_dir.read_text()[:4096].strip()
            if not pointer.startswith("gitdir: "):
                return root.name
            git_dir = root / pointer[8:]
        common = git_dir / "commondir"
        if common.is_file():
            git_dir = git_dir / common.read_text()[:4096].strip()
        config = configparser.ConfigParser(interpolation=None, strict=False)
        config.read(git_dir / "config")
        origin = config.get('remote "origin"', 'url', fallback='')
        name = re.split(r"[/:]", origin.rstrip("/"))[-1].removesuffix(".git")
        if re.fullmatch(r"[\w.-]+", name):
            return name
    except (OSError, ValueError, configparser.Error):
        pass
    return root.name


def _orientation_project(cwd: str, project: str) -> str:
    """Explicit identity wins; otherwise use repo ancestry or known manifest IDs.

    A remote client's path may not exist here, so a manifest ID in its ancestry
    is useful. An arbitrary directory name (especially /root) is not evidence.
    """
    if project.strip():
        return _orientation_text(project, 100).lower()
    if not cwd:
        return ""
    try:
        path = Path(cwd[:4096]).expanduser().resolve()
        ancestors = (path, *path.parents)
        for parent in ancestors:
            if (parent / ".git").exists():
                return _orientation_text(_orientation_repo_name(parent), 100).lower()
        manifest = g_config.MANIFEST_PATH
        data = json.loads(manifest.read_text())
        ids = {str(p["id"]).lower() for p in data.get("projects", [])
               if isinstance(p, dict) and p.get("id")}
        for parent in ancestors:
            if parent.name.lower() in ids:
                return _orientation_text(parent.name, 100).lower()
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
        pass
    return ""


def _orientation_hits(notes: list[Note], project: str, task: str) -> list[Note]:
    tokens = set(re.findall(r"[a-z0-9]{3,}", task[:1000].lower())) - _TASK_STOPWORDS
    project_pattern = re.compile(r"(?<![a-z0-9])" + re.escape(project) + r"(?![a-z0-9])") if project else None
    scored = []
    for note in notes:
        metadata = f"{note.path} {note.title} {' '.join(note.tags)}".lower()
        project_match = bool(project_pattern and project_pattern.search(metadata))
        words = set(re.findall(r"[a-z0-9]+", metadata)) if tokens else set()
        body_words = set(re.findall(r"[a-z0-9]+", note.body[:8000].lower())) if tokens else set()
        task_score = 4 * len(tokens & words) + len(tokens & body_words)
        if not project_match and (project or not task_score):
            continue
        scored.append((note, task_score))
    # Stable tie-breaks independent of filesystem traversal.
    scored.sort(key=lambda item: item[0].path)
    scored.sort(key=lambda item: item[0].updated or "", reverse=True)
    scored.sort(key=lambda item: -item[1])
    return [item[0] for item in scored]


def render_orientation(cwd: str = "", limit: int = 12, project: str = "", task: str = "") -> str:
    """Build the session-start brain block and resolve housekeeping on read."""
    notes = [n for n in all_notes() if is_corpus_note(n.path)]

    lines: list[str] = ["## Brain (long-term corpus, the cortex)"]

    counts = dict.fromkeys(CORPUS_LAYERS, 0)
    for n in notes:
        counts[Path(n.path).parts[0]] += 1
    summary = ", ".join(f"{layer} {count}" for layer, count in counts.items()
                        if count or layer not in ("findings", "references"))
    lines.append(_orientation_text(f"{len(notes)} notes by layer: {summary}", 400))

    slug = _orientation_project(cwd, project)
    hits = _orientation_hits(notes, slug, task)
    relevant: list[str] = []
    if hits:
        label = f"On `{slug}`" if slug else "For this task"
        relevant.append(f"\n**{label}** ({len(hits)} notes, relevance first):")
        budget = ORIENTATION_MAX_CHARS - 3600
        shown = 0
        for n in hits[:max(0, min(limit, ORIENTATION_MAX_NOTES))]:
            flag = " [BROKEN]" if n.status == "broken" else ""
            row = f"- `{_orientation_text(n.path, 180)}`{flag}: {_orientation_text(n.title, 220)}"
            excerpt = next((line for line in n.body[:4000].splitlines()
                            if line.strip() and not line.lstrip().startswith(("#", "---", "```"))), "")
            if excerpt:
                row += f" ({_orientation_text(excerpt, 160)})"
            if len(row) + 1 > budget:
                break
            relevant.append(row)
            budget -= len(row) + 1
            shown += 1
        if shown < len(hits):
            relevant.append(f"… {len(hits) - shown} relevant note(s) omitted; narrow project/task or read the notes directly.")

    broken = sorted((n for n in notes if n.status == "broken"), key=lambda n: n.path)
    if broken:
        lines.append(f"\n{len(broken)} note(s) flagged broken: "
                     + ", ".join(f"`{_orientation_text(n.path, 80)}`" for n in broken[:5]))

    housekeeping: list[str] = []
    data = stale_report(root=BRAIN_ROOT)
    if data.get("outstanding"):
        message = (f"{data['outstanding']} note(s) stand on a dependency that "
                   "moved and haven't been re-checked (`/api/stale`)")
        if data.get("escalating"):
            oldest = ", ".join(f"`{_orientation_text(o['note'], 70)}` ({o['age_days']}d)"
                               for o in data["oldest_detail"][:3])
            message += f"; {data['escalating']} flag(s) aged 14+ days, oldest: {oldest}"
        housekeeping.append(message)

    data = condense_report(root=BRAIN_ROOT)
    if data.get("backlog"):
        c = data.get("counts", {})
        housekeeping.append(
            f"{data['backlog']} condensation candidate(s): "
            f"**{c.get('stale', 0)} possibly outdated**, "
            f"{c.get('overlaps', 0)} overlapping pairs, "
            f"{c.get('accreted', 0)} accreted, {c.get('orphans', 0)} orphans, "
            f"{c.get('drift', 0)} drift (run `/brain-condense`)"
        )

    if housekeeping:
        lines.append("\nHousekeeping: " + "; ".join(_orientation_text(item, 450) for item in housekeeping) + ".")

    lines.extend(relevant)

    lines.append(
        "\n**Writing is part of the work, not a ceremony.** When something "
        "true-always emerges, such as a decision, a finding or how a system "
        "actually behaves, call `brain_note(text, kind)` there and then. One call, no "
        "permission needed, no plan_id. Dated → `create_todo`; true-now → "
        "`pulse_note`. Found something here that's no longer true? "
        "`mark_broken` it. That's unfenced, and a wrong note left unflagged "
        "costs more than a wrong flag. Only the transformative writes stop for "
        "a yes: `supersede`, and `edit_major` on a note the rest of the brain "
        "is built on. `brain_init` has the full manual."
    )
    return "\n".join(lines)


# ---- helpers -----------------------------------------------------------------

def _split_pseudo_frontmatter(body: str) -> tuple[list[tuple[str, str]], str]:
    lines = body.splitlines()
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    has_open_fence = i < len(lines) and lines[i].strip() == "---"
    if has_open_fence:
        i += 1
    pairs: list[tuple[str, str]] = []
    j = i
    while j < len(lines):
        line = lines[j]
        stripped = line.strip()
        if not stripped:
            if has_open_fence:
                j += 1
                continue
            break
        if has_open_fence and stripped == "---":
            j += 1
            break
        m = _YAML_KEY_LINE.match(line)
        if not m:
            break
        pairs.append((m.group(1), m.group(2).strip()))
        j += 1
    if len(pairs) < 3:
        return [], body
    return pairs, "\n".join(lines[j:]).lstrip("\n")


def _resolve_wikilinks(body: str, by_path: dict, by_basename: dict) -> str:
    def repl(m):
        target = m.group(1).strip()
        if target in by_path:
            return f"[{target}](/note/{target})"
        if target in by_basename:
            return f"[{target}](/note/{by_basename[target]})"
        return f"[{target}](#broken)"
    return WIKILINK_RE.sub(repl, body)
