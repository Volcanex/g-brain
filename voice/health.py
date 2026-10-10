"""Deep health check for the Voice + outlet chain. The watchdog polls this and
alerts when anything goes red; gcommand /system can also surface it. Each check
is wrapped so one failure can't take down the endpoint."""
import os
import time

import httpx

import brain_todos
import g_config

from . import config

# The reminder outlet, not the calendar one: gcal was retired 2026-09-23 and
# its state file froze, which would have reported this service degraded for a
# sync that is never going to happen again. A health check pointed at a dead
# dependency is worse than no check, because it teaches you to ignore the red.
_OUTLET_STATE = os.path.join(g_config.STATE_DIR, "reminders.json")
TELNYX_MIN_BALANCE = 2.0   # USD, warn before WhatsApp sends start failing
OUTLET_MAX_AGE = 1800      # seconds, outlet-remind.timer runs every 5 min


def _engine():
    if not config.OPENROUTER_API_KEY:
        return {"ok": False, "detail": "no OpenRouter key"}
    try:
        r = httpx.get("https://openrouter.ai/api/v1/key",
                      headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"}, timeout=6)
        return {"ok": r.status_code == 200, "detail": f"openrouter http {r.status_code}"}
    except Exception as e:
        return {"ok": False, "detail": f"openrouter {type(e).__name__}"}


def _telnyx_balance():
    if not config.TELNYX_API_KEY:
        return {"ok": False, "detail": "no Telnyx key"}
    try:
        r = httpx.get(f"{config.TELNYX_API}/balance",
                      headers={"Authorization": f"Bearer {config.TELNYX_API_KEY}"}, timeout=6)
        bal = float(r.json()["data"]["balance"])
        return {"ok": bal >= TELNYX_MIN_BALANCE, "balance": bal, "detail": f"${bal:.2f}"}
    except Exception as e:
        return {"ok": False, "detail": f"telnyx {type(e).__name__}"}


def _brain():
    try:
        n = len(brain_todos.list_todos(status="open"))
        return {"ok": True, "detail": f"{n} open todos"}
    except Exception as e:
        return {"ok": False, "detail": f"brain {type(e).__name__}"}


def _outlet():
    try:
        age = time.time() - os.path.getmtime(_OUTLET_STATE)
        return {"ok": age < OUTLET_MAX_AGE, "detail": f"reminders ran {int(age)}s ago"}
    except Exception:
        return {"ok": False, "detail": "no reminder state"}


def deep_health() -> dict:
    checks = {
        "engine": _engine(),
        "telnyx_balance": _telnyx_balance(),
        "brain": _brain(),
        "outlet": _outlet(),
        "transport_creds": {"ok": not config.missing(config.TRANSPORT_KEYS),
                            "missing": config.missing(config.TRANSPORT_KEYS)},
    }
    return {"ok": all(c["ok"] for c in checks.values()), "model": config.MODEL, "checks": checks}
