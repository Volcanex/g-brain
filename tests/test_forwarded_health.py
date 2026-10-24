"""A dependency read through a service's /health keeps the catalog's warn line."""
import pytest

DEP = {"id": "telnyx", "criticality": "vital", "health": {"service": "voice", "check": "telnyx_balance"},
       "probe": {"type": "balance", "min": 5.0}}


@pytest.mark.parametrize("check,state", [
    ({"ok": True, "balance": 12.5, "detail": "$12.50"}, "up"),
    ({"ok": True, "balance": 3.67, "detail": "$3.67"}, "warn"),
    ({"ok": False, "balance": 1.2, "detail": "$1.20"}, "down"),
    ({"ok": True, "detail": "no balance in this check"}, "up"),
    ({"ok": True, "balance": True, "detail": "not a number"}, "up"),
])
def test_forwarded_balance_answers_to_the_manifest_floor(check, state):
    from g_spine import status
    got, detail = status._check_via_health(DEP, {"voice": {"telnyx_balance": check}})
    assert got == state
    assert detail.endswith("(via voice/health)")
    if state == "warn":
        assert detail.startswith("balance 3.67 below 5.00")


def test_without_a_probe_floor_the_service_verdict_stands():
    from g_spine import status
    dep = {"id": "r2", "health": {"service": "photo-sync-server", "check": "r2"}}
    assert status._check_via_health(dep, {"photo-sync-server": {"r2": {"ok": True, "balance": 0.1}}})[0] == "up"
    assert status._check_via_health(dep, {"photo-sync-server": None}) is None
