"""gmap mirrors each repo's CLAUDE.md files, and never the vault's own mirror."""
import json


def _tree(*paths, symlink=()):
    return {"tree": [{"path": p, "type": "blob", "mode": "120000" if p in symlink else "100644", "sha": p}
                     for p in paths] + [{"path": "sub", "type": "tree", "mode": "040000", "sha": "t"}]}


def test_vault_mirror_is_not_copied_into_itself_and_old_copies_go(tmp_path, monkeypatch):
    from g_brain import gmap
    monkeypatch.setattr(gmap, "BRAIN_ROOT", tmp_path)
    nested = tmp_path / "nerves" / "brain" / "nerves" / "brain" / "nerves" / "g-site"
    nested.mkdir(parents=True)
    (nested / "CLAUDE.md").write_text("an old nested copy")
    tree = _tree("CLAUDE.md", "AGENTS.md", "sub/CLAUDE.md", "nerves/g-site/CLAUDE.md",
                 "nerves/brain/nerves/g-site/CLAUDE.md", symlink=("AGENTS.md",))
    monkeypatch.setattr(gmap, "_fetch_tree", lambda client, repo: tree)
    fetched = []
    monkeypatch.setattr(gmap, "_fetch_file", lambda client, repo, path, ref="HEAD": fetched.append(path) or f"# {path}")
    gmap.sync_repo(None, "https://github.com/Owner/brain-vault", "brain")
    root = tmp_path / "nerves" / "brain"
    assert sorted(str(p.relative_to(root)) for p in root.rglob("CLAUDE.md")) == ["CLAUDE.md", "sub/CLAUDE.md"]
    assert not (root / "nerves").exists()
    assert "nerves/g-site/CLAUDE.md" not in fetched
    meta = json.loads((root / ".meta.json").read_text())
    assert [n["dir"] for n in meta["nerves"]] == ["", "sub"]
    assert meta["nerves"][0]["pair"] == "symlink"


def test_other_repos_keep_every_claude_md(tmp_path, monkeypatch):
    from g_brain import gmap
    monkeypatch.setattr(gmap, "BRAIN_ROOT", tmp_path)
    monkeypatch.setattr(gmap, "_fetch_tree", lambda client, repo: _tree("CLAUDE.md", "api/CLAUDE.md", "app/lib/pub/CLAUDE.md"))
    monkeypatch.setattr(gmap, "_fetch_file", lambda client, repo, path, ref="HEAD": path)
    gmap.sync_repo(None, "https://github.com/Owner/wing", "wing")
    root = tmp_path / "nerves" / "wing"
    assert sorted(str(p.relative_to(root)) for p in root.rglob("CLAUDE.md")) == ["CLAUDE.md", "api/CLAUDE.md", "app/lib/pub/CLAUDE.md"]
