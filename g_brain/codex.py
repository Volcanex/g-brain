"""brain_codex: semantic + lexical code search across the repos mapped into nerves/.

Exposes a single MCP tool: search_code.
Indexes are built from GitHub on first use and cached in memory.
Reads repo list from brain/nerves/.meta.json files (maintained by brain_gmap).
"""

from __future__ import annotations

import asyncio
import json
import os

from fastmcp import FastMCP
from semble import SembleIndex

import g_config

BRAIN_ROOT = g_config.BRAIN_ROOT
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

mcp = FastMCP("codex")

_index_cache: dict[str, SembleIndex] = {}


def _known_repos() -> dict[str, str]:
    """Return {repo_id: github_url} from nerves/.meta.json files."""
    nerves_root = BRAIN_ROOT / "nerves"
    repos = {}
    for meta_file in nerves_root.glob("*/.meta.json"):
        try:
            meta = json.loads(meta_file.read_text())
            repo_path = meta.get("repo")
            if repo_path:
                repos[meta["id"]] = f"https://github.com/{repo_path}"
        except Exception:
            pass
    return repos


def _auth_url(url: str) -> str:
    if GITHUB_TOKEN and "github.com" in url:
        return url.replace("https://", f"https://{GITHUB_TOKEN}@")
    return url


def _build_index(url: str) -> SembleIndex:
    return SembleIndex.from_git(_auth_url(url))


async def _get_index(repo_id: str, url: str) -> SembleIndex:
    if repo_id not in _index_cache:
        loop = asyncio.get_event_loop()
        _index_cache[repo_id] = await loop.run_in_executor(None, _build_index, url)
    return _index_cache[repo_id]


def _format_results(results, repo_id: str) -> list[dict]:
    out = []
    for r in results:
        out.append({
            "repo": repo_id,
            "file": r.chunk.file_path,
            "lines": f"{r.chunk.start_line}-{r.chunk.end_line}",
            "content": r.chunk.content,
        })
    return out


@mcp.tool()
async def search_code(query: str, repo: str = "all", top_k: int = 5) -> list[dict]:
    """Search code across the mapped repos using semantic + lexical search.

    Finds the exact code chunks relevant to a query. Uses ~98% fewer tokens
    than reading full files. Indexes are built from GitHub on first call and
    cached for the session.

    Args:
        query: Natural language or symbol query, e.g. "authentication flow",
               "save_pretrained", "how is billing calculated"
        repo:  Repo id (e.g. "period", "adze") or "all" to search everything.
               Run search_code with repo="list" to see available repos.
        top_k: Number of chunks to return per repo (default 5).

    Returns list of {repo, file, lines, content} dicts.
    """
    known = _known_repos()

    if repo == "list":
        return [{"available_repos": list(known.keys())}]

    if repo != "all":
        if repo not in known:
            return [{"error": f"unknown repo '{repo}'. Use repo='list' to see options."}]
        targets = {repo: known[repo]}
    else:
        targets = known

    results = []
    for repo_id, url in targets.items():
        try:
            index = await _get_index(repo_id, url)
            hits = index.search(query, top_k=top_k)
            results.extend(_format_results(hits, repo_id))
        except Exception as e:
            results.append({"repo": repo_id, "error": str(e)})

    return results
