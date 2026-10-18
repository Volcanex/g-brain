"""brain-commit: explicit commit semantics for the brain.

Tools exposed:
  - propose_commit(summary)           # allocate plan_id, give Claude instructions
  - execute_commit(plan_id, ops)      # apply the plan (after user assent)
  - supersede_section(note, section)  # chop + footnote + archive
  - mark_broken(note, reason)         # flip status, write banner
  - query_references(note)            # secondary-graph lookup, on-demand
  - rebuild_indexes()                 # recompile indexes/ from note summaries
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import re
import sqlite3
import subprocess
import uuid
from pathlib import Path
from typing import Any

import frontmatter
from fastmcp import FastMCP

import g_config

BRAIN_ROOT = g_config.BRAIN_ROOT
COMMITS_LOG = BRAIN_ROOT / "commits.log"
REFS_DB = BRAIN_ROOT / ".refs-index.sqlite"

PENDING_PLANS: dict[str, dict[str, Any]] = {}

mcp = FastMCP("brain-commit")


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return dt.date.today().isoformat()


# ----------------------------------------------------------------------
# Canonical layout. Legacy kinds remain available to the commit machinery.
#
# The map is enforced on every write: basic-memory's habit of dropping
# notes into singular `reference/` / `design/` / ad-hoc `project/` is
# what produced the orphan files we cleaned up. The whitelist here is
# the only durable stop on that drift.
# ----------------------------------------------------------------------
KIND_DIRS: dict[str, str] = {
    "design":    "designs",
    "finding":   "findings",
    "idea":      "ideas",
    "fact":      "about",
    "hazard":    "hazards",
    "reference": "references",
    "source":    "sources",
    "log":       "logs",
    "todo":      "todos",
}
CANONICAL_DIRS: set[str] = set(KIND_DIRS.values())
LAYERS = {"design": "design", "idea": "idea", "about": "fact", "hazard": "hazard"}
_LAYER_HELP = (
    "Choose layer: design (your own designs), idea (cross-domain ideas no project owns), "
    "about (facts about Gabriel, preferences, people, decisions and reasons), "
    "hazard (ops hazards and external quirks). Project canon belongs in repo docs. "
    "Every-run rules belong as g-levels gates. Dated life commitments go to create_todo, "
    "machine or agent work to create_ticket, current state to pulse_note."
)


def _validate_target_path(target: str) -> None:
    """Enforce shape: ``<canonical-dir>/<slug>.md``.

    Refuses extensionless paths (foo), non-md extensions (foo.txt),
    nested junk dirs (project/foo.md), and the singular forms
    (reference/foo.md). Raises ValueError with an actionable message.
    """
    if not target or target.startswith("/"):
        raise ValueError(f"target must be a relative path, got {target!r}")
    if not target.endswith(".md"):
        raise ValueError(
            f"target must end with .md, got {target!r}. "
            f"All notes are markdown; append the .md extension."
        )
    parts = target.split("/")
    if len(parts) < 2:
        raise ValueError(
            f"target must be '<kind-dir>/<slug>.md', got {target!r}"
        )
    top = parts[0]
    if top not in CANONICAL_DIRS:
        # Most common foot-gun: agent picked the singular form.
        suggested = next(
            (plural for kind, plural in KIND_DIRS.items() if kind == top),
            None,
        )
        msg = (
            f"top-level dir {top!r} is not a canonical kind dir. "
            f"Allowed: {sorted(CANONICAL_DIRS)}."
        )
        if suggested:
            msg += f" Did you mean {suggested!r}?"
        raise ValueError(msg)


def _resolve(target: str) -> Path:
    """Resolve a target path relative to BRAIN_ROOT, refusing escape
    and refusing non-canonical layouts."""
    _validate_target_path(target)
    p = (BRAIN_ROOT / target).resolve()
    if BRAIN_ROOT.resolve() not in p.parents and p != BRAIN_ROOT.resolve():
        raise ValueError(f"path escapes brain root: {target}")
    return p


def append_commit_log(actor: str, op: str, note: str, before: str, after: str, reason: str) -> None:
    line = f"{_now_iso()}\t{actor}\t{op}\t{note}\t{before}->{after}\t{reason}\n"
    with COMMITS_LOG.open("a") as f:
        f.write(line)


def bump_version(current: str, level: str) -> str:
    parts = [int(x) for x in current.split(".")]
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    if level == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"unknown level {level}")


def _set_lineage(post: frontmatter.Post, level: str, editor: str) -> list[str]:
    """Update lineage in place per the bump rules. Returns the new lineage.

    Lineage is always length 3 (one letter per version segment). A bump updates
    only the bumped slot; finer slots reset to the current editor (since they're
    now back to .0 and the bumper is the implicit author of those zeros).
    """
    existing = list(post.get("lineage") or [])
    while len(existing) < 3:
        existing.append(editor)
    if level == "major":
        new = [editor, editor, editor]
    elif level == "minor":
        new = [existing[0], editor, editor]
    elif level == "patch":
        new = [existing[0], existing[1], editor]
    else:
        raise ValueError(level)
    post["lineage"] = new
    return new


def _apply_edits(post: frontmatter.Post, edits: list[dict[str, Any]]) -> None:
    """Apply a list of content edits to post.content in order.

    Each edit is one of:
      {operation: "append", content: "..."}
      {operation: "prepend", content: "..."}
      {operation: "find_replace", find: "...", replace: "...", count?: int}
      {operation: "replace_section", heading: "## Foo", content: "..."}
    """
    body = post.content
    for edit in edits:
        # propose_commit's instructions have long said `op`, this reader has
        # long wanted `operation`. Agents following the docs hit a KeyError.
        # Accept either rather than making the caller guess.
        op = edit.get("operation") or edit.get("op")
        if op is None:
            raise ValueError(f"content edit missing 'operation': {edit!r}")
        if op == "append":
            body = body.rstrip() + "\n\n" + edit["content"].rstrip() + "\n"
        elif op == "prepend":
            body = edit["content"].rstrip() + "\n\n" + body.lstrip()
        elif op == "find_replace":
            count = edit.get("count", 0)
            if count:
                body = body.replace(edit["find"], edit["replace"], count)
            else:
                body = body.replace(edit["find"], edit["replace"])
        elif op == "replace_section":
            body = _replace_section(body, edit["heading"], edit["content"])
        else:
            raise ValueError(f"unknown content edit op: {op}")
    post.content = body


def _split_sections(body: str) -> list[tuple[str, str]]:
    """Split markdown body into (heading_line_or_empty, section_text) pairs.

    The first chunk has heading="" if the body doesn't start with a heading.
    Each subsequent chunk's heading is the line that introduced it.
    """
    lines = body.splitlines(keepends=True)
    chunks: list[tuple[str, list[str]]] = [("", [])]
    heading_re = re.compile(r"^(#{1,6})\s+.*$")
    for line in lines:
        if heading_re.match(line.rstrip("\n")):
            chunks.append((line.rstrip("\n"), [line]))
        else:
            chunks[-1][1].append(line)
    return [(h, "".join(ls)) for h, ls in chunks]


def _replace_section(body: str, heading: str, new_content: str) -> str:
    """Replace the section under `heading` (matched verbatim, ignoring trailing whitespace)."""
    target = heading.strip()
    out: list[str] = []
    chunks = _split_sections(body)
    replaced = False
    for h, text in chunks:
        if h.strip() == target and not replaced:
            out.append(h + "\n" + new_content.rstrip() + "\n")
            replaced = True
        else:
            out.append(text)
    if not replaced:
        # Append as new section
        out.append("\n" + heading.rstrip() + "\n" + new_content.rstrip() + "\n")
    return "".join(out)


def _slice_section(body: str, heading: str) -> tuple[str, str]:
    """Return (body_without_section, removed_section_text). Removed includes the heading."""
    target = heading.strip()
    kept: list[str] = []
    removed_parts: list[str] = []
    found = False
    for h, text in _split_sections(body):
        if h.strip() == target and not found:
            removed_parts.append(text)
            found = True
        else:
            kept.append(text)
    return "".join(kept), "".join(removed_parts)


@mcp.tool()
def propose_commit(summary: str, mode: str = "auto") -> dict:
    """Allocate a plan_id and return instructions for composing a commit plan.

    Args:
      summary: what was learned/decided in this session.
      mode: "auto" (commit after showing plan) or "review" (wait for explicit assent).

    Returns:
      A dict with plan_id and instructions. The caller (Claude) is expected to:
        1. Use Basic Memory's search_notes to find related notes for the summary.
        2. Compose a list of operations (edit_minor, edit_patch, supersede,
           mark_broken, create).
        3. If any operation is structural (supersede, mark_broken, major bump,
           multiple notes touched), set mode="review" and present the plan to
           the user before calling execute_commit.
        4. Call execute_commit(plan_id, operations, actor).
    """
    plan_id = uuid.uuid4().hex[:8]
    plan = {
        "plan_id": plan_id,
        "summary": summary,
        "mode": mode,
        "operations": [],
        "created": _now_iso(),
    }
    PENDING_PLANS[plan_id] = plan
    return {
        "plan_id": plan_id,
        "summary": summary,
        "mode": mode,
        "instructions": (
            "1. Use search_notes to find related notes. "
            "2. Compose operations. Each op is a dict with at least "
            "{type, target}. type ∈ {create, edit_minor, edit_patch, "
            "supersede, mark_broken}. "
            "target MUST be '<plural-kind-dir>/<slug>.md', one of: "
            "designs/, ideas/, about/, hazards/, sources/, logs/, todos/ "
            "(findings/ and references/ for legacy notes). "
            ""
            "FOR create: pass 'body' (full markdown, field name is 'body' "
            "not 'content'); optionally title, kind, frontmatter. Empty body rejected. "
            ""
            "FOR edit_minor / edit_patch / edit_major: pass exactly one of "
            "'body' (replaces the whole markdown body, snapshots prior version "
            "to .history/), 'edits' (list of {op:append|prepend|replace_section|"
            "find_replace, ...}), or 'frontmatter_only': true (bump version + "
            "frontmatter without touching body). Optional 'frontmatter' dict "
            "overrides specific YAML fields on any edit op. Passing both "
            "'body' and 'edits' is rejected; passing none of the three is rejected. "
            ""
            "FOR supersede: archives prior major; needs assent (see below). "
            "FOR mark_broken: pass reason and optionally last_working_version. "
            ""
            "3. Call execute_commit(plan_id, operations, actor). "
            "actor is PROVENANCE, not permission. Who composed the write: "
            "'agent' (you, default), 'human' (the owner dictated it verbatim), "
            "'ai-auto' (cron, nobody watching). Record it honestly; it sets "
            "the note's lineage. "
            "4. supersede and edit_major OVERWRITE existing knowledge, so they "
            "need the owner's yes: ask them (two lines on what changes and why) "
            "then set assent=True on that op and execute. Keep actor='agent'; "
            "you asked, they approved, you wrote it. Everything else is yours to "
            "commit on your own judgement, and a plain additive capture "
            "shouldn't be in this flow at all, use brain_note. "
            ""
            "SUMMARY FIELD: every note should have a 'summary' in frontmatter: "
            "1-2 sentences (~30 tokens) describing what question this note answers. "
            "Include it in 'frontmatter' on create ops. Update it via frontmatter "
            "override on edit_minor ops when content changes substantially. "
            ""
            "5. After execute_commit succeeds, call rebuild_indexes() to refresh "
            "indexes/master.md and the relevant topic index."
        ),
    }


@mcp.tool()
def execute_commit(plan_id: str, operations: list[dict], actor: str = "agent") -> dict:
    """Execute a proposed plan.

    `actor` is **provenance, not permission**. Who composed this write:
    "human" (the owner dictated it), "agent" (you, in a live session), or
    "ai-auto" (cron, nobody watching). It sets the note's lineage, so record
    it honestly.

    Authorisation for a destructive op is carried separately, by `assent` on
    the op itself. See `_authorise`: an agent may supersede or major-bump when
    it has asked and been told yes. It does not have to claim to be the owner
    to do it.
    """
    plan = PENDING_PLANS.get(plan_id)
    if plan is None:
        return {"error": f"unknown plan_id {plan_id}"}

    for op in operations:
        refusal = _authorise(op, actor)
        if refusal:
            return {"error": refusal}

    results: list[dict] = []
    for op in operations:
        try:
            results.append(_apply_op(op, actor))
        except Exception as e:
            results.append({"ok": False, "op": op.get("type"), "error": f"{type(e).__name__}: {e}"})

    plan["operations"] = operations
    plan["executed"] = _now_iso()
    plan["actor"] = actor
    return {"plan_id": plan_id, "results": results}


def _apply_op(op: dict, actor: str) -> dict:
    editor = "H" if actor == "human" else "A"
    op_type = op["type"]

    if op_type == "create":
        return _create_note(op, actor)
    if op_type == "mark_broken":
        return _do_mark_broken(op["target"], op.get("reason", ""),
                               op.get("last_working_version", ""), actor)
    if op_type == "supersede":
        return _do_supersede(op, actor)

    if op_type in ("edit_patch", "edit_minor", "edit_major"):
        level = op_type.removeprefix("edit_")
        refusal = _authorise(op, actor)
        if refusal:
            return {"ok": False, "error": refusal}
        try:
            target = _resolve(op["target"])
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not target.exists():
            return {"ok": False, "error": f"note does not exist: {op['target']}"}

        # Edit-op input shape: exactly one of these three must be true.
        # Until 2026-05-04 a `body` field passed here was silently dropped
        # while the version still bumped, producing notes whose version
        # didn't reflect their content. The validators below close that.
        has_body = op.get("body") is not None
        has_edits = bool(op.get("edits"))
        frontmatter_only = bool(op.get("frontmatter_only"))

        if has_body and has_edits:
            return {"ok": False, "error": (
                "edit_* received both 'body' and 'edits'. Pass exactly one. "
                "'body' replaces the entire markdown body wholesale. "
                "'edits' is a list of structured per-section operations "
                "(append / prepend / replace_section / find_replace)."
            )}
        if not has_body and not has_edits and not frontmatter_only:
            return {"ok": False, "error": (
                f"{op_type} needs content. Pass one of: "
                "'body' (full markdown replacement), "
                "'edits' (list of structured per-section ops), or "
                "'frontmatter_only': true (bump version + tweak frontmatter "
                "without touching body, e.g. for status / tag / expires updates)."
            )}
        if has_body and not str(op["body"]).strip():
            return {"ok": False, "error": (
                "edit_* received empty 'body'. Refusing: empty body is almost "
                "always a parameter-name confusion. If you really want to clear "
                "the body, supersede the note instead."
            )}

        post = frontmatter.load(target)
        before_v = str(post.get("version", "0.0.0"))
        new_v = bump_version(before_v, level)

        # Snapshot the prior version before mutating, but only when
        # content is actually changing. Pure frontmatter bumps don't
        # need a snapshot (the content is unchanged and the commits.log
        # already records who/when).
        if has_body or has_edits:
            _snapshot_prior(target, before_v, post)

        post["version"] = new_v
        _set_lineage(post, level, editor)
        post["updated"] = _today()

        if has_body:
            post.content = str(op["body"]).rstrip() + "\n"
        elif has_edits:
            _apply_edits(post, op["edits"])

        # Apply optional frontmatter overrides (works for any edit type).
        for key, value in (op.get("frontmatter") or {}).items():
            post[key] = value

        target.write_text(frontmatter.dumps(post) + "\n")
        append_commit_log(actor, op_type, op["target"], before_v, new_v, op.get("reason", ""))

        # A major bump or a wholesale body replacement means the thing a
        # dependent read is no longer there. Appends and section edits don't
        # qualify. Flagging on those would cry wolf and get ignored.
        stamped: list[str] = []
        if level == "major" or has_body:
            stamped = _stamp_dependents(
                op["target"],
                f"{op['target']} rewritten {before_v} -> {new_v}",
                actor,
            )

        return {
            "ok": True,
            "target": op["target"],
            "before_version": before_v,
            "new_version": new_v,
            "dependents_flagged": stamped,
            "body_replaced": has_body,
            "edits_applied": len(op.get("edits") or []) if has_edits else 0,
            "frontmatter_only": frontmatter_only,
        }

    return {"ok": False, "error": f"unknown op type {op_type}"}


def _snapshot_prior(target: Path, before_v: str, post: frontmatter.Post) -> None:
    """Write the pre-edit version of a note to history so the change is
    reversible.

    Two conventions, picked from the file's shape:

    - Folder-shaped designs (``<dir>/<slug>/current.md``) snapshot to a
      sibling ``versions/v<before>.md``, visible, the buildbook layout.
    - Flat notes (``<dir>/<slug>.md``) snapshot to a hidden
      ``.history/<slug>__v<before>.md`` so the brain reader and the
      secondary refs index skip the snapshots automatically (they
      filter dot-prefixed dirs).

    Snapshots are immutable: if the target path already exists, this
    is a no-op (we don't double-snapshot the same prior version, e.g.
    if two patch bumps happen back-to-back from the same base).
    """
    if target.name == "current.md":
        snap_dir = target.parent / "versions"
        snap_path = snap_dir / f"v{before_v}.md"
    else:
        snap_dir = target.parent / ".history"
        snap_path = snap_dir / f"{target.stem}__v{before_v}.md"
    snap_dir.mkdir(parents=True, exist_ok=True)
    if not snap_path.exists():
        snap_path.write_text(frontmatter.dumps(post) + "\n")


def _create_note(op: dict, actor: str) -> dict:
    try:
        target = _resolve(op["target"])
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if target.exists():
        return {"ok": False, "error": f"note already exists: {op['target']}"}

    fm = dict(op.get("frontmatter") or {})
    fm.setdefault("title", op.get("title", target.stem))
    # Default the kind from the target dir so frontmatter agrees with
    # filesystem layout. Inverse map: dir → kind.
    dir_to_kind = {v: k for k, v in KIND_DIRS.items()}
    inferred_kind = dir_to_kind.get(op["target"].split("/", 1)[0], "log")
    fm.setdefault("kind", op.get("kind", inferred_kind))
    if fm["kind"] in KIND_DIRS and KIND_DIRS[fm["kind"]] != op["target"].split("/", 1)[0]:
        return {"ok": False, "error": (
            f"kind {fm['kind']!r} disagrees with target dir "
            f"{op['target'].split('/', 1)[0]!r} "
            f"(should be {KIND_DIRS[fm['kind']]!r})"
        )}
    fm.setdefault("version", "1.0.0")
    creator = "H" if actor == "human" else "A"
    fm.setdefault("lineage", [creator, creator, creator])
    fm.setdefault("status", "active")
    fm.setdefault("created", _today())
    fm.setdefault("updated", _today())
    fm.setdefault("tags", [])

    # Body parameter resolution. The historical schema used ``content``;
    # ``body`` reads more naturally and several agents reach for it. We
    # accept either, but loudly refuse if the caller passes both with
    # different values (likely a bug: silent precedence here is how we
    # ended up with empty notes earlier).
    body_arg = op.get("body")
    content_arg = op.get("content")
    if body_arg is not None and content_arg is not None and body_arg != content_arg:
        return {"ok": False, "error": (
            "received both 'body' and 'content' with different values; "
            "use only one (prefer 'body')"
        )}
    body = body_arg if body_arg is not None else (content_arg or "")
    if not body.strip():
        # Empty body is almost always a parameter-name confusion, not
        # an intentional placeholder. Refuse it: the cost of a follow-up
        # call is dwarfed by the cost of an empty stub note.
        return {"ok": False, "error": (
            "refusing to create note with empty body. "
            "Pass the markdown body in the 'body' field of the operation."
        )}

    target.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(body, **fm)
    target.write_text(frontmatter.dumps(post) + "\n")
    append_commit_log(actor, "create", op["target"], "-", str(fm["version"]), op.get("reason", ""))
    return {"ok": True, "target": op["target"], "kind": fm["kind"]}


def _do_mark_broken(note: str, reason: str, last_working_version: str, actor: str) -> dict:
    target = _resolve(note)
    post = frontmatter.load(target)
    prev_status = str(post.get("status", "active"))
    post["status"] = "broken"
    post["broken_reason"] = reason
    post["broken_at"] = _today()

    banner_lines = [
        f"> WARN **This note is flagged BROKEN as of {post['broken_at']}.**",
        f"> Reason: {reason}",
    ]
    if last_working_version:
        banner_lines.append(f"> Last working version: {last_working_version}")
    banner = "\n".join(banner_lines) + "\n"

    if "flagged BROKEN" not in post.content:
        post.content = banner + "\n" + post.content

    target.write_text(frontmatter.dumps(post) + "\n")
    append_commit_log(actor, "mark_broken", note, prev_status, "broken", reason)
    stamped = _stamp_dependents(note, f"depends on broken note {note}", actor)
    return {"ok": True, "note": note, "dependents_flagged": stamped}


@mcp.tool()
def mark_broken(note: str, reason: str, last_working_version: str = "",
                actor: str = "agent") -> dict:
    """Flip a note's status to broken and write the warning banner. Idempotent.

    Not fenced: flagging something as no-longer-true destroys nothing, and a
    stale note left unflagged is the more expensive mistake. Use your own
    judgement and say so afterwards. `actor` is provenance, leave it as
    "agent" when the call is yours.
    """
    return _do_mark_broken(note, reason, last_working_version, actor=actor)


def _do_supersede(op: dict, actor: str) -> dict:
    """Major-bump a design-shaped note. Archives current.md to versions/v{old_major}.md
    then writes a new current.md with bumped major version and provided content.
    """
    refusal = _authorise(op, actor)
    if refusal:
        return {"ok": False, "error": refusal}
    target = _resolve(op["target"])  # path to current.md
    if target.name != "current.md":
        return {"ok": False, "error": "supersede only operates on current.md notes"}

    post = frontmatter.load(target)
    before_v = str(post.get("version", "0.0.0"))
    old_major = int(before_v.split(".")[0])

    versions_dir = target.parent / "versions"
    versions_dir.mkdir(exist_ok=True)
    archive_path = versions_dir / f"v{old_major}.md"
    if archive_path.exists():
        return {"ok": False, "error": f"archive already exists: {archive_path.relative_to(BRAIN_ROOT)}"}

    archived = frontmatter.loads(frontmatter.dumps(post))
    archived["status"] = "superseded"
    archive_path.write_text(frontmatter.dumps(archived) + "\n")

    new_v = bump_version(before_v, "major")
    new_post = frontmatter.loads(frontmatter.dumps(post))
    new_post["version"] = new_v
    # Lineage follows provenance, not authorisation: an approved supersede
    # composed by an agent is an agent's writing, and the note should say so.
    _editor = "H" if actor == "human" else "A"
    new_post["lineage"] = [_editor, _editor, _editor]
    new_post["status"] = "active"
    new_post["updated"] = _today()
    new_post["supersedes"] = f"v{old_major}"
    new_post.metadata.pop("broken_reason", None)
    new_post.metadata.pop("broken_at", None)
    if "content" in op:
        new_post.content = op["content"]
    target.write_text(frontmatter.dumps(new_post) + "\n")

    rel = str(target.relative_to(BRAIN_ROOT))
    append_commit_log(actor, "supersede", rel, before_v, new_v, op.get("reason", ""))
    stamped = _stamp_dependents(
        rel, f"{rel} superseded {before_v} -> {new_v}", actor
    )
    return {
        "ok": True,
        "target": rel,
        "archived_to": str(archive_path.relative_to(BRAIN_ROOT)),
        "before_version": before_v,
        "new_version": new_v,
        "dependents_flagged": stamped,
    }


@mcp.tool()
def supersede_section(note: str, section_heading: str, reason: str,
                      replacement_content: str = "", assent: bool = False) -> dict:
    """Cut a section from an active note, leave a one-line stub, archive the cut content.

    Removes existing knowledge, so it needs the owner's yes: ask, showing which
    section and why, then call again with assent=True. The cut content is
    archived either way, for folder-shaped notes (current.md) to
    ../versions/<heading-slug>-<date>.md, for flat notes to an `archive/`
    sibling folder.
    """
    refusal = _authorise({"type": "supersede", "target": note, "assent": assent}, "agent")
    if refusal:
        return {"ok": False, "error": refusal}
    target = _resolve(note)
    post = frontmatter.load(target)
    body = post.content

    new_body, removed = _slice_section(body, section_heading)
    if not removed:
        return {"ok": False, "error": f"section not found: {section_heading}"}

    if target.name == "current.md":
        archive_dir = target.parent / "versions"
    else:
        archive_dir = target.parent / "archive"
    archive_dir.mkdir(exist_ok=True)

    slug = re.sub(r"[^a-z0-9]+", "-", section_heading.lower()).strip("-")[:60] or "section"
    archive_path = archive_dir / f"{target.stem}-{slug}-{_today()}.md"
    n = 1
    while archive_path.exists():
        archive_path = archive_dir / f"{target.stem}-{slug}-{_today()}-{n}.md"
        n += 1
    archive_path.write_text(
        f"---\nfrom: {note}\ncut_at: {_today()}\nreason: {reason}\n---\n\n{removed}"
    )

    rel_archive = archive_path.relative_to(BRAIN_ROOT)
    stub = f"\n_Section archived {_today()}: see [[{rel_archive.as_posix()}]] ({reason})_\n"
    if replacement_content:
        stub = "\n" + replacement_content.rstrip() + "\n" + stub
    post.content = new_body.rstrip() + "\n" + stub

    before_v = str(post.get("version", "0.0.0"))
    new_v = bump_version(before_v, "minor")
    post["version"] = new_v
    _set_lineage(post, "minor", "H")
    post["updated"] = _today()

    target.write_text(frontmatter.dumps(post) + "\n")
    append_commit_log("human", "supersede_section", note, before_v, new_v,
                      f"{section_heading}: {reason}")
    return {
        "ok": True,
        "note": note,
        "archived_to": str(rel_archive),
        "new_version": new_v,
    }


# ----------------------------------------------------------------------
# Provenance vs permission.
#
# `actor` used to mean both "who wrote this" (lineage) and "who is allowed to
# do this" (authorisation). Conflating them had one bad consequence: a
# confident agent's only route to a supersede was to pass actor="human" and
# thereby lie about the lineage of the note. So they're split.
#
#   actor:  provenance. human | agent | ai-auto. Recorded, never inferred.
#   assent: authorisation for THIS destructive op. Set it when you asked
#           the owner and they said yes.
#
# The rule: additive writes need neither. Destructive writes (supersede,
# edit_major) need a human in the loop who said yes, but the *agent* may be
# the one who composed and executed them. Cron (ai-auto) can never do them at
# all, because there is nobody there to ask.
#
# Discretion is the agent's: commit when confident, ask when overwriting
# something load-bearing or when you aren't sure. The fence only stops the
# case where nobody could have been asked.
# ----------------------------------------------------------------------

DESTRUCTIVE_OPS = ("supersede", "edit_major")

# What makes an overwrite worth interrupting the owner for. Gating on the op
# name alone was wrong: it made "merge two duplicate findings" cost the same
# as "rewrite the design three other notes are built on", so a condenser run
# would ask a dozen times and get a dozen rubber stamps. A gate that fires
# often is a gate nobody reads.
#
# So the question is what's actually at stake:
#   - designs are the canonical, versioned answer to a structural question;
#     superseding one changes what the system believes.
#   - anything several notes lean on has real blast radius.
# Everything else is recoverable (.history/ snapshots, commits.log, and the
# vault is a git repo) and is the agent's call.
# Tuned against the real corpus (84 notes, 2026-08-17). Gating on "is a
# design" caught 52% of notes, which is not a gate, it's a toll booth, and a
# toll booth gets waved through. Blast radius >= 8 catches 8%: the notes the
# rest of the brain is genuinely built on.
#
# `supersede` is exempt from the threshold and always asks. It only operates
# on folder-shaped current.md notes: there are four of them, they are the
# canonical versioned designs, and superseding one archives a major version.
# Rare by construction, so asking every time costs almost nothing.
TRANSFORMATIVE_DEPENDENTS = 8


def _transformative(op_type: str, target: str) -> tuple[bool, str]:
    """Is overwriting this note a big enough deal to interrupt a human for?"""
    try:
        from brain_reader import dependents, normalize_path
    except Exception:  # noqa: BLE001 - if the graph is unavailable, ask
        return True, "dependency graph unavailable"

    key = normalize_path(target)

    if op_type == "supersede":
        return True, (
            f"{key} is a canonical versioned design; superseding archives a "
            "major version and changes what the system believes"
        )

    try:
        deps = dependents(key, hops=1)
    except Exception:  # noqa: BLE001
        return True, "could not compute blast radius"

    if len(deps) >= TRANSFORMATIVE_DEPENDENTS:
        names = ", ".join(d["path"] for d in deps[:4])
        return True, f"{len(deps)} notes are built on {key} ({names}…)"

    return False, ""


def _authorise(op: dict, actor: str) -> str | None:
    """Return a refusal message, or None if the op may proceed.

    Permission is a rare gate, deliberately. Most overwrites are the agent's
    to make on its own judgement; only the transformative ones stop for a yes.
    """
    if op.get("type") not in DESTRUCTIVE_OPS:
        return None

    if actor.startswith("ai-auto"):
        return (
            f"unattended actor cannot perform {op['type']}: there is nobody "
            "to ask if it turns out to matter. Use mark_broken to flag it for "
            "review instead, or run this with a human in the session."
        )

    if op.get("assent"):
        return None

    big, why = _transformative(op["type"], op.get("target", ""))
    if not big:
        return None  # agent's call: commit it and mention what you did

    return (
        f"{op['type']} on {op.get('target', '?')} is transformative: {why}. "
        "This is one of the rare ones worth asking about: show the owner what "
        "changes and why in a couple of lines, then re-run with assent=True. "
        "Keep actor as your own ('agent'): assent records that they approved it, "
        "actor records that you wrote it."
    )


# ----------------------------------------------------------------------
# Blast radius.
#
# A rewrite is only safe if the notes standing on top of it find out. The
# manual has described "broken propagation" since the start, but it was
# written as an instruction to the *reading* agent: nothing computed it,
# so a supersede left its dependents silently wrong.
#
# So every structural write now stamps its dependents with `stale_dep`.
# That is deliberately weaker than `status: broken`: it means "the ground
# under this note moved, nobody has checked whether it still holds". The
# verifier cron (brain_verify.py) is what resolves the flag later.
# ----------------------------------------------------------------------

def _stamp_dependents(note: str, reason: str, actor: str) -> list[dict]:
    """Flag every note that wikilinks to `note` as having a moved dependency."""
    try:
        from brain_reader import dependents, normalize_path
    except Exception:  # noqa: BLE001 - never fail a write on the graph
        return []

    key = normalize_path(note)
    try:
        deps = dependents(key, hops=1)
    except Exception:  # noqa: BLE001
        return []

    stamped = []
    for dep in deps:
        path = BRAIN_ROOT / f"{dep['path']}.md"
        if not path.is_file():
            continue
        try:
            post = frontmatter.load(path)
            existing = post.get("stale_dep") or []
            if not isinstance(existing, list):
                existing = []
            # Every rewrite gets a fresh identity, including same-day rewrites.
            # A review of an earlier stamp must never clear a later change.
            existing = [e for e in existing
                        if not (isinstance(e, dict) and e.get("note") == key)]
            existing.append({"note": key, "since": _today(), "reason": reason,
                             "stamp": uuid.uuid4().hex})
            post["stale_dep"] = existing
            path.write_text(frontmatter.dumps(post) + "\n")
            append_commit_log(actor, "stale_dep", dep["path"], "-", key, reason)
            stamped.append(dep["path"])
        except Exception:  # noqa: BLE001 - one bad note must not stop the sweep
            continue
    return stamped


@mcp.tool()
def review_dependency(note: str, dependency: str, expected_stamp: str,
                      reason: str, actor: str = "agent") -> dict:
    """Record an explicit review of ONE stale dependency after reading both notes.

    expected_stamp must match stale_dep.stamp (or its exact since value for
    legacy flags without a stamp). Explain why the dependent remains valid or
    how it was corrected. Unattended reviews are refused. The verifier clears
    only matching review evidence; a newer dependency rewrite invalidates it.
    """
    if actor not in {"agent", "human"}:
        return {"ok": False, "error": "dependency review requires a human-in-session actor"}
    if len(reason.strip().split()) < 4:
        return {"ok": False, "error": "explain why this dependency is now verified (at least four words)"}
    from brain_reader import normalize_path
    dest = note if note.endswith(".md") else note + ".md"
    try:
        path = _resolve(dest)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not path.is_file():
        return {"ok": False, "error": "note does not exist"}
    post = frontmatter.load(path)
    dependency = normalize_path(dependency)
    flags = post.get("stale_dep") or []
    if not isinstance(flags, list):
        return {"ok": False, "error": "malformed dependency flags; repair before reviewing"}
    match = next((f for f in flags if isinstance(f, dict)
                  and f.get("note") == dependency
                  and str(f.get("stamp") or f.get("since") or "") == expected_stamp), None)
    if not expected_stamp or match is None:
        return {"ok": False, "error": "dependency flag changed or is missing; read it again before reviewing"}
    reviews = post.get("dependency_reviews") or []
    if not isinstance(reviews, list):
        return {"ok": False, "error": "malformed dependency reviews; repair before reviewing"}
    reviews = [r for r in reviews if isinstance(r, dict) and r.get("note") != dependency]
    reviews.append({"note": dependency, "stamp": expected_stamp,
                    "reason": reason.strip(), "actor": actor, "reviewed_at": _now_iso()})
    return _apply_op({"type": "edit_patch", "target": dest, "frontmatter_only": True,
                      "frontmatter": {"dependency_reviews": reviews},
                      "reason": f"review dependency {dependency} ({expected_stamp}): {reason.strip()}"}, actor)


# ----------------------------------------------------------------------
# The front door.
#
# propose_commit -> compose ops -> execute_commit -> rebuild_indexes is the
# right protocol for a supersede. It is far too much ceremony for "the export
# job runs at 02:00", and the cost is why the brain went dozens of sessions
# without a commit while `pulse_note` (one string, one call) got used
# constantly. brain_note is the pulse_note-shaped door onto the same
# versioned machinery: one call, one fact, no plan_id.
#
# It can only create or append. Destructive writes use propose_commit.
# mark_broken remains available directly because it destroys no knowledge.
# ----------------------------------------------------------------------

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    slug = _SLUG_STRIP.sub("-", text.lower()).strip("-")
    return slug[:60].strip("-") or "note"


# Titles and summaries end up in interface chrome: table cells, page heads,
# graph labels. None of those render markdown, so the syntax survives on
# screen as literal asterisks and backticks. Captures almost always open with
# a bolded claim, and the title is then cut to eight words, which strips the
# closing ** and leaves an orphan opener. Flatten before deriving, not after.
_MD_PATTERNS = [
    (re.compile(r"```.*?```", re.S), " "),          # fenced code
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),   # image, keep the alt
    (re.compile(r"\[\[[^\]|]*\|([^\]]*)\]\]"), r"\1"),  # labelled wikilink
    (re.compile(r"\[\[([^\]]*)\]\]"), r"\1"),          # bare wikilink
    (re.compile(r"\[([^\]]*)\]\([^)]*\)"), r"\1"),     # link, keep the text
    (re.compile(r"^\s{0,3}#{1,6}\s+", re.M), ""),  # heading marker
    (re.compile(r"^\s{0,3}>\s?", re.M), ""),       # quote marker
    (re.compile(r"^\s{0,3}([-*+]|\d+\.)\s+", re.M), ""),  # list marker
    (re.compile(r"`+([^`]*)`+"), r"\1"),            # code span
    (re.compile(r"\*\*\*(.*?)\*\*\*", re.S), r"\1"),
    (re.compile(r"\*\*(.*?)\*\*", re.S), r"\1"),
    (re.compile(r"\*(?=\S)(.*?\S)\*", re.S), r"\1"),
    # Underscores pair only at word boundaries; snake_case is far commoner
    # in these notes than _emphasis_ and must survive intact.
    (re.compile(r"(^|\W)___(.*?)___(?!\w)", re.S), r"\1\2"),
    (re.compile(r"(^|\W)__(.*?)__(?!\w)", re.S), r"\1\2"),
    (re.compile(r"(^|\W)_(?=\S)(.*?\S)_(?!\w)", re.S), r"\1\2"),
    (re.compile(r"~~(.*?)~~", re.S), r"\1"),
    (re.compile(r"[*`]"), ""),                     # opener left by a cut
]


def _flatten_markdown(text: str) -> str:
    """Markdown source down to plain text, for anything rendered as a label."""
    for pattern, repl in _MD_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _derive_title(text: str) -> str:
    """Fallback title from the first line. Callers should pass `title`."""
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "note")
    first = _flatten_markdown(first).strip()
    # A fact's opening sentence is not a title; take the clause before the
    # first break and cap it, so the derived slug stays addressable.
    for sep in (":", " — ", ". ", ", "):
        head, found, _ = first.partition(sep)
        if found and len(head.split()) >= 3:
            first = head
            break
    return " ".join(first.split()[:8]).rstrip(".,:;—-") or "note"


def _derive_summary(text: str) -> str:
    """First sentence, for the frontmatter summary the indexes render."""
    flat = " ".join(_flatten_markdown(text).split())
    for stop in (". ", "? ", "! "):
        head, sep, _ = flat.partition(stop)
        if sep and len(head) < 200:
            return head + sep.strip()
    return flat[:200]


_BAR_QUESTION = "Would a future agent be worse off without this, and is it recorded nowhere else?"
# Invented paraphrases score >= 0.571, related distinct facts <= 0.167.
NEAR_DUPLICATE_THRESHOLD = 0.55
_RULE_DIRECTIVE = re.compile(
    r"(?:^|[.!?\n]\s*)(?:[-*#>]+\s*)?(?:always|never|do not|every time)\b"
    r"|\bmust\b|\b(?:you|agents?|we)\s+(?:should\s+)?(?:always|never)\b",
    re.I,
)


def _near_duplicates(title: str, text: str) -> list[dict]:
    """Use the reader cache and condense's tokens, failing open on read errors."""
    try:
        from g_brain.condense import _tokens, _jaccard
        from brain_reader import all_notes

        tokens = _tokens(f"{title} {text}")
        folders = {KIND_DIRS[kind] for kind in LAYERS.values()}
        hits = []
        for note in all_notes():
            parts = Path(note.path).parts
            if not parts or parts[0] not in folders:
                continue
            if any(p in {"versions", "archive"} for p in parts):
                continue
            score = _jaccard(tokens, _tokens(f"{note.title} {note.body}"))
            if score >= NEAR_DUPLICATE_THRESHOLD:
                hits.append({"target": f"{note.path}.md", "title": note.title,
                             "score": round(score, 3)})
        return sorted(hits, key=lambda hit: (-hit["score"], hit["target"]))[:3]
    except Exception:  # noqa: BLE001 - a failed duplicate check must never block capture
        return []


@mcp.tool()
def brain_note(
    text: str,
    layer: str | None = None,
    target: str | None = None,
    title: str | None = None,
    tags: list[str] | None = None,
    actor: str = "agent",
    summary: str | None = None,
    *,
    bar: str | None = None,
    distinct: bool = False,
    kind: str | None = None,
) -> dict:
    """Capture durable knowledge in a required layer. One call, no plan_id.

    Capture freely when a future agent would be worse off without it and it
    is recorded nowhere else. Project canon goes in repo docs, every-run rules
    in g-levels gates, dated life commitments in create_todo, machine or agent
    work in create_ticket, current state in pulse_note.

    Args:
      text:   the fact, in markdown. One fact per note.
      layer:  required: design (own designs), idea (unowned cross-domain ideas),
              about (facts about Gabriel), hazard (ops hazards, external quirks).
      bar:    one-line answer to the capture bar above, required for a new note.
      distinct: bypass the near-duplicate refusal for a truly separate new note.
      kind:   deprecated alias accepting design or idea. Use layer instead.
      target: optional existing note to append to (e.g. "designs/g-site.md").
              Omit to let the note be filed by title; if a note with that slug
              already exists, the text is appended to it rather than colliding.
      title:  optional; derived from the first line when omitted.
      tags:   optional list of tags.
      summary: current summary of the WHOLE note, including this capture.
              Supply on corrections. An append without it marks the summary
              for review instead of advertising the old summary as current.
      actor:  "agent" (default, autonomous capture, lineage A), "human" when
              the owner dictated it verbatim, "ai-auto" for cron-driven writes.

    Near-duplicates return candidates: append with target= or pass distinct=True.
    Standing directives get an advisory g-levels hint. Destructive writes use
    propose_commit, with assent for supersede and transformative edit_major.
    mark_broken is available directly and does not need assent.
    """
    if not text or not text.strip():
        return {"ok": False, "error": "refusing to write an empty note"}

    if kind is not None:
        if kind not in {"design", "idea"} or layer not in (None, kind):
            return {"ok": False, "error": f"kind is deprecated. {_LAYER_HELP}"}
        layer = kind
    if layer not in LAYERS:
        return {"ok": False, "error": f"layer is required. {_LAYER_HELP}"}
    note_kind = LAYERS[layer]
    folder = KIND_DIRS[note_kind]

    if summary is not None and not summary.strip():
        return {"ok": False, "error": "summary must describe the current whole note"}
    if bar is not None:
        bar = bar.strip()
        if len(bar.splitlines()) > 1:
            return {"ok": False, "error": f"bar must be one line: {_BAR_QUESTION}"}

    note_title = title or _derive_title(text)

    if target:
        dest = target if target.endswith(".md") else f"{target}.md"
        if "/" not in dest:
            dest = f"{folder}/{dest}"
    else:
        dest = f"{folder}/{_slugify(note_title)}.md"

    try:
        path = _resolve(dest)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if path.relative_to(BRAIN_ROOT.resolve()).parts[0] != folder:
        return {"ok": False, "error": f"target must be in {folder}/ for layer={layer}. {_LAYER_HELP}"}
    if target and not path.is_file():
        return {"ok": False, "error": "target must name an existing note to append to"}

    if path.exists():
        # Appending rather than colliding is what makes this safe to reach
        # for reflexively: a second capture on the same subject thickens the
        # note instead of erroring out and being abandoned.
        prior = frontmatter.load(path)
        note_title = str(prior.get("title") or path.stem)
        previous_summary = (prior.get("summary_previous", "")
                            if prior.get("summary_needs_review")
                            else prior.get("summary", ""))
        fm = {"summary": summary.strip() if summary is not None else
              f"Needs review after new information: {previous_summary or 'read the full note.'}",
              "summary_needs_review": summary is None, "layer": layer, "kind": note_kind}
        if bar:
            fm["bar"] = bar
        if summary is None:
            fm["summary_previous"] = previous_summary
        stamp = f"_{_today()}_"
        op = {
            "type": "edit_minor",
            "target": dest,
            "edits": [{"operation": "append", "content": f"{stamp}\n\n{text.strip()}"}],
            "reason": "brain_note capture",
            "frontmatter": fm,
        }
        result = _apply_op(op, actor)
        action = "appended"
    else:
        if not bar:
            return {"ok": False, "error": f"bar is required: {_BAR_QUESTION}"}
        duplicates = [] if distinct else _near_duplicates(note_title, text)
        if duplicates:
            return {"ok": False, "near_duplicates": duplicates, "error": (
                "Near-duplicate notes found. Thicken with target=<path>, "
                "or pass distinct=True if this really is separate."
            )}
        fm: dict[str, Any] = {
            "title": note_title,
            "kind": note_kind,
            "layer": layer,
            "bar": bar,
            "summary": summary.strip() if summary is not None else _derive_summary(text),
        }
        if tags:
            fm["tags"] = list(tags)
        op = {
            "type": "create",
            "target": dest,
            "title": note_title,
            "kind": note_kind,
            "frontmatter": fm,
            "body": f"# {note_title}\n\n{text.strip()}\n",
            "reason": "brain_note capture",
        }
        result = _apply_op(op, actor)
        action = "created"

    if not result.get("ok"):
        return result

    # Indexes are how the next session finds this note. Rebuilding here is
    # what makes the capture actually visible without a second tool call.
    try:
        from brain_index import compile_indexes
        compile_indexes()
        indexed = True
    except Exception as exc:  # noqa: BLE001 - a capture must not fail on indexing
        indexed = f"{type(exc).__name__}: {exc}"

    response = {
        "ok": True,
        "action": action,
        "target": dest,
        "title": note_title,
        "actor": actor,
        "version": result.get("new_version") or "1.0.0",
        "indexed": indexed,
        "summary_needs_review": action == "appended" and summary is None,
    }
    if _RULE_DIRECTIVE.search(text):
        response["hint"] = (
            "A standing rule that should be checked every run belongs as a gate in "
            "g-levels (glevels, /home/gabriel/g-levels)."
        )
    return response


@mcp.tool()
def query_references(note: str, hops: int = 1) -> dict:
    """On-demand secondary-graph lookup. Returns URLs cited by this note.

    With hops=2+, also returns URLs cited by notes that wikilink to or from this one
    (requires the .refs-index.sqlite to be populated by `brain-refs scan`).
    """
    if not REFS_DB.exists():
        return {"error": "run `brain-refs scan` first to build the reference index"}
    conn = sqlite3.connect(REFS_DB)
    try:
        cur = conn.execute(
            "SELECT url, canonical_url, status, last_checked FROM refs WHERE note = ?",
            (note,),
        )
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()
    return {"note": note, "hops": hops, "references": rows}


@mcp.tool()
def rebuild_indexes() -> dict:
    """Recompile indexes/master.md and all folder-level topic indexes.

    Reads summary: frontmatter from every note and writes indexes/*.md.
    Call this after execute_commit to keep the index current.
    Fast and deterministic, no LLM required.
    """
    result = subprocess.run(
        [sys.executable, "-m", "brain_index"],
        capture_output=True,
        text=True,
        cwd=g_config.REPO_ROOT,
        env={**os.environ, "BRAIN_ROOT": str(BRAIN_ROOT)},
    )
    if result.returncode != 0:
        return {"ok": False, "error": (result.stderr or result.stdout).strip()}
    return {"ok": True, "output": result.stdout.strip()}


def main() -> None:
    BRAIN_ROOT.mkdir(parents=True, exist_ok=True)
    COMMITS_LOG.touch(exist_ok=True)
    mcp.run()


if __name__ == "__main__":
    main()
