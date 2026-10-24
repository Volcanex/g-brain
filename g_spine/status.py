"""brain_status.py: layer 5, live service health.

Probes every service in the instance manifest.json using the health-check
resolution order from conventions.md and writes a snapshot to
brain/status/snapshot.json. Run on a timer; /api/status serves the latest.

The manifest stays the SSOT for *what should exist*; this records *what is alive*.
"""
import json
import hashlib
import shutil
import sqlite3
import time
import os
import socket
import subprocess
import sys
import base64
import tomllib
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

import brain_alerts
import g_config
from g_spine import reflexes

GITHUB_TOKEN = g_config.env("GITHUB_TOKEN") or None

BRAIN_ROOT = g_config.BRAIN_ROOT
BRAIN_HOST = g_config.BRAIN_HOST
GCORE_REPO = g_config.MANIFEST_REPO
API = "https://api.github.com"


def _client():
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN not set")
    return httpx.Client(
        headers={
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
        follow_redirects=True,
    )


def _fetch_file(client, repo, path, ref="HEAD"):
    r = client.get(f"{API}/repos/{repo}/contents/{path}", params={"ref": ref})
    r.raise_for_status()
    return base64.b64decode(r.json()["content"]).decode("utf-8", errors="replace")


def _check_http(url, timeout=8):
    try:
        r = httpx.get(url, timeout=timeout, follow_redirects=True)
        ok = 200 <= r.status_code < 300
        return ("up" if ok else "down", f"HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        return ("down", type(e).__name__)


def _check_http_from(host_user, host_ip, url, timeout=8):
    """For a service on a private network that this host is not on."""
    # The probe key on the far side is a forced command that allows only this verb.
    cmd = ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", f"{host_user}@{host_ip}",
           "http-status", url]
    try:
        code = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 12).stdout.strip()
    except Exception as e:  # noqa: BLE001
        return ("unknown", f"remote probe failed: {type(e).__name__}")
    if code.startswith("2"):
        return ("up", f"HTTP {code}")
    return ("down", f"HTTP {code or 'no response'}")


def _check_tcp(host, port, timeout=5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return ("up", f"TCP {port} open")
    except Exception as e:  # noqa: BLE001
        return ("down", f"TCP {port}: {type(e).__name__}")


def _local(host_id):
    return not BRAIN_HOST or host_id == BRAIN_HOST


def _check_unit(host_id, host_user, host_ip, unit):
    if _local(host_id):
        cmd = ["systemctl", "is-active", unit]
    else:
        cmd = [
            "ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
            f"{host_user}@{host_ip}", "systemctl", "is-active", unit,
        ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        state = out.stdout.strip()
        return ("up" if state == "active" else "down", f"systemctl: {state or 'no-response'}")
    except Exception as e:  # noqa: BLE001
        return ("unknown", f"unit probe failed: {type(e).__name__}")


def _dig(data, path):
    """Walk a dotted path into nested dict/list JSON, e.g. 'data.balance'."""
    cur = data
    for part in path.split("."):
        cur = cur[int(part)] if isinstance(cur, list) else cur[part]
    return cur


def _check_dependency(dep):
    """Probe one external dependency. Returns (state, detail).

    state is up | warn | down | unknown. The key failure mode this catches is
    'unpaid -> suspended': an authenticated call returns 401/403 -> down. Balance
    probes also warn BEFORE a provider hits zero. Creds resolve via brain_alerts
    _env (live env, then the known .env homes), so no secret moves into the instance.
    """
    p = dep.get("probe", {})
    ptype = p.get("type")
    if not ptype:
        # Recorded in the dependency map but not probed, e.g. its credentials live
        # in another repo's env, not reachable centrally. Honest 'declared' state.
        return ("unknown", "declared, no probe configured")
    try:
        if ptype == "ntfy":
            base = brain_alerts.env(p.get("url_env", "NTFY_URL"))
            if not base:
                return ("unknown", f"{p.get('url_env', 'NTFY_URL')} unset")
            from urllib.parse import urlsplit
            u = urlsplit(base)
            r = httpx.get(f"{u.scheme}://{u.netloc}/v1/health", timeout=8)
            if r.status_code == 200 and r.json().get("healthy"):
                return ("up", "healthy")
            return ("down", f"HTTP {r.status_code}")

        if ptype in ("http", "balance"):
            headers = {}
            url = p["url"]
            if p.get("auth_env"):
                tok = brain_alerts.env(p["auth_env"])
                if not tok:
                    return ("unknown", f"{p['auth_env']} unset")
                headers["Authorization"] = f"{p.get('auth_scheme', 'Bearer')} {tok}"
            if p.get("key_env"):
                # Providers that take the key as a query param (e.g. SerpApi)
                # rather than an Authorization header.
                tok = brain_alerts.env(p["key_env"])
                if not tok:
                    return ("unknown", f"{p['key_env']} unset")
                url += ("&" if "?" in url else "?") + f"{p.get('key_param', 'api_key')}={tok}"
            r = httpx.get(url, headers=headers, timeout=8)
            if r.status_code in (401, 403):
                return ("down", f"auth failed (HTTP {r.status_code}), unpaid or suspended?")
            if not (200 <= r.status_code < 300):
                return ("down", f"HTTP {r.status_code}")
            if ptype == "http":
                return ("up", f"HTTP {r.status_code}")
            val = float(_dig(r.json(), p["value_path"]))
            if p.get("minus_path"):
                val -= float(_dig(r.json(), p["minus_path"]))
            mn = float(p.get("min", 0))
            if val <= 0:
                return ("down", f"balance {val:.2f} exhausted")
            if val < mn:
                return ("warn", f"balance {val:.2f} below {mn:.2f}")
            return ("up", f"balance {val:.2f}")

        if ptype == "expires":
            days = (date.fromisoformat(p["date"]) - date.today()).days
            warn = int(p.get("warn_days", 14))
            if days < 0:
                return ("down", f"expired {-days}d ago ({p['date']})")
            if days <= warn:
                return ("warn", f"expires in {days}d ({p['date']})")
            return ("up", f"valid, expires {p['date']} ({days}d)")
    except Exception as e:  # noqa: BLE001
        return ("unknown", f"probe error: {type(e).__name__}")
    return ("unknown", f"unknown probe type: {ptype}")


def _dependency_repo_map():
    """dep id -> [repo ids that declare it], read from the locally-synced lineage
    tomls (brain_gmap already stores each repo's full pyproject there). This is the
    'openrouter down -> these repos are affected' reverse index, with no extra crawl."""
    out = {}
    ldir = BRAIN_ROOT / "lineage"
    if not ldir.exists():
        return out
    for f in ldir.glob("*.toml"):
        try:
            data = tomllib.loads(f.read_text())
        except Exception:  # noqa: BLE001
            continue
        for d in (data.get("tool", {}).get("g-core") or data.get("tool", {}).get("gcore", {})).get("depends", []):
            out.setdefault(d, []).append(f.stem)
    return out


def _build_health_cache(deps, services):
    """Fetch /health once per service that a dependency maps to. The service owns
    the check (its own creds, its own service); brain just forwards. Returns
    {service_id: checks_dict | None}. None on unreachable -> caller falls back."""
    by_id = {s.get("id"): s for s in services}
    cache = {}
    for dep in deps:
        h = dep.get("health")
        if not h or h.get("service") in cache:
            continue
        sid = h.get("service")
        svc = by_id.get(sid)
        url = svc.get("health_url") if svc else None
        if not url:
            cache[sid] = None
            continue
        try:
            data = httpx.get(url, timeout=8).json()
            cache[sid] = data.get("checks") if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001
            cache[sid] = None
    return cache


def _check_via_health(dep, health_cache):
    """If the dep maps to a reachable service /health check, return its
    (state, detail), the creds-local source of truth. Else None, and the caller
    falls back to the central probe (resilience when the service is down)."""
    h = dep.get("health")
    if not h:
        return None
    checks = health_cache.get(h.get("service"))
    if not isinstance(checks, dict):
        return None
    c = checks.get(h.get("check"))
    if not isinstance(c, dict) or "ok" not in c:
        return None
    detail = str(c.get("detail") or ("ok" if c["ok"] else "not ok"))
    return ("up" if c["ok"] else "down", f"{detail} (via {h['service']}/health)")


def probe_dependency(dep, repo_map, health_cache):
    via = _check_via_health(dep, health_cache)
    state, detail = via if via is not None else _check_dependency(dep)
    return {
        "id": dep.get("id"),
        "kind": "dependency",
        "provider": dep.get("provider"),
        "powers": dep.get("powers"),
        "criticality": dep.get("criticality", "important"),
        "state": state,
        "detail": detail,
        "affected_repos": sorted(repo_map.get(dep.get("id"), [])),
    }


def _prior_states(snap_path):
    """Map id -> state from the previous snapshot, or None on the first run (so a
    deploy never alerts on something that was already down, only real transitions)."""
    try:
        prev = json.loads(snap_path.read_text())
    except Exception:  # noqa: BLE001
        return None
    states = {}
    for r in prev.get("services", []) + prev.get("dependencies", []):
        # A deferred outage still needs its transition page if remedies run out.
        states[r["id"]] = "unknown" if r.get("reflex_pending") else r["state"]
    return states


REPAGE_HOURS = 24  # a vital dependency that stays down re-pages this often


def _now():
    return datetime.now(timezone.utc)


def _fmt_duration(delta):
    secs = int(delta.total_seconds())
    days, rem = divmod(secs, 86400)
    hours = rem // 3600
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h" if hours else f"{rem // 60}m"


def _parse_ts(value):
    try:
        ts = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _track_vital_down(r, key, level, now):
    """Keep the down-since clock and re-page a vital dependency that stays down.

    State lives in the spine's alerts.json under `<key>:down_since` and
    `<key>:last_page`, so a restart neither re-pages early nor loses the clock.
    A vital dep already down when this first runs starts its clock now (the
    dedup stamp can be from an old episode, so it is not trusted as down-since)."""
    since_k, page_k = f"{key}:down_since", f"{key}:last_page"
    state = brain_alerts.load_state()
    since = _parse_ts(state.get(since_k))
    last = _parse_ts(state.get(page_k))
    if since is None:
        since = last = now
        if not brain_alerts.dry_run():
            state[since_k], state[page_k] = since.isoformat(), last.isoformat()
            brain_alerts.save_state(state)
    if last is None:
        last = since
    if now - last < timedelta(hours=REPAGE_HOURS):
        return
    body = (f"{r['id']} STILL DOWN for {_fmt_duration(now - since)}: {r['detail']}"
            f" (powers {r.get('powers') or 'unknown'})")
    result = brain_alerts.send(level, r["id"], body)
    if result.get("ok") and not result.get("dry_run"):
        state = brain_alerts.load_state()
        state[page_k] = now.isoformat()
        brain_alerts.save_state(state)


def _clear_vital_clock(key):
    state = brain_alerts.load_state()
    gone = [k for k in (f"{key}:down_since", f"{key}:last_page") if k in state]
    for k in gone:
        del state[k]
    if gone and not brain_alerts.dry_run():
        brain_alerts.save_state(state)


def _alert_transitions(prior, results):
    """Page the spine when something crosses into trouble or recovers. Dedup keeps
    a stuck failure to one ping per cooldown; recovery pings only if we'd alerted.
    A vital dependency that stays down re-pages every REPAGE_HOURS."""
    if prior is None:
        return
    now_dt = _now()
    for r in results:
        rid, now, was = r["id"], r["state"], prior.get(r["id"])
        key = f"status:{rid}"
        if r.get("healed"):
            if not brain_alerts.dry_run():
                brain_alerts.clear(key)
            continue
        if r.get("reflex_pending"):
            continue
        bad = ("down", "warn")
        vital = r.get("kind") == "dependency" and r.get("criticality") == "vital"
        if now in bad and was not in bad:
            # Tier by criticality: a 'nice' dependency degrading shouldn't scream;
            # vital/important down is loud. Services carry no criticality -> loud.
            if r.get("criticality") == "nice":
                level = "warn"
            else:
                level = "error" if now == "down" else "warn"
            ctx = r.get("powers") or r.get("repo") or ""
            body = f"{rid} {now}: {r['detail']}" + (f" (powers {ctx})" if ctx else "")
            sent = brain_alerts.send(level, rid, body, dedup_key=key)
            if vital and now == "down" and sent.get("ok") and not brain_alerts.dry_run():
                state = brain_alerts.load_state()
                state[f"{key}:down_since"] = state[f"{key}:last_page"] = now_dt.isoformat()
                brain_alerts.save_state(state)
        elif vital and now == "down" and was == "down":
            _track_vital_down(r, key, "error", now_dt)
        elif now == "up" and was in bad:
            had_alerted = key in brain_alerts.load_state()
            was_down_for = None
            if vital:
                since = _parse_ts(brain_alerts.load_state().get(f"{key}:down_since"))
                was_down_for = _fmt_duration(now_dt - since) if since else None
            if not brain_alerts.dry_run():
                brain_alerts.clear(key)
            if had_alerted:
                tail = f" after {was_down_for} down" if was_down_for else ""
                brain_alerts.send("info", rid, f"{rid} recovered{tail}: {r['detail']}")
        if vital and now != "down":
            _clear_vital_clock(key)


def _check_search_freshness():
    """Read one local canary, never scan the corpus or mutate the search DB.

    Basic Memory 0.20.3 and 0.23.2 store SHA-256 in entity.checksum. This checks source
    ingestion only, not ranking or embedding quality. Watch timestamps can be
    old on a quiet corpus and are deliberately not treated as freshness proof.
    """
    db = g_config.BRAIN_MEMORY_DB
    watcher = Path(os.environ.get("BRAIN_MEMORY_WATCH_STATUS", str(db.parent / "watch-status.json")))
    canary = g_config.env("BRAIN_MEMORY_CANARY")
    if not canary:
        return "unknown", "search freshness: no canary configured (BRAIN_MEMORY_CANARY)"
    try:
        grace = max(0, float(os.environ.get("BRAIN_MEMORY_FRESHNESS_GRACE_SECONDS", "120")))
        source = (BRAIN_ROOT / canary).resolve()
        if Path(canary).is_absolute() or not source.is_relative_to(BRAIN_ROOT.resolve()):
            return "unknown", "search freshness: invalid canary path"
        if not source.is_file():
            return "warn", f"search freshness: canary missing ({canary})"
        before = source.stat()
        # Bound even a misconfigured canary; this is a lightweight health probe.
        if before.st_size > 2 * 1024 * 1024:
            return "unknown", "search freshness: canary exceeds 2 MiB limit"
        checksum = hashlib.sha256(source.read_bytes()).hexdigest()
        after = source.stat()
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            return "unknown", "search freshness: canary changed during probe"
        if not db.is_file():
            return "warn", "search freshness: index database missing"
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as conn:
            deadline = time.monotonic() + 1
            conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            # Explicit schema check prevents future layouts looking falsely green.
            entity_cols = {r[1] for r in conn.execute("PRAGMA table_info(entity)")}
            project_cols = {r[1] for r in conn.execute("PRAGMA table_info(project)")}
            if not {"project_id", "file_path", "checksum"} <= entity_cols or not {"id", "path"} <= project_cols:
                return "unknown", "search freshness: unsupported Basic Memory schema"
            project = conn.execute("SELECT id FROM project WHERE path = ?", (str(BRAIN_ROOT.resolve()),)).fetchall()
            if len(project) != 1:
                return "unknown", "search freshness: local corpus project not uniquely configured"
            rows = conn.execute("SELECT checksum FROM entity WHERE project_id = ? AND file_path = ?",
                                (project[0][0], canary)).fetchall()
        if len(rows) != 1 or rows[0][0] != checksum:
            problem = "canary not indexed" if not rows else "canary checksum differs from disk"
            age = time.time() - after.st_mtime
            if 0 <= age < grace:
                return "unknown", f"search freshness: {problem}; within {grace:g}s sync grace ({canary})"
            return "warn", f"search freshness: {problem} ({canary})"
        if not watcher.is_file():
            return "warn", "search freshness: canary matches but watcher state is missing"
        state = json.loads(watcher.read_text())
        if not isinstance(state, dict) or not isinstance(state.get("running"), bool):
            return "warn", "search freshness: watcher state is malformed (missing running boolean)"
        if not state["running"]:
            return "warn", "search freshness: canary matches but watcher reports stopped"
        return "up", f"search freshness: canary checksum matches ({canary})"
    except json.JSONDecodeError:
        return "warn", "search freshness: watcher state is empty or invalid JSON"
    except sqlite3.Error as exc:
        return "unknown", f"search freshness: database read failed ({type(exc).__name__})"
    except (OSError, ValueError, TypeError) as exc:
        return "unknown", f"search freshness: state read failed ({type(exc).__name__})"


def probe(svc, hosts):
    """Resolve and run a health check per conventions.md order."""
    host = hosts.get(svc.get("host"), {})
    health_url = svc.get("health_url")
    domain = svc.get("domain")
    port = svc.get("port")
    unit = svc.get("unit")

    if svc.get("paused"):
        method = "paused"
        state, detail = ("paused", svc["paused"])
    elif health_url and svc.get("probe_from") and not _local(svc["probe_from"]):
        via = hosts.get(svc["probe_from"], {})
        method = f"health_url@{svc['probe_from']}"
        state, detail = _check_http_from(via.get("user"), via.get("hostname"), health_url)
    elif health_url:
        method = "health_url"
        state, detail = _check_http(health_url)
    elif domain:
        method = "domain"
        state, detail = _check_http(f"https://{domain}/")
    elif port:
        method = "port"
        target = "127.0.0.1" if _local(svc.get("host")) else host.get("hostname", "")
        state, detail = _check_tcp(target, port)
    elif unit:
        method = "unit"
        state, detail = _check_unit(
            svc.get("host"), host.get("user"), host.get("hostname"), unit
        )
    else:
        method = "none"
        state, detail = ("unknown", "no health check configured, not g-core compliant")

    if svc.get("id") == "basic-memory":
        method += "+freshness"
        if not _local(svc.get("host")):
            fresh_state, fresh_detail = "unknown", "search freshness: remote corpus probe not configured"
        else:
            fresh_state, fresh_detail = _check_search_freshness()
        detail = f"{detail}; {fresh_detail}"
        if state != "down":
            state = fresh_state if fresh_state != "up" else state

    return {
        "id": svc.get("id"),
        "repo": svc.get("repo"),
        "host": svc.get("host"),
        "domain": domain,
        "unit": unit,
        "method": method,
        "state": state,
        "detail": detail,
    }


def run():
    print("brain_status: probing services from g-core manifest")
    with _client() as client:
        try:
            if not GCORE_REPO:
                raise LookupError("no instance repo: set G_INSTANCE_REPO or give the instance an origin remote")
            manifest = json.loads(_fetch_file(client, GCORE_REPO, "manifest.json"))
        except Exception as e:  # noqa: BLE001
            print(f"error: could not fetch g-core manifest: {e}", file=sys.stderr)
            sys.exit(1)

    hosts = {h["id"]: h for h in manifest.get("hosts", [])}
    results = [probe(svc, hosts) for svc in manifest.get("services", [])]
    out_dir = BRAIN_ROOT / "status"
    remedies = reflexes.Reflexes(out_dir)
    disk = remedies.clean_disk(shutil.disk_usage("/"))
    growers = remedies.disk_report(disk)
    if growers:
        disk["growers"] = growers[:3]
    remedies.repair_services(results, manifest.get("services", []), hosts, probe, _local, _check_unit)

    if disk["used_pct"] >= 90 and "status:disk" not in brain_alerts.load_state():
        grew = "; grew most: " + ", ".join(
            f"{g['path']} +{reflexes.size(g['grew'])}" for g in growers[:3]
        ) + f" in {growers[0]['hours']:g}h" if growers else ""
        brain_alerts.send("error", "disk",
                          f"root disk still {disk['used_pct']:g}% used after cleanup, "
                          f"{disk['free_gb']:g} GiB free{grew}", dedup_key="status:disk")
    elif disk["used_pct"] < 85 and not brain_alerts.dry_run():
        brain_alerts.clear("status:disk")
    for r in results:
        print(f"  {r['id']:16} {r['state']:8} ({r['method']}) {r['detail']}")

    repo_map = _dependency_repo_map()
    catalog = manifest.get("dependencies", [])
    health_cache = _build_health_cache(catalog, manifest.get("services", []))
    deps = [probe_dependency(d, repo_map, health_cache) for d in catalog]
    for d in deps:
        print(f"  {d['id']:16} {d['state']:8} (dep) {d['detail']}")

    out_dir.mkdir(parents=True, exist_ok=True)
    snap_path = out_dir / "snapshot.json"

    # Alert on transitions BEFORE overwriting the snapshot we compare against.
    prior = _prior_states(snap_path)
    _alert_transitions(prior, results + deps)

    snapshot = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "up": sum(1 for r in results if r["state"] == "up"),
            "warn": sum(1 for r in results if r["state"] == "warn"),
            "down": sum(1 for r in results if r["state"] == "down"),
            "unknown": sum(1 for r in results if r["state"] == "unknown"),
            "paused": sum(1 for r in results if r["state"] == "paused"),
            "total": len(results),
        },
        "dep_summary": {
            "up": sum(1 for d in deps if d["state"] == "up"),
            "warn": sum(1 for d in deps if d["state"] == "warn"),
            "down": sum(1 for d in deps if d["state"] == "down"),
            "unknown": sum(1 for d in deps if d["state"] == "unknown"),
            "total": len(deps),
        },
        "services": results,
        "dependencies": deps,
        "disk": disk,
    }
    snap_path.write_text(json.dumps(snapshot, indent=2))
    print(f"brain_status: wrote {snap_path}")
    return snapshot


if __name__ == "__main__":
    run()
