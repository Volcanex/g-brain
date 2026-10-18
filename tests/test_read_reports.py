"""Report requests resolve reviews and detect condensation without scheduled jobs."""
import copy
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import frontmatter
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def reports(tmp_path, monkeypatch):
    from g_brain import api, commit, condense, index, reader, verify
    from g_spine import alerts
    for module in (api, commit, condense, index, reader, verify):
        monkeypatch.setattr(module, "BRAIN_ROOT", tmp_path)
    reader._all_notes_cached.cache_clear()
    monkeypatch.setattr(commit, "COMMITS_LOG", tmp_path / "commits.log")
    monkeypatch.setattr(index, "INDEXES_DIR", tmp_path / "indexes")
    monkeypatch.setattr(verify, "_today", lambda: dt.date(2026, 10, 18))
    send = Mock()
    monkeypatch.setattr(alerts, "send", send)
    app = FastAPI()
    app.mount("/api", api.create_api())
    with TestClient(app) as client:
        yield client, commit, verify, send
    reader._all_notes_cached.cache_clear()


def _flag(dependency="designs/upstream", since="2026-09-01"):
    return {"note": dependency, "since": since, "stamp": "fixture-revision",
            "reason": "Fixture dependency rewritten"}


def _review(flag):
    return {"note": flag["note"], "stamp": flag["stamp"], "actor": "agent",
            "reason": "Read both fixture contracts; they remain compatible.",
            "reviewed_at": "2026-10-18T12:00:00"}


def _write_note(root, flags, reviews=()):
    md = root / "ideas/transport.md"
    md.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post("The fixture transport uses polling.", title="Fixture transport",
                            kind="idea", summary="Fixture transport contract", version="1.0.0",
                            stale_dep=flags, dependency_reviews=list(reviews))
    md.write_text(frontmatter.dumps(post) + "\n")
    return md


def test_stale_request_clears_reviewed_flag_and_keeps_unreviewed(reports, tmp_path):
    client, commit, _, send = reports
    flags = [_flag(), _flag("designs/other")]
    md = _write_note(tmp_path, flags)
    assert client.get("/api/stale").json()["outstanding"] == 2
    review = commit.review_dependency("ideas/transport", flags[0]["note"], flags[0]["stamp"],
                                      "Read both contracts; transport remains compatible.")
    assert review["ok"], review
    before = frontmatter.load(md)
    assert len(before["stale_dep"]) == 2  # the write records evidence only
    response = client.get("/api/stale")
    assert response.status_code == 200
    report = response.json()
    assert report["cleared"] == 1
    assert report["outstanding"] == report["escalating"] == 1
    assert report["outstanding_detail"][0]["depends_on"] == "designs/other"
    after = frontmatter.load(md)
    assert after["stale_dep"] == [flags[1]]
    assert after["version"] == before["version"]
    assert after.content == before.content
    assert after["dependency_reviews"] == before["dependency_reviews"]
    assert client.get("/api/stale").json()["cleared"] == 0
    assert not (tmp_path / "status/stale.json").exists()
    send.assert_not_called()


def test_orientation_resolves_reviews_and_surfaces_oldest_flags(reports, tmp_path):
    client, _, _, send = reports
    flags = [_flag(f"designs/upstream-{i}", since=f"2026-09-0{i + 1}") for i in range(6)]
    md = _write_note(tmp_path, flags, [_review(flags[-1])])
    response = client.get("/api/orient", params={"project": "transport"})
    assert response.status_code == 200
    assert "5 flag(s) aged 14+ days" in response.text
    assert "oldest:" in response.text
    assert "ideas/transport' (47d)" in response.text
    assert "1 notes by layer: designs 0, ideas 1, about 0, hazards 0" in response.text
    assert len(response.text) <= 6000
    assert len(frontmatter.load(md)["stale_dep"]) == 5
    report = client.get("/api/stale").json()
    assert [n["age_days"] for n in report["oldest_detail"]] == [47, 46, 45, 44, 43]
    send.assert_not_called()


def test_whole_batch_clear_warns_once_with_spine_dedup(reports, tmp_path):
    client, _, _, send = reports
    flags = [_flag(f"designs/upstream-{i}") for i in range(5)]
    _write_note(tmp_path, flags, [_review(flag) for flag in flags])
    first = client.get("/api/stale").json()
    assert first["suspicious_full_clear"]
    assert first["cleared"] == 5
    assert not client.get("/api/stale").json()["suspicious_full_clear"]
    send.assert_called_once()
    assert send.call_args.args[:2] == ("warn", "brain-verify")
    assert send.call_args.kwargs["dedup_key"] == "brain-verify-full-clear"


def test_one_note_resolution_is_pure_and_rejects_old_or_unattended_reviews(reports):
    _, _, verify, _ = reports
    flags = [_flag("designs/exact"), _flag("designs/old"), _flag("designs/auto")]
    reviews = [_review(f) for f in flags]
    reviews[1]["stamp"] = "earlier-revision"
    reviews[2]["actor"] = "ai-auto"
    metadata = {"stale_dep": flags, "dependency_reviews": reviews}
    before = copy.deepcopy(metadata)
    result = verify.resolve_note("ideas/transport", metadata, dt.date(2026, 10, 18))
    assert metadata == before
    assert result["keep"] == flags[1:]
    assert len(result["cleared"]) == 1
    assert len(result["outstanding"]) == 2


def test_failed_clear_stays_outstanding(reports, tmp_path, monkeypatch):
    client, _, _, send = reports
    flag = _flag()
    md = _write_note(tmp_path, [flag], [_review(flag)])
    write_text = Path.write_text

    def fail_note_write(path, *args, **kwargs):
        if path == md:
            raise OSError("fixture write failure")
        return write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_note_write)
    report = client.get("/api/stale").json()
    assert report["cleared"] == 0
    assert report["outstanding"] == 1
    assert "fixture write failure" in report["errors"][0]
    assert frontmatter.load(md)["stale_dep"] == [flag]
    assert send.call_args.kwargs["dedup_key"] == "brain-verify-errors"


def test_condense_request_ignores_old_file_and_tracks_note_edits(reports, tmp_path):
    client, _, _, _ = reports
    md = _write_note(tmp_path, [])
    status = tmp_path / "status/condense.json"
    status.parent.mkdir()
    status.write_text('{"backlog": 9999}')
    report = client.get("/api/condense").json()
    assert report["notes_scanned"] == 1
    assert report["backlog"] != 9999
    assert report["counts"]["drift"] == 0
    post = frontmatter.load(md)
    post.metadata.pop("summary")
    md.write_text(frontmatter.dumps(post))
    assert client.get("/api/condense").json()["counts"]["drift"] == 1
    assert json.loads(status.read_text()) == {"backlog": 9999}


@pytest.mark.parametrize("script", ["brain_verify.py", "brain_condense.py"])
def test_manual_aliases_still_write_reports(script, tmp_path):
    flag = _flag()
    md = _write_note(tmp_path, [flag], [_review(flag)])
    env = {**os.environ, "BRAIN_ROOT": str(tmp_path)}
    command = [sys.executable, script]
    if script == "brain_condense.py":
        command.append("-v")
    result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], env=env,
                            text=True, capture_output=True, check=True)
    assert '"checked_at"' in result.stdout
    report_path = tmp_path / "status" / script.replace("brain_", "").replace(".py", ".json")
    if script == "brain_verify.py":
        report_path = tmp_path / "status/stale.json"
        assert "stale_dep" not in frontmatter.load(md)
    assert json.loads(report_path.read_text())["checked_at"]
