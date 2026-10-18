"""Writer contract; every record stays inside a temporary vault."""
import datetime as dt

import frontmatter
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def todos(tmp_path, monkeypatch):
    import brain_todos
    monkeypatch.setattr(brain_todos, "TODOS_DIR", tmp_path / "todos")
    return brain_todos


def test_normalization_and_provenance(todos):
    item = todos.create(r'Book\n the\t dentist"', "P2", body=r'Bring\r\nID\tand \"card\"',
                        requester="gabriel", composer="agent", source="mcp")
    assert item["title"] == "Book the dentist"
    assert item["body"].strip() == 'Bring\nID\tand "card"'
    assert item["provenance"] == {"requester": "gabriel", "composer": "agent", "source": "mcp"}
    assert todos.get(item["slug"]).metadata["provenance"] == item["provenance"]


@pytest.mark.parametrize("title,body,requester", [
    ("Dentist appointment", "", "gabriel"),
    ("Dentist appointment", "", "pulse.gmail.distil"),
    ("Birthday trip to the coast", "", "pulse.page.compact"),
    ("Obtain a group retreat quote and arrange the activity", "", "pulse.page.compact"),
    ("Pay the water bill payment plan", "", "pulse.gmail.distil"),
    ("Reply to Morgan about the job role", "", "pulse.gmail.distil"),
    ("Migrate legacy API keys on the provider dashboard", "", "pulse.gmail.distil"),
    ("Update the billing account on the provider dashboard", "", "pulse.gmail.distil"),
    ("Refactor the sample-repo auth module", "", "gabriel"),
    ("Finish Sample renderer integration", "Captured from the pulse page", "gabriel"),
])
def test_owner_action_accepts(todos, title, body, requester):
    assert todos.create(title, "P1", body=body, requester=requester,
                        start="2027-06-04T09:00:00", end="2027-06-06T18:00:00")["status"] == "open"


@pytest.mark.parametrize("title,body,requester,route", [
    ("Fix deploy workflow failure in sample-repo", "", "gabriel", "brain_alert"),
    ("Dependabot alert: vulnerable library", "", "gabriel", "brain_alert"),
    ("Build failed in sample-repo", "", "agent", "brain_alert"),
    ("Service down: sample", "", "pulse.gmail.distil", "brain_alert"),
    ("CVE-2099-12345 advisory", "", "agent", "brain_alert"),
    ("Secret-scan advisory", "", "agent", "brain_alert"),
    ("Cron failed: sample", "", "agent", "brain_alert"),
    ("Finish Sample renderer integration", "Captured from the pulse page", "pulse.page.compact", "create_ticket"),
    ("Finish Sample renderer integration", "", "unknown", "create_ticket"),
    ("Refactor the sample-repo auth module", "", "agent", "create_ticket"),
    ("Fix a bug in the sample repo", "", "agent", "create_ticket"),
    ("Review changes", "Codex should implement this", "agent", "create_ticket"),
    ("Review sample", "Branch sample-feature, commit abc123, PR #23", "agent", "create_ticket"),
])
def test_owner_action_rejects(todos, title, body, requester, route):
    with pytest.raises(ValueError, match=route):
        todos.create(title, "P2", body=body, requester=requester)
    assert list(todos.iter_todos()) == []


def test_idempotency_includes_closed_and_writes_nothing(todos):
    first = todos.create("Pay the water bill", "P1", requester="pulse.gmail.distil", source_key="mail:fake#1")
    todos.mark_done(first["slug"])
    path = todos.TODOS_DIR / (first["slug"] + ".md")
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    result = todos.create("Pay water", "P1", source_key="mail:fake#1")
    assert result["deduped"] is True and result["status"] == "done"
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
    assert len(list(todos.iter_todos())) == 1


def test_hygiene(todos):
    with pytest.raises(ValueError, match="one short line"):
        todos.create("Book appointment", "P1", body="a" * 801, requester="agent")
    assert todos.create("Book appointment", "P1", body="a" * 801, requester="gabriel")


def test_rest_and_mcp_contract(todos):
    app = FastAPI()
    app.include_router(todos.create_router(), prefix="/api")
    client = TestClient(app)
    item = client.post("/api/todos", json={"title": "Book appointment", "priority": "P1"}).json()
    assert item["provenance"] == dict.fromkeys(("requester", "composer", "source"), "unknown")
    assert client.post("/api/todos", json={"title": "Refactor sample auth", "priority": "P1"}).status_code == 400
    payload = {"title": r"Pay\nwater", "priority": "P1", "requester": "pulse.gmail.distil",
               "composer": "pulse.gmail.distil", "source": "gmail:<fake@example.invalid>",
               "source_key": "gmail:fake#1"}
    made = client.post("/api/todos", json=payload).json()
    assert made["title"] == "Pay water"
    assert client.post("/api/todos", json=payload).json()["deduped"] is True
    with pytest.raises(TypeError):
        todos.create_todo("Book appointment", "P1")
    assert todos.create_todo("Book appointment", "P1", requester="gabriel")["provenance"]["source"] == "mcp"
    assert client.patch("/api/todos/" + item["slug"], json={"title": r'Call\nclinic"'}).json()["title"] == "Call clinic"


def test_expiry_reads_without_mutation(todos, monkeypatch):
    from g_pulse import pulse
    today = dt.date.today()
    old = (today - dt.timedelta(days=2)).isoformat()
    item = todos.create("Book optician", "P2", due=old, expires=old)
    path = todos.TODOS_DIR / (item["slug"] + ".md")
    before = path.read_bytes()
    assert todos.serialize(item["slug"], todos.get(item["slug"]))["status"] == "expired"
    assert todos.list_todos() == []
    assert len(todos.list_todos(status="expired")) == 1
    assert todos.occurrences(old, today.isoformat(), "live") == []
    assert pulse._todos_soon() == []
    assert path.read_bytes() == before
    monkeypatch.setattr("sys.argv", ["brain_todos.py", "--dry-run"])
    assert todos.main() == 0
    assert path.read_bytes() == before


def test_legacy_expiry_and_priority(todos):
    old = (dt.date.today() - dt.timedelta(days=90)).isoformat()
    for slug, priority, rrule in [("old", "P2", None), ("vital", "P0", None), ("habit", "P2", "FREQ=DAILY")]:
        todos._save(slug, frontmatter.Post("", title=slug, priority=priority, created=old, status="open", rrule=rrule))
    assert todos.serialize("old", todos.get("old"))["status"] == "expired"
    assert {t["slug"] for t in todos.list_todos()} == {"vital", "habit"}


def test_upsert_uses_boundary(todos):
    provenance = {"requester": "agent", "composer": "agent", "source": "test"}
    with pytest.raises(ValueError, match="create_ticket"):
        todos.upsert("sample", {"title": "Refactor sample auth"}, "", provenance)
    assert todos.upsert("sample", {"title": r'Book\nclinic"'}, r'Bring\nID', provenance)["title"] == "Book clinic"
    assert todos.get("sample").content == "Bring\nID"


def test_voice_records_owner_and_event(todos):
    from voice import tools
    item = tools.create_todo("Call the clinic", turn_id="fake-turn")
    assert item["provenance"] == {"requester": "gabriel", "composer": "agent", "source": "voice:fake-turn"}
    other = tools.create_todo("Book appointment")
    assert other["provenance"]["source"].startswith("voice:")
    with pytest.raises(ValueError, match="brain_alert"):
        tools.create_todo("Fix deploy workflow failure in sample-repo")


def test_expiry_today_and_all_read_surfaces(todos):
    day = dt.date.today().isoformat()
    item = todos.create("Visit the optician", "P2", due=day, expires=day)
    assert item["status"] == "open"
    assert len(todos.occurrences(day, day, "live")) == 1
    app = FastAPI()
    app.include_router(todos.create_router())
    client = TestClient(app)
    old = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    expired = todos.create("Collect glasses", "P2", due=old, expires=old)
    slug = expired["slug"]
    assert client.get("/todos/" + slug).json()["status"] == "expired"
    assert slug not in {t["slug"] for t in client.get("/todos").json()}
    assert slug in {t["slug"] for t in client.get("/todos?status=expired").json()}
    assert slug not in {t["slug"] for t in client.get("/todos/calendar").json()}


def test_patches_cannot_bypass_owner_boundary(todos):
    item = todos.create("Book appointment", "P1")
    with pytest.raises(ValueError, match="create_ticket"):
        todos._update(item["slug"], {"title": "Refactor sample auth"})
    assert todos.get(item["slug"])["title"] == "Book appointment"


def test_source_key_cannot_relocate_an_existing_todo(todos):
    item = todos.create("Pay bill", "P1", source_key="fake:bill")
    result = todos.upsert("new-slug", {"title": "Pay bill again"}, "", {}, source_key="fake:bill")
    assert result["deduped"] and result["slug"] == item["slug"]
    assert not (todos.TODOS_DIR / "new-slug.md").exists()


def test_legacy_sweep_cannot_write_outside_boundary(todos, monkeypatch, capsys):
    import json
    old = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    item = todos.create("Collect parcel", "P2", expires=old)
    path = todos.TODOS_DIR / (item["slug"] + ".md")
    before = path.read_bytes()
    report = todos.expire_stale(apply=True)
    assert report["retired"] == 1 and report["applied"] is False
    monkeypatch.setattr("sys.argv", ["brain_todos.py"])
    assert todos.main() == 0
    assert json.loads(capsys.readouterr().out)["retired"] == 1
    assert path.read_bytes() == before


def test_legacy_todo_stays_closable(todos, tmp_path):
    # A stored todo with no provenance and a long body predates the boundary;
    # closing it must not be refused by checks meant for new wording.
    path = todos._path_for("legacy-long")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("---\ntitle: Sort out the billing account\nkind: todo\n"
                    "permalink: brain/todos/legacy-long\nstatus: open\npriority: P1\n"
                    "created: '2026-09-01'\n---\n\n" + "detail " * 200 + "\n")
    assert todos._update("legacy-long", {"status": "done"})["status"] == "done"


@pytest.mark.parametrize("title, body, requester", [
    ("Finish home setup: energy, broadband, TV", "", "unknown"),
    ("Birthday trip, 14-20 November", "Captured from the pulse page on 2026-10-01.", "pulse.page.compact"),
    ("Claude plan £18.00", "Monthly charge.", "inlets.subs"),
    ("Reply to a client about the PR and brand audit", "", "pulse.gmail.distil"),
])
def test_real_shaped_owner_actions_pass(todos, title, body, requester):
    assert todos.create(title, body=body, requester=requester)["title"]
