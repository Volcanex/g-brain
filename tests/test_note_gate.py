"""Layer routing and the write gate, using only invented notes in a temp vault."""
import asyncio
import json

import frontmatter
import pytest

BAR = "Avoids repeating this invented mistake, which is recorded nowhere else."
BODY = "The amber adapter drops queued messages during reconnect unless its buffer is drained first."


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    from g_brain import capture, commit, index, reader

    for module in (capture, commit, index, reader):
        monkeypatch.setattr(module, "BRAIN_ROOT", tmp_path)
    monkeypatch.setattr(commit, "COMMITS_LOG", tmp_path / "commits.log")
    monkeypatch.setattr(index, "INDEXES_DIR", tmp_path / "indexes")
    reader._all_notes_cached.cache_clear()
    yield tmp_path, commit
    reader._all_notes_cached.cache_clear()


def seed(root, target, title, body=BODY):
    path = root / target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dumps(frontmatter.Post(body, title=title)))
    return path


@pytest.mark.parametrize("layer,folder,kind", [
    ("design", "designs", "design"), ("idea", "ideas", "idea"),
    ("about", "about", "fact"), ("hazard", "hazards", "hazard"),
])
def test_layer_routes_and_persists_bar(corpus, layer, folder, kind):
    root, commit = corpus
    out = commit.brain_note(BODY, layer, title="Amber adapter", bar="  " + BAR + "  ")
    assert out["ok"] and out["action"] == "created"
    assert out["target"] == f"{folder}/amber-adapter.md"
    post = frontmatter.load(root / out["target"])
    assert (post["layer"], post["kind"], post["bar"]) == (layer, kind, BAR)
    assert out["indexed"] is True
    assert "Amber adapter" in (root / "indexes/master.md").read_text()


@pytest.mark.parametrize("args", [{}, {"layer": "finding"}, {"layer": "fact"},
    {"kind": "finding"}, {"kind": "reference"}, {"kind": "todo"}, {"kind": "source"},
    {"layer": "hazard", "kind": "design"}])
def test_missing_layer_and_retired_kind_return_routing_help(corpus, args):
    root, commit = corpus
    out = commit.brain_note(BODY, bar=BAR, **args)
    assert out["ok"] is False
    for word in ("design", "idea", "about", "hazard", "repo docs", "g-levels",
                 "create_todo", "create_ticket", "pulse_note"):
        assert word in out["error"]
    assert not (root / "commits.log").exists()


@pytest.mark.parametrize("kind", ["design", "idea"])
def test_deprecated_kind_alias(corpus, kind):
    root, commit = corpus
    out = commit.brain_note(BODY, kind=kind, bar=BAR)
    assert out["ok"]
    assert frontmatter.load(root / out["target"])["layer"] == kind


@pytest.mark.parametrize("bar", [None, "", "  ", "\n\t"])
def test_new_note_requires_bar_even_when_distinct(corpus, bar):
    root, commit = corpus
    out = commit.brain_note(BODY, "hazard", bar=bar, distinct=True)
    assert out["ok"] is False
    assert "Would a future agent be worse off without this, and is it recorded nowhere else?" in out["error"]
    assert not (root / "hazards").exists()


def test_multiline_bar_is_rejected(corpus):
    _, commit = corpus
    assert not commit.brain_note(BODY, "hazard", bar="first\nsecond")["ok"]


@pytest.mark.parametrize("explicit_target", [True, False])
def test_append_preserves_prior_bar_and_needs_no_new_bar(corpus, explicit_target):
    root, commit = corpus
    first = commit.brain_note(BODY, "hazard", title="Amber adapter", bar=BAR)
    kwargs = {"target": "hazards/amber-adapter"} if explicit_target else {"title": "Amber adapter"}
    out = commit.brain_note("Drain completion is signalled by a green lamp.", "hazard", **kwargs)
    assert out["ok"] and out["action"] == "appended"
    assert out["title"] == "Amber adapter"
    post = frontmatter.load(root / first["target"])
    assert post["bar"] == BAR and post["layer"] == "hazard"
    assert BODY in post.content and "green lamp" in post.content
    assert out["summary_needs_review"]
    assert list((root / "hazards/.history").glob("*.md"))


def test_target_requires_existing_note_and_matching_layer(corpus):
    root, commit = corpus
    assert not commit.brain_note(BODY, "hazard", target="hazards/missing.md")["ok"]
    for path in ("findings/old.md", "references/old.md", "about/person.md"):
        old = seed(root, path, "Legacy note")
        before = old.read_text()
        assert not commit.brain_note("more", "hazard", target=path)["ok"]
        assert old.read_text() == before


def test_near_duplicate_refusal_and_distinct_override(corpus):
    root, commit = corpus
    first = commit.brain_note(BODY, "hazard", title="Amber reconnect buffer", bar=BAR)
    out = commit.brain_note(BODY, "design", title="Amber adapter drain", bar=BAR)
    assert out["ok"] is False
    hit, = out["near_duplicates"]
    assert hit["target"] == first["target"] and hit["title"] == "Amber reconnect buffer"
    assert 0.6 <= hit["score"] <= 1
    assert "target=<path>" in out["error"] and "distinct=True" in out["error"]
    assert not (root / "designs").exists()
    override = commit.brain_note(BODY, "design", title="Amber adapter drain", bar=BAR, distinct=True)
    assert override["ok"] and override["action"] == "created"
    appended = commit.brain_note("A second observed detail.", "hazard", target=hit["target"])
    assert appended["ok"] and appended["action"] == "appended"


@pytest.mark.parametrize("title,text,duplicate", [
    ("Amber buffer drain", BODY, True),
    ("Amber reconnect workaround", "Drain the amber adapter buffer first during reconnect or queued messages are dropped.", True),
    ("Amber adapter recovery", "The amber adapter drops queued messages during reconnect unless its buffer is drained first. A status lamp confirms completion.", True),
    ("Amber adapter limits", "The amber adapter supports a maximum payload of four kilobytes per message.", False),
    ("Amber adapter reconnect", "A violet gateway negotiates credentials over a separate encrypted channel.", False),
    ("Orchard irrigation", "Pear trees require deep watering through dry summers and shallow pruning in winter.", False),
])
def test_overlap_calibration_with_invented_fixtures(corpus, title, text, duplicate):
    root, commit = corpus
    seed(root, "hazards/original.md", "Amber adapter reconnect")
    out = commit.brain_note(text, "hazard", title=title, bar=BAR)
    assert (out.get("near_duplicates") is not None) is duplicate
    assert out["ok"] is not duplicate


def test_candidates_are_limited_ranked_and_only_from_kept_layers(corpus):
    root, commit = corpus
    for folder in ("findings", "references", "nerves", "todos", "tickets", "sources",
                   "hazards/versions", "ideas/archive", "designs/.history"):
        seed(root, f"{folder}/old.md", "Amber adapter")
    assert commit._near_duplicates("Amber adapter", BODY) == []
    for i, folder in enumerate(("hazards", "designs", "ideas", "about")):
        seed(root, f"{folder}/{i}.md", "Amber adapter")
    hits = commit._near_duplicates("Amber adapter", BODY)
    assert len(hits) == 3
    assert all(h["score"] == 1.0 for h in hits)
    assert [h["target"] for h in hits] == sorted(h["target"] for h in hits)


def test_duplicate_check_failure_does_not_block_capture(corpus, monkeypatch):
    from g_brain import reader
    _, commit = corpus
    def broken():
        raise OSError("invented read failure")
    monkeypatch.setattr(reader, "all_notes", broken)
    assert commit.brain_note(BODY, "hazard", bar=BAR)["ok"]


@pytest.mark.parametrize("text", ["Always drain the adapter before reconnecting.",
    "Never reuse stale credentials.", "Clients must drain the adapter.",
    "Do not reuse a session key.", "Every time credentials rotate, refresh the cache."])
def test_standing_directives_write_with_advisory_hint(corpus, text):
    root, commit = corpus
    out = commit.brain_note(text, "hazard", bar=BAR)
    assert out["ok"] and (root / out["target"]).exists()
    assert "glevels" in out["hint"] and "/home/gabriel/g-levels" in out["hint"]


def test_description_does_not_get_directive_hint(corpus):
    _, commit = corpus
    out = commit.brain_note("The amber adapter always reports its firmware revision.", "hazard", bar=BAR)
    assert out["ok"] and "hint" not in out


@pytest.mark.parametrize("folder,kind", [("findings", "finding"), ("references", "reference"),
    ("sources", "source"), ("logs", "log"), ("todos", "todo"), ("about", "fact"), ("hazards", "hazard")])
def test_commit_machinery_retains_legacy_and_other_kinds(corpus, folder, kind):
    root, commit = corpus
    plan = commit.propose_commit("Maintain an invented note")
    out = commit.execute_commit(plan["plan_id"], [{"type": "create", "target": f"{folder}/legacy.md",
                                                  "body": BODY}])
    assert out["results"][0]["ok"]
    assert frontmatter.load(root / f"{folder}/legacy.md")["kind"] == kind
    edited = commit.execute_commit(plan["plan_id"], [{"type": "edit_minor", "target": f"{folder}/legacy.md",
                                                      "edits": [{"op": "append", "content": "Extra detail."}]}])
    assert edited["results"][0]["ok"]


def test_indexes_contain_only_kept_layers_and_clear_empty_master(corpus):
    from g_brain import index
    root, _ = corpus
    for folder in ("designs", "ideas", "about", "hazards", "findings", "references", "nerves", "todos", "tickets"):
        seed(root, f"{folder}/fixture.md", f"Fixture {folder}")
    index.compile_indexes()
    master = (root / "indexes/master.md").read_text()
    for folder in ("designs", "ideas", "about", "hazards"):
        assert f"indexes/{folder}" in master
        assert (root / f"indexes/{folder}.md").is_file()
        (root / f"{folder}/fixture.md").unlink()
    for folder in ("findings", "references", "nerves", "todos", "tickets"):
        assert folder not in master
    index.compile_indexes()
    assert "Fixture" not in (root / "indexes/master.md").read_text()


def test_capture_maps_kinds_and_reports_unmapped_notes(corpus):
    from g_brain import capture
    root, _ = corpus
    out = capture.apply({"notes": [
        {"kind": "design", "text": "A ring of mirrors redirects daylight into the courtyard.", "bar": BAR},
        {"kind": "idea", "text": "Map orchard moisture onto a musical scale for an installation.", "bar": BAR},
        {"kind": "fact", "text": "The fictional owner prefers paper invitations for family gatherings.", "bar": BAR},
        {"kind": "hazard", "text": BODY, "bar": BAR},
        {"kind": "finding", "text": "A legacy finding with no obvious layer."},
        {"kind": "reference", "text": "Project canon belongs in its repo."},
        {"text": "No kind or layer."},
    ]})
    assert [r["action"] for r in out] == ["created"] * 4 + ["skipped"] * 3
    for result, layer in zip(out, ("design", "idea", "about", "hazard")):
        post = frontmatter.load(root / result["target"])
        assert post["layer"] == layer and post["bar"] == BAR
        assert post["lineage"] == ["A"] * 3
    assert all("No clear layer mapping" in r["reason"] for r in out[4:])


def test_capture_surfaces_gate_refusal_and_cli_failure(corpus, monkeypatch, capsys):
    import io
    import sys
    from g_brain import capture
    root, commit = corpus
    commit.brain_note(BODY, "hazard", title="Amber adapter", bar=BAR)
    plan = {"notes": [{"layer": "hazard", "text": BODY, "title": "Amber reconnect", "bar": BAR}]}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(plan)))
    assert capture.main(["apply"]) == 1
    output = capsys.readouterr().out
    assert "FAILED" in output and "near_duplicates" in output and "hazards/amber-adapter.md" in output


def test_capture_search_finds_new_layers(corpus):
    from g_brain import capture
    root, _ = corpus
    seed(root, "about/preference.md", "Imaginary orchard preference", "Paper invitations.")
    seed(root, "findings/old.md", "Imaginary orchard legacy", "Legacy data.")
    hits = capture.search("imaginary orchard")
    assert [h[1] for h in hits] == ["about/preference.md"]


def test_mcp_missing_layer_returns_help_and_accepts_new_signature(corpus):
    from fastmcp import Client
    root, commit = corpus
    async def exercise():
        async with Client(commit.mcp) as client:
            missing = await client.call_tool("brain_note", {"text": BODY})
            assert missing.data["ok"] is False and "layer is required" in missing.data["error"]
            retired = await client.call_tool("brain_note", {"text": BODY, "kind": "finding"})
            assert retired.data["ok"] is False and "kind is deprecated" in retired.data["error"]
            created = await client.call_tool("brain_note", {"text": BODY, "layer": "hazard", "bar": BAR})
            assert created.data["ok"] and (root / created.data["target"]).exists()
    asyncio.run(exercise())


def test_duplicate_cache_observes_same_timestamp_creates(corpus):
    import os
    root, commit = corpus
    stamp = 1700000000000000000
    old = seed(root, "nerves/ignored.md", "Amber adapter")
    os.utime(old, ns=(stamp, stamp))
    assert commit._near_duplicates("Amber adapter", BODY) == []
    for folder in ("ideas", "hazards"):
        new = seed(root, f"{folder}/fresh.md", "Amber adapter")
        os.utime(new, ns=(stamp, stamp))
    assert len(commit._near_duplicates("Amber adapter", BODY)) == 2
