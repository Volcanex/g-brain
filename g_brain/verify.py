"""brain_verify: resolve the `stale_dep` flags that rewrites leave behind.

Structural writes (supersede, mark_broken, major bump, wholesale body
replacement) stamp every dependent note with `stale_dep`: "the ground under
this note moved, nobody has checked whether it still holds". This is the pass
that resolves those flags, so habitual writing doesn't quietly accumulate
notes standing on ground that shifted.

**It clears only on explicit dependency-specific review evidence.** A review
must name the exact stamp being reviewed, record a reason and an attended
actor. A note's updated date is not evidence of review. Every other flag stays
in the review queue and, past a threshold, reaches the alert spine.

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

import frontmatter

import g_config

BRAIN_ROOT = g_config.BRAIN_ROOT
REPORT_PATH = BRAIN_ROOT / "status" / "stale.json"

# A flag older than this has been sitting unreviewed long enough to page.
ESCALATE_AFTER_DAYS = 14


def _today() -> dt.date:
    return dt.date.today()


def _parse_date(value) -> dt.date | None:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value).strip())
    except (ValueError, AttributeError):
        return None


def _iter_flagged():
    """Yield (path, post, flags) for every note carrying stale_dep."""
    for md in sorted(BRAIN_ROOT.rglob("*.md")):
        rel = md.relative_to(BRAIN_ROOT)
        if any(p.startswith(".") for p in rel.parts):
            continue
        try:
            post = frontmatter.load(md)
        except Exception:  # noqa: BLE001 - a malformed note must not stop the sweep
            continue
        flags = post.get("stale_dep")
        if isinstance(flags, list) and flags:
            yield md, post, flags


def sweep(apply: bool = True) -> dict:
    """Walk every flagged note. Clear what evidence allows, report the rest."""
    cleared: list[dict] = []
    outstanding: list[dict] = []
    errors: list[str] = []

    for md, post, flags in _iter_flagged():
        rel = md.relative_to(BRAIN_ROOT).as_posix().removesuffix(".md")
        reviews = post.get("dependency_reviews") or []
        if not isinstance(reviews, list):
            reviews = []
        keep: list[dict] = []

        for flag in flags:
            if not isinstance(flag, dict):
                keep.append(flag)
                errors.append(f"{rel}: malformed dependency flag retained")
                continue
            dep = str(flag.get("note", ""))
            since = _parse_date(flag.get("since"))
            entry = {
                "note": rel,
                "title": str(post.get("title") or rel),
                "depends_on": dep,
                "since": since.isoformat() if since else None,
                "reason": str(flag.get("reason", "")),
                "stamp": str(flag.get("stamp") or flag.get("since") or ""),
            }

            # Exact stamp identity, not an unrelated edit or an earlier review.
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

            age = (_today() - since).days if since else 0
            entry["age_days"] = age
            entry["escalate"] = age >= ESCALATE_AFTER_DAYS
            outstanding.append(entry)
            keep.append(flag)

        if apply and len(keep) != len(flags):
            try:
                if keep:
                    post["stale_dep"] = keep
                else:
                    post.metadata.pop("stale_dep", None)
                md.write_text(frontmatter.dumps(post) + "\n")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{rel}: {type(exc).__name__}: {exc}")

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
    }

    # The rubber-stamp alarm. A verifier whose job is to clear things will,
    # if it ever goes wrong, go wrong by clearing everything and looking
    # healthy. Clearing a whole non-trivial batch is therefore reported as a
    # suspicious outcome rather than a good one.
    report["suspicious_full_clear"] = total >= 5 and len(outstanding) == 0

    return report


def _notify(report: dict) -> None:
    """Page the spine for anything that needs a human. Never for a quiet run."""
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

    n = report["escalating"]
    if n:
        oldest = max(
            (o for o in report["outstanding_detail"] if o.get("escalate")),
            key=lambda o: o.get("age_days", 0),
        )
        review = g_config.env("BRAIN_REVIEW_URL")
        send("warn", "brain-verify",
             f"{n} brain note(s) have stood on moved ground for "
             f"{ESCALATE_AFTER_DAYS}+ days, oldest: {oldest['note']} "
             f"(depends on {oldest['depends_on']}, {oldest['age_days']}d)."
             + (f" Review at {review}" if review else ""),
             dedup_key="brain-verify-stale")


def main() -> int:
    dry = "--dry-run" in sys.argv
    report = sweep(apply=not dry)

    if not dry:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n")
        _notify(report)

    print(json.dumps({k: v for k, v in report.items()
                      if not k.endswith("_detail")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
