"""Facts are read directly from about/, never through the corpus component."""
import frontmatter
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def pulse(tmp_path, monkeypatch):
    from g_pulse import pulse
    monkeypatch.setattr(pulse, "BRAIN_ROOT", tmp_path)
    monkeypatch.setattr(pulse, "PAGE_PATH", tmp_path / "page.md")
    monkeypatch.setattr(pulse, "BRIEFS_DIR", tmp_path / "briefs")
    monkeypatch.setattr(pulse, "STATUS_SNAPSHOT", tmp_path / "status.json")
    return pulse


def test_about_missing_and_empty_produce_no_block(pulse):
    assert "About Gabriel" not in pulse.pulse_brief()["body"]
    (pulse.BRAIN_ROOT / "about").mkdir()
    assert "About Gabriel" not in pulse.composed_payload()


def test_about_in_brief_composed_and_rest(pulse):
    about = pulse.BRAIN_ROOT / "about"
    about.mkdir()
    (about / "preference.md").write_text(frontmatter.dumps(frontmatter.Post(
        "Private body should not be injected.", title="Preferred rhythm", kind="fact",
        summary="Keep the morning\nclear for focused work.")))
    pulse.BRIEFS_DIR.mkdir()
    (pulse.BRIEFS_DIR / "2027-06-03.md").write_text("A quiet day.")
    expected = "- Preferred rhythm: Keep the morning clear for focused work."
    assert expected in pulse.pulse_brief()["body"]
    assert expected in pulse.composed_payload()
    assert "Private body" not in pulse.composed_payload()
    assert pulse.pulse_brief()["date"] == "2027-06-03"
    app = FastAPI()
    app.include_router(pulse.create_router())
    assert expected in TestClient(app).get("/pulse/brief").json()["body"]


def test_about_bounded_and_filters_nonfacts(pulse):
    about = pulse.BRAIN_ROOT / "about"
    about.mkdir()
    (about / "000-other.md").write_text(frontmatter.dumps(frontmatter.Post("", title="Skip", kind="finding", summary="Not an about fact")))
    for n in range(45):
        (about / f"fact-{n:02}.md").write_text(frontmatter.dumps(frontmatter.Post("", title=f"Fact {n}", kind="fact", summary="A brief fact")))
    block = pulse._about_block()
    assert len(block.splitlines()) == 41
    assert "Skip" not in block and "Fact 44:" not in block


def test_about_keeps_room_for_other_payload_sections(pulse):
    about = pulse.BRAIN_ROOT / "about"
    about.mkdir()
    for n in range(40):
        (about / f"fact-{n:02}.md").write_text(frontmatter.dumps(frontmatter.Post("", title=f"Fact {n}", kind="fact", summary="a" * 500)))
    assert len(pulse._about_block()) < 1700
    assert "No daily brief yet" in pulse.composed_payload()


def test_malformed_about_frontmatter_does_not_break_brief(pulse):
    about = pulse.BRAIN_ROOT / "about"
    about.mkdir()
    (about / "a-broken.md").write_text("---\nkind: fact\nsummary: [unclosed\n---\n")
    (about / "b-good.md").write_text(frontmatter.dumps(frontmatter.Post(
        "", title="Good fact", kind="fact", summary="Prefers a morning walk.")))
    assert "- Good fact: Prefers a morning walk." in pulse.composed_payload()
    assert "- Good fact: Prefers a morning walk." in pulse.pulse_brief()["body"]


def _long_page(pulse, n):
    pulse.PAGE_PATH.write_text(pulse._PAGE_HEADER + "\n" + "".join(
        f"- Snippet {i:02} about something current, padded to look like a real line of context.\n"
        for i in range(n)))


def test_long_page_keeps_dated_todos_health_and_brief_age(pulse, monkeypatch):
    import datetime as dt
    import json
    about = pulse.BRAIN_ROOT / "about"
    about.mkdir()
    for n in range(12):
        (about / f"fact-{n:02}.md").write_text(frontmatter.dumps(frontmatter.Post("", title=f"Fact {n}", kind="fact", summary="b" * 120)))
    _long_page(pulse, 60)
    monkeypatch.setattr(pulse, "_todos_soon", lambda days=7: ["- 2027-06-04 Pay the water bill"])
    pulse.STATUS_SNAPSHOT.write_text(json.dumps({"checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                                                 "summary": {"up": 3, "total": 3}, "services": []}))
    text = pulse.composed_payload()
    assert len(text) <= pulse.PAYLOAD_CHAR_BUDGET
    assert "Dated, next 7 days:\n- 2027-06-04 Pay the water bill" in text
    assert "Services: 3/3 up." in text and "No daily brief yet" in text
    assert "- Snippet 59 " in text and "- Snippet 00 " not in text
    assert "older snippets not shown: the page needs compaction]" in text
    assert "About Gabriel" in text and "payload truncated" not in text


def test_full_page_returns_every_snippet_and_short_pages_are_untouched(pulse):
    _long_page(pulse, 60)
    full = pulse.pulse_page(full=True)
    assert all(f"- Snippet {i:02} " in full for i in range(60)) and "not shown" not in full
    _long_page(pulse, 3)
    text = pulse.composed_payload()
    assert "<!-- pulse page" in text and "- Snippet 02 " in text and "not shown" not in text
