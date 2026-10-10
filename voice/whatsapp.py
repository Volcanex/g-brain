"""WhatsApp transport via Telnyx (Telnyx is the BSP).

Send through Telnyx's WhatsApp API; verify inbound via Telnyx's Ed25519 webhook
signature (telnyx-signature-ed25519 + telnyx-timestamp, signed over
'{timestamp}|{raw_body}'). Public key from Mission Control → Keys & Credentials.
"""
import base64
import logging
import re
import time
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import config

log = logging.getLogger("voice")

_MEDIA_SIZE_CAP = 5 * 1024 * 1024  # 5 MB
_ACCEPTED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

_TOLERANCE = 600  # seconds, reject stale webhooks (replay guard)


def verify_signature(raw_body: bytes, signature_b64: str, timestamp: str) -> bool:
    """Verify a Telnyx webhook. Fail closed on anything unexpected."""
    if not (config.TELNYX_PUBLIC_KEY and signature_b64 and timestamp):
        return False
    try:
        if abs(time.time() - int(timestamp)) > _TOLERANCE:
            return False
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(config.TELNYX_PUBLIC_KEY))
        signed = f"{timestamp}|".encode() + raw_body
        key.verify(base64.b64decode(signature_b64), signed)
        return True
    except Exception:
        return False


def format_for_whatsapp(text: str) -> str:
    """Normalize model output to what WhatsApp actually renders: em/en dashes to
    hyphens, markdown bold ** ** to WhatsApp's single-* bold, strip # headers."""
    text = text.replace("—", "-").replace("–", "-")
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)
    text = re.sub(r"(?m)^\s*#{1,6}\s*", "", text)
    return text


def send_text(to: str, body: str) -> dict:
    """Send a plain-text WhatsApp message. `to` is +E.164. Free-form text is
    allowed inside the 24h customer-service window (user messaged us first)."""
    url = f"{config.TELNYX_API}/messages/whatsapp"
    headers = {"Authorization": f"Bearer {config.TELNYX_API_KEY}",
               "Content-Type": "application/json"}
    payload = {
        "from": config.TELNYX_WHATSAPP_NUMBER,
        "to": to,
        "whatsapp_message": {"type": "text",
                             "text": {"body": format_for_whatsapp(body)[:4096], "preview_url": False}},
    }
    with httpx.Client(timeout=20) as client:
        r = client.post(url, headers=headers, json=payload)
        return {"status": r.status_code, "body": r.text}


def fetch_media(url: str):
    """Fetch a Telnyx media URL and return (bytes, content_type) or None on failure.

    Adds Bearer auth only for telnyx.com hostnames; skips auth for all others.
    Caps at 5 MB and times out after 20s. Never raises: returns None on any
    error so a media failure doesn't crash the webhook.
    """
    try:
        host = urlparse(url).hostname or ""
        headers = {}
        if host.endswith("telnyx.com"):
            headers["Authorization"] = f"Bearer {config.TELNYX_API_KEY}"
        with httpx.Client(timeout=20) as client:
            r = client.get(url, headers=headers, follow_redirects=True)
            if r.status_code != 200:
                log.warning("fetch_media: status %s for host %s", r.status_code, host)
                return None
            ct = r.headers.get("content-type", "").split(";")[0].strip()
            if ct not in _ACCEPTED_MEDIA_TYPES:
                log.info("fetch_media: skipping unsupported type %s", ct)
                return None
            data = r.content
            if len(data) > _MEDIA_SIZE_CAP:
                log.warning("fetch_media: response exceeds 5 MB, discarding")
                return None
            return data, ct
    except Exception:
        log.warning("fetch_media: failed to fetch media", exc_info=True)
        return None


def parse_inbound(payload: dict):
    """Yield (sender, text, media) for an inbound Telnyx message webhook.

    Sender keeps its leading '+' (as Telnyx sends it). Ignores delivery
    receipts etc. text may be None for a caption-less photo. media is a list
    of {url, content_type} dicts (may be empty).
    """
    data = payload.get("data", {})
    if data.get("event_type") != "message.received":
        return
    p = data.get("payload", {})
    sender = (p.get("from") or {}).get("phone_number")
    if not sender:
        return

    # SMS puts the text at payload.text (str); WhatsApp nests it at
    # payload.body.text.body. Handle both.
    text = p.get("text")
    if not isinstance(text, str):
        t = (p.get("body") or {}).get("text")
        text = t.get("body") if isinstance(t, dict) else (t if isinstance(t, str) else None)

    # Media: Telnyx WhatsApp puts media in a top-level `media` list of
    # {url, content_type} on the payload. Also check the WhatsApp-nested
    # shape at body.image / body.document.
    media = []
    raw_media = p.get("media")
    if isinstance(raw_media, list):
        for m in raw_media:
            if isinstance(m, dict) and m.get("url"):
                media.append({"url": m["url"], "content_type": m.get("content_type", "")})

    # WhatsApp-nested image shape: payload.body.image.url
    body = p.get("body") or {}
    for key in ("image", "sticker"):
        img = body.get(key)
        if isinstance(img, dict) and img.get("url"):
            media.append({"url": img["url"], "content_type": img.get("mime_type", "image/jpeg")})

    if sender and (text or media):
        yield sender, text, media
