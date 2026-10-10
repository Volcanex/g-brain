"""Page the alert spine when a systemd unit fails.

Wired as `OnFailure=g-alert@%n.service` on any unit that must not fail silently.
systemd passes the failed unit's name as the template instance, so one generic
unit serves every caller. See `g-alert@.service`.

The journal tail travels with the alert: a notification that only says "it
failed" still costs a session on the box to find out why, which is most of the
delay the spine exists to remove.

Recovery must clear the dedup key (`--clear <unit>`), or the first failure mutes
the next six hours of them, the failure mode brain_alerts' docstring warns
about. Callers on the success path do this; see sync_vault.sh.
"""
from __future__ import annotations

import subprocess
import sys

import brain_alerts

TAIL_LINES = 15
MAX_CHARS = 600


def _dedup_key(unit: str) -> str:
    return f"unit-{unit}"


def _journal_tail(unit: str) -> str:
    try:
        out = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(TAIL_LINES), "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception as e:
        return f"(journal unavailable: {e!r})"
    return out[-MAX_CHARS:] if out else "(no journal output)"


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m unit_alert [--clear] <unit>", file=sys.stderr)
        return 2
    if argv[0] == "--clear":
        if len(argv) < 2:
            print("usage: python -m unit_alert --clear <unit>", file=sys.stderr)
            return 2
        brain_alerts.clear(_dedup_key(argv[1]))
        return 0
    unit = argv[0]
    brain_alerts.send("error", unit, f"unit failed\n\n{_journal_tail(unit)}",
                      dedup_key=_dedup_key(unit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
