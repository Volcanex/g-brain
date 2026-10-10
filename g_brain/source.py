"""brain-source: bookkeeping for source-kind notes.

Commands:
  brain-source add <url>         Fetch metadata, write sources/<slug>.md
  brain-source bulk-add <file>   One URL per line; sequential add
  brain-source check             HEAD each source URL, update status field
  brain-source archive           Submit URLs without archive_url to Wayback
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import frontmatter
import httpx

import g_config

BRAIN_ROOT = g_config.BRAIN_ROOT
SOURCES = BRAIN_ROOT / "sources"
USER_AGENT = g_config.USER_AGENT


def _today() -> str:
    return dt.date.today().isoformat()


def slugify(s: str, maxlen: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:maxlen] or "untitled"


class _MetaParser(HTMLParser):
    """Tiny extractor: <title>, og:title, og:description, meta description, first <h1>."""

    def __init__(self) -> None:
        super().__init__()
        self.title: str | None = None
        self.og_title: str | None = None
        self.og_desc: str | None = None
        self.meta_desc: str | None = None
        self.h1: str | None = None
        self._in_title = False
        self._in_h1 = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "h1" and self.h1 is None:
            self._in_h1 = True
        elif tag == "meta":
            prop = (d.get("property") or "").lower()
            name = (d.get("name") or "").lower()
            content = d.get("content") or ""
            if prop == "og:title" and not self.og_title:
                self.og_title = content
            elif prop == "og:description" and not self.og_desc:
                self.og_desc = content
            elif name == "description" and not self.meta_desc:
                self.meta_desc = content

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False

    def handle_data(self, data: str) -> None:
        if self._in_title and not self.title:
            self.title = data.strip()
        if self._in_h1 and not self.h1:
            self.h1 = data.strip()


def _guess_source_type(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if "arxiv.org" in host or "acm.org" in host or "ieee.org" in host:
        return "paper"
    if "youtube.com" in host or "youtu.be" in host or "vimeo.com" in host:
        return "video"
    if "github.com" in host or host.endswith("readthedocs.io") or "docs." in host:
        return "docs"
    return "blog"


def add(url: str) -> Path:
    """Fetch URL, extract title/description, write sources/<slug>.md."""
    SOURCES.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(timeout=20, follow_redirects=True, headers=headers) as client:
        r = client.get(url)
        r.raise_for_status()
        html = r.text
        canonical = str(r.url)

    parser = _MetaParser()
    try:
        parser.feed(html)
    except Exception:
        pass

    title = parser.og_title or parser.title or parser.h1 or urlparse(canonical).hostname or "untitled"
    description = parser.og_desc or parser.meta_desc or ""
    title = re.sub(r"\s+", " ", title).strip()
    description = re.sub(r"\s+", " ", description).strip()

    slug = slugify(title)
    target = SOURCES / f"{slug}.md"
    n = 1
    while target.exists():
        existing = frontmatter.load(target)
        if existing.get("canonical_url") == canonical or existing.get("url") == url:
            print(f"already exists: {target.relative_to(BRAIN_ROOT)} (skip)")
            return target
        target = SOURCES / f"{slug}-{n}.md"
        n += 1

    fm = {
        "title": title,
        "kind": "source",
        "version": "1.0.0",
        "lineage": ["A", "A", "A"],
        "status": "active",
        "created": _today(),
        "updated": _today(),
        "url": url,
        "canonical_url": canonical,
        "source_type": _guess_source_type(canonical),
        "accessed": _today(),
        "confidence": "independent",
        "tags": [],
    }
    body = f"# {title}\n"
    if description:
        body += f"\n{description}\n"
    body += f"\n<{canonical}>\n"

    post = frontmatter.Post(body, **fm)
    target.write_text(frontmatter.dumps(post) + "\n")
    print(f"wrote {target.relative_to(BRAIN_ROOT)}")
    return target


def bulk_add(file_path: str) -> None:
    urls = Path(file_path).read_text().splitlines()
    for raw in urls:
        url = raw.strip()
        if not url or url.startswith("#"):
            continue
        try:
            add(url)
        except Exception as e:
            print(f"FAIL {url}: {type(e).__name__}: {e}", file=sys.stderr)


def check() -> None:
    today = _today()
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(timeout=10, follow_redirects=True, headers=headers) as client:
        for md in SOURCES.glob("*.md"):
            post = frontmatter.load(md)
            url = post.get("url")
            if not url:
                continue
            try:
                r = client.head(url)
                if r.status_code == 405:
                    r = client.get(url)
                if r.status_code >= 400:
                    post["status"] = "broken"
                    post["broken_reason"] = f"HTTP {r.status_code}"
                else:
                    if post.get("status") == "broken" and post.get("broken_reason", "").startswith("HTTP"):
                        post["status"] = "active"
                        post.pop("broken_reason", None)
            except Exception as e:
                post["status"] = "broken"
                post["broken_reason"] = type(e).__name__
            post["last_checked"] = today
            md.write_text(frontmatter.dumps(post) + "\n")
            print(f"{post.get('status','?'):8s} {url}")


def archive() -> None:
    """Submit URLs lacking archive_url to the Wayback Machine."""
    headers = {"User-Agent": USER_AGENT}
    with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as client:
        for md in SOURCES.glob("*.md"):
            post = frontmatter.load(md)
            url = post.get("url")
            if not url or post.get("archive_url"):
                continue
            save_url = f"https://web.archive.org/save/{url}"
            try:
                r = client.get(save_url)
                if r.status_code < 400:
                    final = str(r.url)
                    post["archive_url"] = final
                    md.write_text(frontmatter.dumps(post) + "\n")
                    print(f"archived: {url} -> {final}")
                else:
                    print(f"archive failed ({r.status_code}): {url}")
            except Exception as e:
                print(f"archive error {url}: {type(e).__name__}: {e}", file=sys.stderr)


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    cmd, *rest = sys.argv[1:]
    if cmd == "add":
        add(rest[0])
    elif cmd == "bulk-add":
        bulk_add(rest[0])
    elif cmd == "check":
        check()
    elif cmd == "archive":
        archive()
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
