"""Voice web service: Telnyx WhatsApp webhook.

  GET  /webhook  liveness (Telnyx has no GET handshake; returns ok)
  POST /webhook  inbound messages (Ed25519-verified, sender-allowlisted)
  GET  /health   readiness + which creds are still missing
  POST /talk     spoken turn, NDJSON stream of sentences (localhost, talk page)
  POST /tts      one sentence to WAV via the warm Kokoro service (localhost)

Run: uvicorn voice.app:app --host 127.0.0.1 --port 8077  (from the repo root)
"""
import json
import logging
import os
import sys

import httpx

import brain_alerts
from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import agent, config, talk, whatsapp
from .health import deep_health

log = logging.getLogger("voice")

# uvicorn configures only its own loggers; the root logger keeps the default
# WARNING level and no handler, so every log.info in this package went nowhere
# in production while looking fine under a test harness that called
# basicConfig(level=INFO). That silently cost us the inbound and tool-call
# lines -- the exact records added to make a no-op turn visible. Attach our own
# handler so INFO reaches the journal.
if not log.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(levelname)s:     voice %(message)s"))
    log.addHandler(_handler)
    log.setLevel(logging.INFO)
    log.propagate = False
app = FastAPI(title="brain voice", docs_url=None, redoc_url=None)


class ChatIn(BaseModel):
    message: str
    session: str = "web"


@app.get("/health")
def health(response: Response):
    # sync def -> FastAPI runs it in a threadpool, so the blocking dependency
    # probes (OpenRouter, Telnyx) don't stall the event loop.
    #
    # The status code must track `ok`. g-core's health contract is "health_url
    # set -> HTTP GET, expect 2xx", and brain_status._check_http maps 2xx to
    # "up", so returning 200 with {"ok": false} meant this endpoint computed a
    # full dependency check and then told the alert spine everything was fine.
    # The Telnyx balance sat below its own threshold, unreported, while the bot
    # was one payment away from going mute.
    body = deep_health()
    if not body.get("ok"):
        response.status_code = 503
    return body


@app.get("/webhook")
async def webhook_alive():
    return {"ok": True}


@app.post("/chat")
async def chat(body: ChatIn):
    """Internal text transport (localhost-only, nginx exposes only /webhook).
    gcommand calls this server-side behind its admin gate."""
    try:
        reply = agent.handle(body.session, body.message)
    except Exception as e:
        log.exception("chat failed")
        reply = f"(error: {type(e).__name__}: {e})"
    return {"reply": reply}


class TalkIn(BaseModel):
    message: str
    session: str = "talk"


@app.post("/talk")
def talk_turn(body: TalkIn):
    """Spoken turn for the talk page: one JSON object per line, a sentence as
    soon as it closes. Localhost only, like /chat; gcommand gates it."""
    def lines():
        try:
            for ev in talk.stream_turn(body.session, body.message):
                yield json.dumps(ev) + "\n"
        except Exception as e:
            log.exception("talk failed")
            yield json.dumps({"t": "error", "error": f"{type(e).__name__}: {e}"}) + "\n"
    return StreamingResponse(lines(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


TTS_URL = os.environ.get("VOICE_TTS_URL", "http://127.0.0.1:8078")


class TtsIn(BaseModel):
    text: str
    voice: str | None = None


@app.post("/tts")
async def tts(body: TtsIn):
    """One sentence to WAV through voice-tts.service (warm Kokoro on CPU)."""
    payload = {"text": body.text[:400]}
    if body.voice:
        payload["voice"] = body.voice
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{TTS_URL}/tts", json=payload)
    except httpx.HTTPError as e:
        return Response(status_code=502, content=f"tts unreachable: {type(e).__name__}")
    return Response(content=r.content, status_code=r.status_code,
                    media_type=r.headers.get("content-type", "audio/wav"),
                    headers={"X-Synth-Ms": r.headers.get("x-synth-ms", "")})


# A reply that never goes out looks like silence on the phone, so it pages
# through ntfy, which does not depend on Telnyx. Once per cooldown while it
# stays broken; the next delivered reply clears it.
_SEND_ALERT = "voice-send"


def _send_failed(why: str):
    brain_alerts.send("error", "voice", f"WhatsApp reply not sent: {why}", dedup_key=_SEND_ALERT)


def _telnyx_error(body) -> str:
    try:
        e = json.loads(body)["errors"][0]
        return f"{e.get('code')} {e.get('title')}: {e.get('detail')}"
    except Exception:
        return str(body)[:200]


def _process(sender: str, text: str, media: list, owner: bool = True):
    """Runs after we've already ACKed Telnyx: does the slow LLM work, then replies."""
    try:
        images = []
        if owner and media:
            for m in media:
                result = whatsapp.fetch_media(m["url"])
                if result is not None:
                    images.append(result)
        reply = agent.handle(sender, text, owner=owner, images=images or None)
        result = whatsapp.send_text(sender, reply)
        log.warning("send -> %s: status=%s body=%s", sender, result.get("status"), str(result.get("body"))[:400])
        if 200 <= result.get("status", 0) < 300:
            brain_alerts.clear(_SEND_ALERT)
        else:
            _send_failed(f"Telnyx {result.get('status')}, {_telnyx_error(result.get('body'))}")
            if owner and config.NTFY_FALLBACK:
                brain_alerts.send("info", "voice reply", whatsapp.format_for_whatsapp(reply)[:3500])
    except Exception as e:
        log.exception("voice processing failed for %s", sender)
        _send_failed(f"{type(e).__name__}: {e}")


@app.post("/webhook")
async def inbound(request: Request, bg: BackgroundTasks):
    raw = await request.body()
    sig = request.headers.get("telnyx-signature-ed25519", "")
    ts = request.headers.get("telnyx-timestamp", "")
    if not whatsapp.verify_signature(raw, sig, ts):
        return Response(status_code=403)

    payload = await request.json()
    matched = False
    for sender, text, media in whatsapp.parse_inbound(payload):
        matched = True
        owner = sender.lstrip("+") in config.ALLOWED_NUMBERS
        if not owner and not config.GUESTS_OK:
            log.warning("ignored message from non-allowlisted sender %s", sender)
            continue
        log.info("inbound sender=%s text_len=%d media_count=%d owner=%s",
                 sender, len(text or ""), len(media), owner)
        bg.add_task(_process, sender, text, media, owner)

    if not matched:
        data = payload.get("data", {})
        if data.get("event_type") == "message.received":
            # Received a message.received we couldn't extract sender/content from.
            safe = str(payload)[:500]
            log.info("message.received: unrecognised payload shape: %s", safe)

    # Always 200 fast so Telnyx doesn't retry.
    return Response(status_code=200)
