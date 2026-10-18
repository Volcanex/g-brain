"""brain_condense: find what the corpus needs condensed. Never condense it.

Habitual capture (`brain_note`) fixes the empty-brain problem and creates the
opposite one: accretion. Near-duplicate notes on the same subject, notes that
have grown a stack of dated appends instead of prose, orphans nothing links to,
frontmatter drift. Left alone, the corpus stops being readable, and an
unreadable corpus stops being read, which is how it stops being written to.
The brain flagged this itself in ideas/idea-capture-reflex: "the pulse and
clusters have accumulated garble."

**This module only looks.** Merging two notes is `edit_major`; rewriting an
accreted note into prose is a judgement about what to keep and what to drop.
Neither is something to do at 4am with nobody watching, and the commit fence
agrees: `ai-auto` cannot perform destructive ops at all.

So condensing splits in two, and this is the half that can run alone:

  - **Detection** (here, on report reads): mechanical signals, no model, no writes.
    Cached by corpus signature; manual CLI runs also produce status/condense.json.
  - **Condensation** (the `brain-condense` skill): an agent with the owner in the
    session reads this report, proposes specific merges, and executes them.
    Under the current gate most merges are its own call; only the load-bearing
    notes (>= 8 dependents, or a canonical design) stop for a yes.

The split is the point. A detector that never writes cannot damage anything,
so it can run on demand and be trusted; the writing happens where someone can be
asked.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from functools import lru_cache
from itertools import combinations
from pathlib import Path
from threading import Lock

import frontmatter

import g_config

BRAIN_ROOT = g_config.BRAIN_ROOT
REPORT_PATH = BRAIN_ROOT / "status" / "condense.json"
CORPUS_LAYERS = ("designs", "ideas", "about", "hazards", "findings", "references")
_REPORT_LOCK = Lock()

# Tuned against the real corpus (84 notes, 2026-08-17), where whole-body
# Jaccard runs median 0.041 / p95 0.098 / max 0.276: long prose shares few
# tokens even on one subject, so anything above ~0.20 is a strong signal.
#
# Note what this measures: shared *territory*, not duplication. The top pairs
# here (period-app-pcos-model vs period-app-pmdd-model) are legitimately
# separate notes. That's the honest framing: this surfaces "these two cover
# the same ground, should one absorb the other?" and a human or an agent in a
# session answers it. A detector that declared two notes duplicates and acted
# on it would be making exactly the judgement it is least equipped to make.
OVERLAP_THRESHOLD = 0.20
ACCRETION_STAMPS = 3       # dated appends before a note wants rewriting as prose

CANONICAL_KINDS = {"design", "finding", "idea", "reference", "source", "log", "about", "fact", "hazard"}

# Words that carry no subject signal here: mostly g-system furniture that
# appears in half the corpus and would make everything look similar, plus the
# owner's name (VOICE_OWNER_NAME), which half the notes mention.
_STOP = {
    "this", "that", "with", "from", "have", "into", "when", "what", "which",
    "they", "them", "then", "than", "there", "their", "would", "could",
    "should", "about", "after", "before", "because", "still", "just", "only",
    "also", "does", "done", "make", "made", "much", "more", "most", "some",
    "such", "over", "under", "each", "been", "being", "were", "will", "your",
    "brain", "note", "notes", "system", "gcore", "https", "http",
} | set(g_config.env("VOICE_OWNER_NAME").lower().split())
_WORD = re.compile(r"[a-z][a-z0-9_-]{3,}")
_STAMP = re.compile(r"^_\d{4}-\d{2}-\d{2}_$", re.MULTILINE)


def _today() -> dt.date:
    return dt.date.today()


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def is_corpus_note(path: str) -> bool:
    parts = Path(path).parts
    return (len(parts) > 1 and parts[0] in CORPUS_LAYERS
            and not any(p.startswith(".") or p in ("versions", "archive") for p in parts)
            and parts[-1] not in ("CLAUDE", "AGENTS", "README", "CLAUDE.md", "AGENTS.md", "README.md"))


def _note_paths(root: Path):
    for layer in CORPUS_LAYERS:
        for directory, folders, files in os.walk(root / layer):
            folders[:] = sorted(f for f in folders
                                if not f.startswith(".") and f not in ("versions", "archive"))
            for name in sorted(files):
                md = Path(directory) / name
                if name.endswith(".md") and is_corpus_note(md.relative_to(root).as_posix()):
                    yield md


def _load_notes(root: Path | None = None) -> list[dict]:
    """Every authored note, with the fields the signals need."""
    root = BRAIN_ROOT if root is None else root
    out = []
    for md in _note_paths(root):
        rel = md.relative_to(root)
        try:
            post = frontmatter.load(md)
        except Exception:  # noqa: BLE001
            continue
        body = post.content or ""
        out.append({
            "path": rel.as_posix().removesuffix(".md"),
            "title": str(post.get("title") or md.stem),
            "kind": str(post.get("kind") or "unknown"),
            "status": str(post.get("status") or "active"),
            "summary": str(post.get("summary") or "").strip(),
            "tags": [str(t) for t in (post.get("tags") or [])],
            "updated": str(post.get("updated") or ""),
            "body": body,
            "chars": len(body),
            "stamps": len(_STAMP.findall(body)),
            "tokens": _tokens(f"{post.get('title', '')} {post.get('summary', '')} {body}"),
        })
    return out


def find_overlaps(notes: list[dict]) -> list[dict]:
    """Pairs covering the same territory. Candidates, not verdicts. Deciding
    two notes are really one note is a judgement, made later, with a human."""
    pairs = []
    for a, b in combinations(notes, 2):
        if a["kind"] != b["kind"]:
            continue
        score = _jaccard(a["tokens"], b["tokens"])
        if score < OVERLAP_THRESHOLD:
            continue
        pairs.append({
            "a": a["path"], "a_title": a["title"],
            "b": b["path"], "b_title": b["title"],
            "kind": a["kind"],
            "overlap": round(score, 3),
            "shared_tags": sorted(set(a["tags"]) & set(b["tags"])),
        })
    return sorted(pairs, key=lambda p: -p["overlap"])


def find_accreted(notes: list[dict]) -> list[dict]:
    """Notes that have become a stack of dated appends rather than prose.

    This is the specific debt `brain_note`'s append path creates: every capture
    on an existing subject thickens the note, which is right for capture and
    wrong for reading. Past a few stamps the note wants rewriting."""
    out = []
    for n in notes:
        if n["stamps"] < ACCRETION_STAMPS:
            continue
        out.append({
            "path": n["path"], "title": n["title"],
            "appends": n["stamps"], "chars": n["chars"],
        })
    return sorted(out, key=lambda o: -o["appends"])


def find_orphans(notes: list[dict]) -> list[dict]:
    """Notes with no link in and no link out, unreachable by navigation."""
    try:
        from brain_reader import Note, WIKILINK_RE, link_edges
        edges = link_edges([
            Note(path=n["path"], title=n["title"], kind=n["kind"], status=n["status"],
                 version="", lineage=[], tags=n["tags"], updated=n["updated"], created="",
                 wikilinks=sorted({m.group(1).strip() for m in WIKILINK_RE.finditer(n["body"])}))
            for n in notes
        ])
    except Exception:  # noqa: BLE001
        return []
    linked = {s for s, _ in edges} | {t for _, t in edges}
    return [
        {"path": n["path"], "title": n["title"], "kind": n["kind"]}
        for n in notes
        if n["path"] not in linked and n["status"] == "active"
    ]


MANIFEST = g_config.MANIFEST_PATH
AGING_DAYS = 270

# Things that were retired but still get described in the present tense by old
# notes. Not derivable from the projects list, which records what exists: these
# are exactly what stopped existing. They are the instance's own history, so the
# instance lists them in its manifest as `retired_terms`, each entry a `term` and
# the `reason` it is stale. With no such list, no term is checked.

_PATH_RE = re.compile("`(" + re.escape(str(Path.home())) + r"/[^`\s]+)`")


def find_stale(notes: list[dict]) -> list[dict]:
    """Notes that describe a world that has moved on.

    This is the signal that actually matters and the one structural checks
    miss entirely: a note can be perfectly well-formed, well-linked and
    non-duplicated while being simply *wrong*. Three mechanical tells, all
    checked against things that are their own SSOT:

      - it talks about a project the manifest now marks fossil/deprecated
      - it cites a path under the home directory that is no longer on disk
      - it names something known to be retired

    Age alone is reported but never used as proof: a design from March can be
    perfectly true. Age plus one of the above is what makes a strong case.

    Nothing here is a verdict. The fix is `mark_broken` (deliberately
    unfenced: flagging a note as no-longer-true destroys nothing, and an
    unflagged wrong note is the costlier failure) or a rewrite in a session.
    """
    retired_projects: dict[str, str] = {}
    retired_terms: dict[str, str] = {}
    try:
        manifest = json.loads(MANIFEST.read_text())
        for p in manifest.get("projects", []):
            status = p.get("status", "active")
            if status != "active" and p.get("id"):
                retired_projects[p["id"]] = status
        for t in manifest.get("retired_terms", []):
            if t.get("term"):
                retired_terms[t["term"]] = t.get("reason", "retired")
    except (OSError, json.JSONDecodeError):
        pass

    out = []
    for n in notes:
        if n["status"] in ("broken", "superseded"):
            continue  # already flagged; not news

        reasons = []
        hay = n["body"].lower()

        for proj, status in retired_projects.items():
            # Require a *technical* mention, not just the word. Several project
            # ids are ordinary English ("period"), and a plain word-boundary
            # match flagged notes for the phrase "a period of time": noise
            # that would have made the whole signal unusable.
            p = re.escape(proj)
            technical = rf"`{p}[`/]|/{p}\b|\b{p}[-/](?:api|app|server|web|src)\b|\b{p}\.[a-z]|\b{p}/"
            if re.search(technical, hay):
                reasons.append(f"describes `{proj}`, which the manifest now marks {status}")

        for term, why in retired_terms.items():
            if term.lower() in hay:
                reasons.append(f"mentions `{term}` ({why})")

        phantom = [p for p in set(_PATH_RE.findall(n["body"])) if not Path(p).exists()]
        if phantom:
            reasons.append(f"cites {len(phantom)} path(s) not on disk: {', '.join(sorted(phantom)[:3])}")

        if not reasons:
            continue

        age = None
        try:
            age = (_today() - dt.date.fromisoformat(n["updated"])).days
        except (ValueError, TypeError):
            pass

        out.append({
            "path": n["path"], "title": n["title"], "kind": n["kind"],
            "age_days": age, "reasons": reasons,
            # Old *and* contradicted is the strong case; recent and contradicted
            # is more likely a deliberate mention of history.
            "confidence": "high" if (age or 0) > AGING_DAYS or len(reasons) > 1 else "check",
        })
    return sorted(out, key=lambda o: (o["confidence"] != "high", -(o["age_days"] or 0)))


def find_drift(notes: list[dict]) -> list[dict]:
    """Frontmatter that has drifted from what the indexes rely on.

    Deliberately narrow. Flagging "no tags" caught 47 of 84 notes (tags are
    barely used in this corpus) and would have buried the three real problems
    under a signal nobody would ever act on. A check that fires on half the
    corpus is a check that gets ignored.

    These are the cheap ones: fixing them is a frontmatter `edit_minor`, which
    destroys nothing and needs nobody's permission. An agent finding them here
    should just fix them."""
    out = []
    for n in notes:
        problems = []
        if n["kind"] not in CANONICAL_KINDS:
            problems.append(f"kind {n['kind']!r} is not canonical")
        if not n["summary"]:
            problems.append("no summary (the indexes render it)")
        if problems:
            out.append({"path": n["path"], "title": n["title"], "problems": problems})
    return out


def scan(root: Path | None = None) -> dict:
    notes = _load_notes(root)
    overlaps = find_overlaps(notes)
    stale = find_stale(notes)
    accreted = find_accreted(notes)
    orphans = find_orphans(notes)
    drift = find_drift(notes)

    backlog = len(stale) + len(overlaps) + len(accreted) + len(orphans)
    return {
        "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
        "notes_scanned": len(notes),
        "backlog": backlog,
        "counts": {
            "stale": len(stale),
            "overlaps": len(overlaps),
            "accreted": len(accreted),
            "orphans": len(orphans),
            "drift": len(drift),
        },
        "stale": stale,
        "overlaps": overlaps,
        "accreted": accreted,
        "orphans": orphans,
        "drift": drift,
    }


def _file_signature(path: Path) -> tuple:
    try:
        stat = path.stat()
        return (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return (str(path), None, None)


@lru_cache(maxsize=1)
def _cached_scan(root: Path, signature: tuple) -> dict:
    return scan(root)


def read_report(root: Path | None = None) -> dict:
    """Return the detector report, reusing it until kept notes or the manifest change."""
    root = BRAIN_ROOT if root is None else root
    with _REPORT_LOCK:
        signature = (_today(), _file_signature(MANIFEST),
                     tuple(_file_signature(md) for md in _note_paths(root)))
        return _cached_scan(root, signature)


def main() -> int:
    report = scan()
    if "--dry-run" not in sys.argv:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n")

    print(json.dumps({
        "checked_at": report["checked_at"],
        "notes_scanned": report["notes_scanned"],
        "backlog": report["backlog"],
        "counts": report["counts"],
    }, indent=2))

    if "-v" in sys.argv:
        for s_ in report["stale"][:15]:
            print(f"  STALE [{s_['confidence']:5}] {s_['path']}: {s_['reasons'][0]}")
        for p in report["overlaps"][:15]:
            print(f"  ovl {p['overlap']:.2f}  {p['a']}  <->  {p['b']}")
        for a in report["accreted"][:10]:
            print(f"  acc {a['appends']} appends  {a['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
