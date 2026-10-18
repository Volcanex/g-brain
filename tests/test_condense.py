"""condense's stale check takes its retired terms from the instance manifest."""
import json
import os
from unittest.mock import patch


def _note(body):
    return {"path": "findings/x", "title": "x", "kind": "finding", "status": "active",
            "body": body, "updated": "2026-01-01"}


def test_retired_terms_come_from_the_manifest(tmp_path):
    from g_brain import condense
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"retired_terms": [
        {"term": "oldhub.example", "reason": "the old hub was retired"}]}))
    with patch.object(condense, "MANIFEST", manifest):
        stale = condense.find_stale([_note("Deploy through oldhub.example first.")])
    assert [s["reasons"] for s in stale] == [["mentions `oldhub.example` (the old hub was retired)"]]


def test_no_manifest_means_no_retired_terms(tmp_path):
    from g_brain import condense
    with patch.object(condense, "MANIFEST", tmp_path / "manifest.json"):
        assert condense.find_stale([_note("Deploy through oldhub.example first.")]) == []


def _write_note(root, path, kind="idea", body="Independent fixture evidence."):
    md = root / f"{path}.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(f"---\ntitle: Fixture\nkind: {kind}\nsummary: Fixture summary\n---\n{body}\n")
    return md


def test_cache_reuses_report_and_invalidates_same_size_note_change(tmp_path):
    from g_brain import condense
    md = _write_note(tmp_path, "hazards/fixture", kind="legacy")
    with patch.object(condense, "scan", wraps=condense.scan) as scan:
        first = condense.read_report(tmp_path)
        assert first["counts"]["drift"] == 1
        assert condense.read_report(tmp_path) is first
        assert scan.call_count == 1
        before = md.stat()
        md.write_text(md.read_text().replace("kind: legacy", "kind: hazard"))
        assert md.stat().st_size == before.st_size
        os.utime(md, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        assert condense.read_report(tmp_path)["counts"]["drift"] == 0
        assert scan.call_count == 2


def test_cache_tracks_additions_deletions_and_manifest(tmp_path):
    from g_brain import condense
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    with patch.object(condense, "MANIFEST", manifest):
        assert condense.read_report(tmp_path)["notes_scanned"] == 0
        md = _write_note(tmp_path, "ideas/fixture", body="retired-fixture.example")
        assert condense.read_report(tmp_path)["notes_scanned"] == 1
        manifest.write_text(json.dumps({"retired_terms": [
            {"term": "retired-fixture.example", "reason": "fixture retired"}]}))
        assert condense.read_report(tmp_path)["counts"]["stale"] == 1
        md.unlink()
        assert condense.read_report(tmp_path)["notes_scanned"] == 0


def test_detector_and_cache_stay_in_kept_layers(tmp_path):
    from g_brain import condense, reader
    for layer in condense.CORPUS_LAYERS:
        _write_note(tmp_path, f"{layer}/fixture", kind="about")
    excluded = ("nerves", "todos", "tickets", "indexes", "lineage", "logs", "sources",
                "designs/.history", "designs/versions", "ideas/archive")
    for layer in excluded:
        _write_note(tmp_path, f"{layer}/fixture")
    with patch.object(reader, "all_notes", side_effect=AssertionError("must not read other layers")):
        first = condense.read_report(tmp_path)
        assert first["notes_scanned"] == 6
        assert len(first["orphans"]) == 6
        _write_note(tmp_path, "tickets/another")
        assert condense.read_report(tmp_path) is first
    assert not (tmp_path / "status").exists()


def test_orphans_resolve_links_inside_kept_layers(tmp_path):
    from g_brain import condense
    _write_note(tmp_path, "ideas/fixture", body="See [[contract]].")
    _write_note(tmp_path, "designs/contract/current", kind="design")
    _write_note(tmp_path, "hazards/alone", kind="hazard")
    report = condense.scan(tmp_path)
    assert [n["path"] for n in report["orphans"]] == ["hazards/alone"]
