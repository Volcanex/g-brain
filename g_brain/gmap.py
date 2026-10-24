"""brain_gmap.py: Sync layers 3+4 from GitHub into the brain.

Layer 3 (nerves):  fetches all CLAUDE.md files per repo → brain/nerves/{id}/
Layer 4 (lineage): fetches pyproject.toml per repo       → brain/lineage/{id}.toml

Also writes brain/nerves/{id}/.meta.json with:
  - compliance flags (has_agents_md, has_pyproject)
  - full directory list (for the nerves graph)
  - one entry per directory carrying a CLAUDE.md or AGENTS.md, with the
    pair state of the two (see pair_state). Mode and blob sha come from
    the tree we already fetched, so this costs no extra API calls
  - sync timestamp

Source of truth for the repo list: the instance's manifest.json on GitHub.
Run on brain-mcp startup and daily via cron.
"""
import json
import sys
import base64
import tomllib
from datetime import datetime, timezone

import httpx

import g_config

GITHUB_TOKEN = g_config.env("GITHUB_TOKEN") or None

BRAIN_ROOT = g_config.BRAIN_ROOT
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
    data = r.json()
    return base64.b64decode(data["content"]).decode("utf-8", errors="replace")


def _fetch_tree(client, repo):
    """Return full recursive git tree for repo's default branch."""
    repo_info = client.get(f"{API}/repos/{repo}").raise_for_status().json()
    branch = repo_info["default_branch"]
    branch_info = client.get(f"{API}/repos/{repo}/branches/{branch}").raise_for_status().json()
    tree_sha = branch_info["commit"]["commit"]["tree"]["sha"]
    r = client.get(f"{API}/repos/{repo}/git/trees/{tree_sha}", params={"recursive": "1"})
    r.raise_for_status()
    return r.json()


SYMLINK_MODE = "120000"
# The vault is a system repo too, and its nerves/ is this very mirror. Syncing it would
# copy the mirror into itself one level deeper on every run (nerves/brain/nerves/brain/...),
# so a repo's nerves/ is never read as nerves.
MIRROR_DIR = "nerves/"


def own_entries(entries):
    """The tree without anything under a top-level nerves/ mirror."""
    return [e for e in entries if not e["path"].startswith(MIRROR_DIR)]


def prune_empty_dirs(root):
    """Remove directories left empty once their CLAUDE.md files went, deepest first."""
    for directory in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


def pair_state(claude, agents):
    """How a directory's CLAUDE.md and AGENTS.md relate.

    The convention is that AGENTS.md mirrors CLAUDE.md, ideally as a symlink.
    Two real files are legal but drift silently, so they are called apart.
    """
    if claude and not agents:
        return "claude_only"
    if agents and not claude:
        return "agents_only"
    if claude["mode"] == SYMLINK_MODE or agents["mode"] == SYMLINK_MODE:
        return "symlink"
    if claude["sha"] == agents["sha"]:
        return "copy_synced"
    return "copy_drift"


def collect_nerves(entries):
    """One record per directory holding a CLAUDE.md and/or AGENTS.md."""
    by_dir = {}
    for e in entries:
        if e["type"] != "blob":
            continue
        path = e["path"]
        name = path.rsplit("/", 1)[-1]
        if name not in ("CLAUDE.md", "AGENTS.md"):
            continue
        directory = path.rsplit("/", 1)[0] if "/" in path else ""
        by_dir.setdefault(directory, {})[name] = {
            "mode": e["mode"], "sha": e["sha"],
        }

    out = []
    for directory in sorted(by_dir):
        claude = by_dir[directory].get("CLAUDE.md")
        agents = by_dir[directory].get("AGENTS.md")
        out.append({
            "dir": directory,
            "claude": claude,
            "agents": agents,
            "pair": pair_state(claude, agents),
        })
    return out


def sync_repo(client, repo_url, repo_id):
    repo = repo_url.replace("https://github.com/", "")
    print(f"  {repo_id} ({repo})", end=" ... ", flush=True)

    try:
        tree_data = _fetch_tree(client, repo)
    except httpx.HTTPStatusError as e:
        print(f"skip ({e.response.status_code})")
        return

    entries = own_entries(tree_data.get("tree", []))
    paths = {e["path"] for e in entries}
    dir_paths = sorted({e["path"] for e in entries if e["type"] == "tree"})
    claude_paths = [e["path"] for e in entries if e["type"] == "blob" and e["path"].endswith("CLAUDE.md")]
    has_agents_md = any(e["path"] == "AGENTS.md" for e in entries)
    has_pyproject = "pyproject.toml" in paths

    # Layer 3: fetch CLAUDE.md files
    nerves_dir = BRAIN_ROOT / "nerves" / repo_id
    nerves_dir.mkdir(parents=True, exist_ok=True)

    # Remove stale files first (repo may have deleted some)
    for old in nerves_dir.rglob("CLAUDE.md"):
        old.unlink()

    for claude_path in claude_paths:
        try:
            content = _fetch_file(client, repo, claude_path)
        except Exception:
            continue
        out = nerves_dir / claude_path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content)
    prune_empty_dirs(nerves_dir)

    # Write meta (compliance + full dir list for graph)
    top_level_dirs = [d for d in dir_paths if "/" not in d]
    nerves = collect_nerves(entries)
    meta = {
        "repo": repo,
        "id": repo_id,
        "has_agents_md": has_agents_md,
        "has_pyproject": has_pyproject,
        "dirs": top_level_dirs,
        "nerves": nerves,
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }
    (nerves_dir / ".meta.json").write_text(json.dumps(meta, indent=2))

    # Layer 4: fetch pyproject.toml
    lineage_dir = BRAIN_ROOT / "lineage"
    lineage_dir.mkdir(parents=True, exist_ok=True)
    if has_pyproject:
        try:
            content = _fetch_file(client, repo, "pyproject.toml")
            (lineage_dir / f"{repo_id}.toml").write_text(content)
        except Exception:
            pass

    unpaired = sum(1 for n in nerves if n["pair"] != "symlink" and n["pair"] != "copy_synced")
    print(f"{len(claude_paths)} CLAUDE.md, {len(top_level_dirs)} dirs, "
          f"{len(nerves)} nerve dirs ({unpaired} unpaired)")


def run():
    print("brain_gmap: syncing layers 3+4 from GitHub")

    with _client() as client:
        # Fetch canonical repo list from the instance manifest
        try:
            if not GCORE_REPO:
                raise LookupError("no instance repo: set G_INSTANCE_REPO or give the instance an origin remote")
            manifest_raw = _fetch_file(client, GCORE_REPO, "manifest.json")
        except Exception as e:
            print(f"error: could not fetch g-core manifest: {e}", file=sys.stderr)
            sys.exit(1)

        manifest = json.loads(manifest_raw)
        repos = manifest.get("system", []) + manifest.get("projects", [])

        for entry in repos:
            url = entry.get("url", "")
            if not url or "github.com" not in url:
                continue
            sync_repo(client, url, entry["id"])

    print("brain_gmap: done")


if __name__ == "__main__":
    run()
