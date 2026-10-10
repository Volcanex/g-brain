# g_spine: the alert spine (g-spine)

The one path from this system to the owner's phone. Anything that must reach a person goes
through `alerts.send()`, which posts to ntfy and dedups, so a stuck failure pages once per
cooldown instead of every run. The phone, the desktop app and the TV subscribe to the ntfy
topic, and no code here knows about them.

## Files
- `alerts.py`: `send(level, source, msg, dedup_key=, click=)`, `clear()`, the `brain_alert`
  MCP tool and a CLI (`python -m brain_alerts --level warn --source me "msg"`). `click` is a URL the phone opens when the
  alert is tapped (ntfy's `Click` header). There is no REST route on purpose: `/api` is
  public, and an open push endpoint would be a spam vector.
- `status.py`: the L5 watcher. Probes every manifest service and every catalog dependency,
  alerts on transitions tiered by criticality, re-pages a vital dependency that stays down,
  and writes `status/snapshot.json` in the vault.
- `unit_alert.py`: the `OnFailure=g-alert@%n.service` hook. One alert per failed unit, with
  the journal tail attached.

## Rules
- Imports only `g_config`. The spine must keep working when the corpus, todos or pulse are
  broken, because reporting that is its job.
- `NTFY_URL` and `GITHUB_TOKEN` resolve through `g_config.env()`: the live environment first,
  then the `.env` chain anchored on the repo root. Never anchor a lookup on this file. A
  chain built from `__file__` would look in `g_spine/`, find no `NTFY_URL`, and the spine
  would go quiet with no error.
- The units still call the old names (`python brain_status.py`, `-m unit_alert %i`), which
  are aliases for these modules. See "Old module names still work" in the root `CLAUDE.md`.
- `BRAIN_ALERTS_DRY_RUN=1` is for a manual test send. Never set it for the test suite. The
  tests patch `dry_run` where they need it.

More on dedup, tiers and the dependency catalog: "Alerting & dependencies (the spine)" in
the root `CLAUDE.md`.
