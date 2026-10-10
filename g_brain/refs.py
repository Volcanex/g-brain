"""brain-refs: scan all notes for URLs, validate them, build the secondary graph.

Usage:
  brain-refs scan         # walk all .md files, extract URLs, populate sqlite
  brain-refs check        # HEAD each URL, update status
  brain-refs view         # regenerate .refs-index.md from sqlite
  brain-refs all          # scan + check + view (cron-friendly)
"""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
import sys

from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import httpx

import g_config

BRAIN_ROOT = g_config.BRAIN_ROOT

TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "utm_id", "utm_name", "fbclid", "gclid", "gbraid", "wbraid", "msclkid",
    "mc_cid", "mc_eid", "ref", "ref_src", "ref_url", "_hsenc", "_hsmi",
    "vero_conv", "vero_id", "yclid", "igshid", "spm",
}


def canonicalize(url: str) -> str:
    """Lowercase scheme+host, drop fragment, strip common tracking params,
    drop trailing slash on non-root paths. Domain-specific normalization
    (arxiv version suffixes, youtube variants) is intentionally not done here."""
    # urlsplit is lazy: it parses fine, then raises ValueError from the
    # .hostname / .port properties on a malformed authority. A single note
    # containing `...:7500\`` (stray backtick) was enough to kill the whole
    # scan, and the link checker had been dead ever since, silently, which
    # is the failure mode that matters. A bad URL is now just a bad URL.
    try:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower() or "https"
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return url
    if host.startswith("www."):
        host = host[4:]
    netloc = host
    if port and not (
        (scheme == "http" and port == 80)
        or (scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query_pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                   if k.lower() not in TRACKING_PARAMS]
    query = urlencode(query_pairs)
    return urlunsplit((scheme, netloc, path, query, ""))
DB = BRAIN_ROOT / ".refs-index.sqlite"
MD_VIEW = BRAIN_ROOT / ".refs-index.md"

URL_RE = re.compile(r"https?://[^\s\)\]\"'<>]+")
TRAILING = ".,;:!?)\"'"


def _today() -> str:
    return dt.date.today().isoformat()


def _open() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS refs (
            url TEXT,
            canonical_url TEXT,
            note TEXT,
            first_seen TEXT,
            last_seen TEXT,
            status TEXT,
            last_checked TEXT,
            temporal_marker TEXT,
            PRIMARY KEY (url, note)
        );
        CREATE INDEX IF NOT EXISTS idx_refs_url ON refs(url);
        CREATE INDEX IF NOT EXISTS idx_refs_note ON refs(note);
        CREATE INDEX IF NOT EXISTS idx_refs_status ON refs(status);
        """
    )
    return conn


def scan() -> None:
    conn = _open()
    today = _today()
    pairs: set[tuple[str, str]] = set()
    for md in BRAIN_ROOT.rglob("*.md"):
        if any(part.startswith(".") for part in md.relative_to(BRAIN_ROOT).parts):
            continue
        rel = str(md.relative_to(BRAIN_ROOT))
        text = md.read_text(errors="ignore")
        for raw in URL_RE.findall(text):
            url = raw.rstrip(TRAILING)
            canonical = canonicalize(url)
            pairs.add((url, rel))
            conn.execute(
                """
                INSERT INTO refs (url, canonical_url, note, first_seen, last_seen,
                                  status, last_checked, temporal_marker)
                VALUES (?, ?, ?, ?, ?, 'unknown', NULL, NULL)
                ON CONFLICT(url, note) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    canonical_url = excluded.canonical_url
                """,
                (url, canonical, rel, today, today),
            )
    conn.commit()
    conn.close()
    print(f"scanned {len(pairs)} (url, note) pairs across {BRAIN_ROOT}")


def check() -> None:
    conn = _open()
    today = _today()
    cur = conn.execute("SELECT DISTINCT url FROM refs")
    urls = [r[0] for r in cur.fetchall()]
    headers = {"User-Agent": g_config.USER_AGENT}
    with httpx.Client(timeout=10, follow_redirects=True, headers=headers) as client:
        for url in urls:
            try:
                r = client.head(url)
                if r.status_code == 405:
                    r = client.get(url)
                status = "ok" if r.status_code < 400 else f"http-{r.status_code}"
            except httpx.HTTPError as e:
                status = f"error-{type(e).__name__}"
            except Exception as e:
                status = f"error-{type(e).__name__}"
            conn.execute(
                "UPDATE refs SET status = ?, last_checked = ? WHERE url = ?",
                (status, today, url),
            )
            print(f"{status:24s} {url}")
    conn.commit()
    conn.close()


def view() -> None:
    conn = _open()
    lines = [
        "---",
        "title: References Index",
        "kind: reference",
        "version: 1.0.0",
        "lineage: [A]",
        "status: active",
        f"updated: {_today()}",
        "tags: [refs, auto-generated]",
        "---",
        "",
        "# References Index",
        "",
        "_Auto-generated by brain-refs. Do not edit by hand._",
        "",
        "## Most-cited URLs",
        "",
    ]
    cur = conn.execute(
        """
        SELECT canonical_url, COUNT(DISTINCT note) AS n_notes,
               COUNT(DISTINCT url) AS n_variants, MAX(status) AS status
        FROM refs GROUP BY canonical_url
        ORDER BY n_notes DESC, canonical_url ASC LIMIT 50
        """
    )
    for canonical, n, n_variants, status in cur.fetchall():
        marker = "ok" if status == "ok" else ("4xx" if status and status.startswith("http-4") else "?")
        suffix = f" _(status: {status}, {n_variants} URL variants)_" if n_variants > 1 \
                 else f" _(status: {status})_"
        lines.append(f"- [{marker}] **{n}x** {canonical}{suffix}")

    cur = conn.execute(
        """
        SELECT url, note FROM refs
        WHERE status LIKE 'http-4%' OR status LIKE 'http-5%' OR status LIKE 'error-%'
        ORDER BY url
        """
    )
    rows = cur.fetchall()
    if rows:
        lines += ["", "## Broken URLs", ""]
        for url, note in rows:
            lines.append(f"- `{note}` cites broken: {url}")

    MD_VIEW.write_text("\n".join(lines) + "\n")
    conn.close()
    print(f"wrote {MD_VIEW}")


COMMANDS = {"scan": scan, "check": check, "view": view}


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if cmd == "all":
        scan()
        check()
        view()
        return
    if cmd not in COMMANDS:
        print(f"unknown command: {cmd}; choices: scan, check, view, all", file=sys.stderr)
        sys.exit(2)
    COMMANDS[cmd]()


if __name__ == "__main__":
    main()
