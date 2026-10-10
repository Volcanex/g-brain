"""condense's stale check takes its retired terms from the instance manifest."""
import json
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
