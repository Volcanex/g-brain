# voice: interactive face of the brain (WhatsApp + OpenRouter)

The third layer of the live system: `Cortex ← Pulse ← Voice`. See
`designs/brain-voice` and `designs/pulse` in the brain for the architecture.

## What it is
A small FastAPI service. WhatsApp messages in → an OpenRouter reasoning loop with
brain tools → a reply out. Runs on the brain host as `voice.service`, proxied at
the instance's `VOICE_PUBLIC_URL`.

## Why it imports g-brain directly
The Voice runs *beside* the brain code, so it calls `brain_reader` /
`brain_todos` / `brain_commit` in-process instead of the OAuth-gated MCP face.
The MCP/REST faces are for *remote* agents; the Voice is local. Same codepath the
MCP tools use.

## Safety contract
- Reads the cortex freely and creates owner commitments through the shared todo boundary.
  Voice records `requester="gabriel"`, `composer="agent"` and the originating voice event
  as source. Machine alerts are refused even for the owner. Expiry is derived on read.
- **Never** writes a durable note without an explicit in-chat 'yes': the model
  must `propose_commit` (draft), show the owner, then `execute_commit` only on
  confirmation. Enforced by the system prompt, not by code. Keep it that way
  deliberately (the human is the gate).
- Inbound is fail-closed: Telnyx **Ed25519** webhook signature check
  (`telnyx-signature-ed25519` + `telnyx-timestamp`, verified with
  `TELNYX_PUBLIC_KEY`) **and** a sender allowlist (`VOICE_ALLOWED_NUMBERS`).
  Empty allowlist = nobody.
- Photo messages: owner senders can send JPEG/PNG/WebP/GIF images. Media is
  fetched after the 200 ACK (background task), base64-encoded for the vision
  model, then discarded: only a `[photo]` placeholder lands in history.
  Guests get text-only (media silently dropped).

## App session (the G-Command iPhone and Mac chat)
`appchat.py`, reached as `/chat` with `session: "app"` (one JSON reply) or
`/chat/stream` (NDJSON `delta` / `tool` / `done`). g-command-web's
`pages/voice/api.py` is the only caller and fills the extra fields.
- Replies are `{reply, blocks}`. Blocks are g-command-web's panel contract
  (`core/panels/CLAUDE.md`, the one schema web, phone and Mac draw);
  `blocks.py` drops anything that does not fit it. `show_panel` emits a
  reference that the proxy expands into the live dash panel. A new block type
  goes into that spec first, then `blocks.TYPES`.
- `budget` (`{lines, chars_per_line}`, the app's visible chat area) is a soft
  guide in the app prompt only. Web, talk and WhatsApp prompts are unchanged.
- Attachments: the proxy stores uploads in its `var/voice-uploads/` and sends
  images base64 (the model, deepseek-v4.1-flash, takes image input; check
  OpenRouter's `input_modalities` before changing `VOICE_MODEL`) and small text
  files inline. History keeps `[photo]` / `[file: name]` and the upload ids.
- **Agent activity is READ ONLY, by design.** `agent_activity` reads tmux
  sessions, pane tails and recent Claude Code session files through
  g-command-web's `GET /api/sessions/activity`, with a five-minute grant the
  proxy mints per turn. Voice must never send input to, draft for or drive an
  agent session: Gabriel does that himself from Workspaces. Do not add a
  send-keys tool, an input route or a "type this for me" path.
- The transcript records `blocks` and `attachments` beside `text`;
  `load_tail` still reads only role and text, so the model never re-reads
  old block JSON.

## Files
- `app.py`: webhook (GET verify / POST inbound) + `/health`; logs one journal
  line per inbound message (sender, text_len, media_count, not the full text)
- `agent.py`: OpenRouter tool-calling loop; per-sender history is rehydrated
  from transcript on first access after a restart (restart-proof, no amnesia)
- `transcript.py`: write-through JSONL transcript; one file per sender in
  `g_config.VOICE_TRANSCRIPT_DIR` (default `$BRAIN_ROOT/.pulse/state/voice/`,
  moved by setting `VOICE_TRANSCRIPT_DIR`, which the tests do). To review a
  conversation: `cat $BRAIN_ROOT/.pulse/state/voice/+<number>.jsonl`
- `tools.py`: brain tool wrappers (the bounded toolset)
- `whatsapp.py`: Telnyx WhatsApp transport: send (`POST /v2/messages/whatsapp`)
  + Ed25519 webhook verify + inbound parse. `parse_inbound` yields
  `(sender, text, media)` where `media` is a list of `{url, content_type}` dicts
  and `text` may be None for caption-less photos. `fetch_media(url)` downloads a
  Telnyx media URL (Bearer auth for telnyx.com hosts), caps at 5 MB, returns
  `(bytes, content_type)` or None. Telnyx is the BSP.
- `prompt.py`: identity + the gate rule
- `appchat.py`, `blocks.py`: the app session and its reply blocks (above)
- `cli.py`: local tester (`python -m voice.cli`), no WhatsApp needed
- `config.py`: all env; secrets arrive via `push-secrets` → `voice/.env`

## Run / test
- Local brain chat (needs only `OPENROUTER_API_KEY`): from the repo root,
  `.venv/bin/python -m voice.cli`
- Service: `uvicorn voice.app:app --host 127.0.0.1 --port 8077`
- Health: `curl localhost:8077/health`, shows which creds are still missing.

## Gotchas
- **This package configures its own logging, and must.** uvicorn sets up only
  its own loggers; the root logger stays at WARNING with no handler, so every
  `log.info` here is discarded unless `app.py` attaches a handler to the
  `"voice"` logger. It does. Don't remove it, and don't "verify" logging from a
  test that calls `basicConfig`: that installs a root handler and hides the
  very bug you're testing for. Check the real journal instead.
- The model id is injected into the system prompt (`agent.py`) so "what model
  are you running?" has a truthful answer. Without it the model guesses from
  training data and confidently claims to be Claude.
- `voice/.env` is written by `push-secrets`, never committed. `VOICE_MODEL` is
  pinned in the passport (the instance `.env`); setting it only in `voice/.env` gets
  reverted by the next `push-secrets` run.
- `/health` returns **503** when any dependency check fails. That is deliberate:
  g-core's contract is "health_url -> expect 2xx", so a 200 with `{"ok": false}`
  tells the alert spine everything is fine while the bot is about to go mute.
- A reply that does not go out (Telnyx refuses it, or `_process` raises) pages
  through `brain_alerts` with `dedup_key="voice-send"`: once per six-hour
  cooldown while it stays broken, cleared by the next delivered reply. Without
  it a refused send is just silence on the phone; Telnyx's 403 10038 (account
  level) went unnoticed for days in October 2026.
- Semantic search isn't wired yet: `brain_search` is plain text match over
  note bodies (MVP). Upgrade path: basic-memory at 127.0.0.1:8765.
- Conversation history survives restarts: rehydrated from the JSONL transcript
  in `VOICE_TRANSCRIPT_DIR` on first access per sender.
- Talk (spoken turns): `POST /talk` streams NDJSON, one sentence per line as it
  closes (`talk.py`), and `POST /tts` turns one sentence into WAV through
  `voice-tts.service`, warm Kokoro-82M int8 on CPU at 127.0.0.1:8078
  (`tts_server.py`, own venv and model files in `~gabriel/.local/share/voice-tts`,
  MemoryMax 700M, about 350 MB resident). The browser side is the talk card on
  gcommand's `/voice` page: Web Speech recognition, barge-in, one TTS call per
  sentence. Short messages with no request words skip the tool schema; the
  first clause of a reply goes out alone so audio starts sooner. To swap in
  Sesame CSM-1B, replace only the voice-tts service: same `/tts` contract, a GPU
  host, and the last few turns of audio passed as context.
- Telnyx 10038 covers the whole WhatsApp API on this account (reads included),
  so no other endpoint gets a reply out. `VOICE_NTFY_FALLBACK=1` pushes refused
  owner replies to ntfy instead.
