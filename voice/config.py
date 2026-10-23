"""Voice config: everything from the environment, delivered by push-secrets
into voice/.env (which the systemd unit loads via EnvironmentFile). No secret
literals live in code."""
import os

import g_config

# --- reasoning engine (OpenRouter, OpenAI-compatible) -----------------------
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# Sent as the app's referer when set. Read through g_config, so the instance env can carry it.
PUBLIC_URL = g_config.env("VOICE_PUBLIC_URL")
# The owner's name as the Voice says it ("the Voice of <name>'s brain"), also read
# through g_config. Unset, the prompts say "the owner".
OWNER_NAME = g_config.env("VOICE_OWNER_NAME") or "the owner"
# Override with VOICE_MODEL. Must be an OpenRouter model id that supports BOTH
# tool-calling and image input: voice runs a tool loop and ingests photos.
#
# Measured against the real TOOLS payload, running the FULL tool loop (not just
# the first assistant turn: judging turn one scores a model that sensibly
# searches before writing as a failure) and with realistic conversation history,
# because the production misses happened mid-chat, not on a clean first turn:
#   deepseek-v4.1-flash  60/60    mistral-small-3.2  38/40
#   glm-5.3-flash        40/40    claude-haiku-4.5   37/40
# mistral-small's misses were the real bug: it answered "Got it, noted." and
# called nothing, which is exactly what the owner saw on WhatsApp. Cost is not a
# tiebreak at this volume: the whole field is under $2/year for ~16 msgs/month.
# deepseek also has 18 providers behind it on OpenRouter (mistral-small had 3),
# which matters because a single degraded provider is how this silently breaks.
MODEL = os.environ.get("VOICE_MODEL", "deepseek/deepseek-v4.1-flash")
# App-session turns that carry an image run on this model instead. deepseek
# lists image input, but on 2026-10-23 most of its OpenRouter providers
# (DigitalOcean, CoreWeave, InferenceNet, Baidu) silently dropped the image once
# the real system prompt and history were in the request: a red test square came
# back "White", or "no image came through". glm-5.3-flash read it 3/3 across
# three providers with the full prompt, history and tools, and scored 40/40 on
# the tool loop above.
VISION_MODEL = os.environ.get("VOICE_VISION_MODEL", "z-ai/glm-5.3-flash")

# --- WhatsApp via Telnyx (Telnyx is the BSP) --------------------------------
TELNYX_API_KEY = os.environ.get("TELNYX_API_KEY", "")            # Bearer for the API
TELNYX_WHATSAPP_NUMBER = os.environ.get("TELNYX_WHATSAPP_NUMBER", "")  # bot sender, +E.164
TELNYX_PUBLIC_KEY = os.environ.get("TELNYX_PUBLIC_KEY", "")      # base64 Ed25519, webhook verify
TELNYX_API = "https://api.telnyx.com/v2"
# When Telnyx refuses a reply, push the owner's reply text through ntfy instead
# so it is read rather than lost. Off by default: ntfy.sh is a public relay and
# the topic name is the only lock on it, so the owner opts in.
NTFY_FALLBACK = os.environ.get("VOICE_NTFY_FALLBACK", "").strip().lower() not in ("", "0", "false", "no")

# --- access control ---------------------------------------------------------
# Only these sender numbers may talk to the brain. Comma-separated E.164 without
# the leading '+'. Empty list = deny everyone (fail closed).
ALLOWED_NUMBERS = [n.strip().lstrip("+") for n in os.environ.get("VOICE_ALLOWED_NUMBERS", "").split(",") if n.strip()]

# Allow non-owner numbers to chat as GUESTS (fun conversation, NO brain access).
# Owner numbers = ALLOWED_NUMBERS get full tools; everyone else is a guest.
GUESTS_OK = os.environ.get("VOICE_ALLOW_GUESTS", "").strip().lower() not in ("", "0", "false", "no")

# Agent loop guardrail.
# deepseek explores before it commits: measured chains of 8 tool calls where
# a tighter model used 3. The loop must have room to reach create_todo, because
# running out of iterations returns the "got stuck" fallback and writes nothing.
MAX_TOOL_ITERATIONS = 10


def missing(keys):
    """Return the subset of env keys that are unset, used for a clear /health."""
    return [k for k in keys if not os.environ.get(k)]


TRANSPORT_KEYS = ["TELNYX_API_KEY", "TELNYX_WHATSAPP_NUMBER", "TELNYX_PUBLIC_KEY"]
ENGINE_KEYS = ["OPENROUTER_API_KEY"]
