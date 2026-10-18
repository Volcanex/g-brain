"""brain_verify: resolve the `stale_dep` flags that rewrites leave behind.

Structural writes (supersede, mark_broken, major bump, wholesale body
replacement) stamp every dependent note with `stale_dep`: "the ground under
this note moved, nobody has checked whether it still holds". This is the pass
that resolves those flags, so habitual writing doesn't quietly accumulate
notes standing on ground that shifted.

**It clears only on explicit dependency-specific review evidence.** A review
must name the exact stamp being reviewed, record a reason and an attended
actor. A note's updated date is not evidence of review. Every other flag stays
in the review queue, with aged flags surfaced to readers.

That restraint is the point. A verifier that decided for itself whether a note
"still holds" would be rubber-stamping: it would clear everything, look
healthy, and the corpus would rot behind a green light. So the deliberate
design is: mechanical clears here, judgement stays with a human or with an
explicitly-invoked agent reading /api/stale.

Self-monitoring: every run reports its own numbers, and a run that clears
*everything* is itself treated as suspicious: that is the shape a rubber
stamp would take.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from threading import Lock

import frontmatter

import g_config

BRAIN_ROOT = g_config.BRAIN_ROOT
REPORT_PATH = BRAIN_ROOT / "status" / "stale.json"

# A flag this old is highlighted in the report and session orientation.
ESCALATE_AFTER_DAYS = 14
_REPORT_LOCK = Lock()


def _today() -> dt.date:
    return dt.date.today()


def _parse_date(value) -> dt.date | None:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip())
    except (ValueError, AttributeError):
        return None


def _iter_flagged(root: Path):
    """Yield (path, post, flags) for every note carrying stale_dep."""
    for md in sorted(root.rglob("*.md")):
        rel = md.relative_to(root)
        if any(p.startswith(".") for p in rel.parts):
            continue
        try:
            post = frontmatter.load(md)
        except Exception:  # noqa: BLE001 - a malformed note must not stop the sweep
            continue
        flags = post.get("stale_dep")
        if isinstance(flags, list) and flags:
            yield md, post, flags


def resolve_note(note: str, metadata: dict, today: dt.date) -> dict:
    """Pure review check for one note; return retained flags and report entries."""
    flags = metadata.get("stale_dep") or []
    reviews = metadata.get("dependency_reviews") or []
    if not isinstance(reviews, list):
        reviews = []
    keep = []
    cleared = []
    outstanding = []
    errors = []
    for flag in flags:
        if not isinstance(flag, dict):
            keep.append(flag)
            errors.append(f"{note}: malformed dependency flag retained")
            continue
        dep = str(flag.get("note", ""))
        since = _parse_date(flag.get("since"))
        entry = {
            "note": note,
            "title": str(metadata.get("title") or note),
            "depends_on": dep,
            "since": since.isoformat() if since else None,
            "reason": str(flag.get("reason", "")),
            "stamp": str(flag.get("stamp") or flag.get("since") or ""),
        }
        review = next((r for r in reviews if isinstance(r, dict)
                       and r.get("note") == dep
                       and entry["stamp"] and r.get("stamp") == entry["stamp"]
                       and r.get("actor") in {"human", "agent"}
                       and len(str(r.get("reason", "")).strip().split()) >= 4
                       and r.get("reviewed_at")), None)
        if review is not None:
            entry["cleared_because"] = review["reason"]
            cleared.append(entry)
            continue
        age = (today - since).days if since else 0
        entry["age_days"] = age
        entry["escalate"] = age >= ESCALATE_AFTER_DAYS
        outstanding.append(entry)
        keep.append(flag)
    return {"keep": keep, "cleared": cleared, "outstanding": outstanding, "errors": errors}


def sweep(apply: bool = True, root: Path | None = None) -> dict:
    """Walk every flagged note. Clear what evidence allows, report the rest."""
    root = BRAIN_ROOT if root is None else root
    cleared: list[dict] = []
    outstanding: list[dict] = []
    errors: list[str] = []

    for md, post, flags in _iter_flagged(root):
        rel = md.relative_to(root).as_posix().removesuffix(".md")
        result = resolve_note(rel, post.metadata, _today())
        if apply and result["cleared"]:
            try:
                if result["keep"]:
                    post["stale_dep"] = result["keep"]
                else:
                    post.metadata.pop("stale_dep", None)
                md.write_text(frontmatter.dumps(post) + "\n")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{rel}: {type(exc).__name__}: {exc}")
                # A failed write must not advertise flags as cleared.
                result = resolve_note(rel, {**post.metadata, "stale_dep": flags,
                                           "dependency_reviews": []}, _today())
        cleared.extend(result["cleared"])
        outstanding.extend(result["outstanding"])
        errors.extend(result["errors"])

    total = len(cleared) + len(outstanding)
    report = {
        "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
        "flags_seen": total,
        "cleared": len(cleared),
        "outstanding": len(outstanding),
        "escalating": sum(1 for o in outstanding if o.get("escalate")),
        "errors": errors,
        "cleared_detail": cleared,
        "outstanding_detail": outstanding,
        "oldest_detail": sorted((o for o in outstanding if o["escalate"]),
                                key=lambda o: (-o["age_days"], o["note"], o["depends_on"]))[:5],
    }

    # The rubber-stamp alarm. A verifier whose job is to clear things will,
    # if it ever goes wrong, go wrong by clearing everything and looking
    # healthy. Clearing a whole non-trivial batch is therefore reported as a
    # suspicious outcome rather than a good one.
    report["suspicious_full_clear"] = total >= 5 and len(outstanding) == 0 and not errors

    return report


def _notify(report: dict) -> None:
    """Warn on verifier failures or a suspicious batch; age stays in the report."""
    try:
        from brain_alerts import send
    except Exception:  # noqa: BLE001 - the sweep still succeeded
        return

    if report["errors"]:
        send("error", "brain-verify",
             f"stale-dep sweep hit {len(report['errors'])} error(s): {report['errors'][0]}",
             dedup_key="brain-verify-errors")

    if report["suspicious_full_clear"]:
        send("warn", "brain-verify",
             f"stale-dep sweep cleared all {report['flags_seen']} flags in one run: "
             "verify this is real and not a broken check",
             dedup_key="brain-verify-full-clear")


def read_report(root: Path | None = None) -> dict:
    """Resolve reviewed flags on demand, serializing concurrent report readers."""
    with _REPORT_LOCK:
        report = sweep(root=root)
        _notify(report)
        return report


def main() -> int:
    dry = "--dry-run" in sys.argv
    report = sweep(apply=False) if dry else read_report()

    if not dry:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n")

    print(json.dumps({k: v for k, v in report.items()
                      if not k.endswith("_detail")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
