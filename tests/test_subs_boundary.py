"""The subscription inlet reconciles records through the public todo boundary."""
import pytest


@pytest.fixture
def inlet(tmp_path, monkeypatch):
    import brain_todos
    from inlets import subs
    monkeypatch.setattr(brain_todos, "TODOS_DIR", tmp_path / "todos")
    rows = [{"id": "sample", "label": "Example Vendor", "amount": 9.99, "lapsed": False,
             "status": "active", "seen": True, "next_due": "2027-07-03",
             "last_charged": "2027-06-03", "charges": 3, "cycle": "monthly"}]
    monkeypatch.setattr(subs, "_fetch", lambda: rows)
    return subs, brain_todos, rows


def test_subscriptions_reconcile_provenance_and_lapse(inlet):
    subs, todos, rows = inlet
    assert subs.run() == {"created": 1}
    item = todos.list_todos()[0]
    assert item["title"] == "Example Vendor £9.99" and item["category"] == "finance"
    assert item["rrule"] == "FREQ=MONTHLY"
    assert item["source_key"] == "gfin-sub:sample"
    assert item["provenance"] == {"requester": "inlets.subs", "composer": "inlets.subs", "source": "gfin-sub:sample"}
    rows[0]["next_due"] = "2027-08-03"
    assert subs.run() == {"updated": 1}
    assert todos.list_todos()[0]["start"] == "2027-08-03T09:00:00"
    rows[0]["lapsed"] = True
    assert subs.run() == {"lapsed": 1}
    assert todos.list_todos() == []
    rows[0]["lapsed"] = False
    assert subs.run() == {"reopened": 1}
    assert len(todos.list_todos()) == 1


@pytest.mark.parametrize("status", ["done", "cancelled", "failed", "blocked"])
def test_subscriptions_never_reopen_manual_closures(inlet, status):
    subs, todos, rows = inlet
    subs.run()
    todos._update("sub-sample", {"status": status})
    path = todos.TODOS_DIR / "sub-sample.md"
    before = path.read_bytes()
    assert subs.run() == {"skipped-closed": 1}
    rows[0]["lapsed"] = True
    subs.run()
    assert path.read_bytes() == before


def test_cancel_subscription_remains_p1_and_nonrecurring(inlet):
    subs, todos, rows = inlet
    rows[0]["status"] = "cancel"
    subs.run()
    item = todos.list_todos()[0]
    assert item["title"] == "Cancel Example Vendor (£9.99 a month)"
    assert item["priority"] == "P1" and item["rrule"] is None
    assert item["expires"] is None


def test_unseen_subscription_has_no_todo(inlet):
    subs, todos, rows = inlet
    rows[0].update(seen=False, next_due=None)
    assert subs.run() == {"no-date": 1}
    assert todos.list_todos() == []
