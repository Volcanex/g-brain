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
  images base64 and small text files inline. A turn with an image runs on
  `VOICE_VISION_MODEL` (glm-5.3-flash): deepseek lists image input, but most of
  its OpenRouter providers silently drop the image once the real prompt and
  history are in the request (measured 2026-10-23, see `config.py`). Re-test
  with a full-prompt request, not a bare one, before changing either model. History keeps `[photo]` / `[file: name]` and the upload ids.
- **Todos and the calendar are one thing** (the calendar is the dated todos).
  `todos.py` changes them: `update_todo` (title, date, time, priority, star,
  category, location), `reschedule_todo`, `mark_done`, `mark_failed`
  (did-not-happen), `drop_todo` (cancelled), plus `create_todo` (wraps
  `tools.create_todo`). Every write is `brain_todos._update`, the function
  behind the brain's `PATCH /api/todos/<slug>` that the dash and Day view reach
  through G-Command's `/api/brain` proxy; field meanings follow the Day view
  (star = `urgent`, recurring todos tick one date). Natural dates resolve in
  Europe/London. A target matching several todos changes nothing and returns
  the candidates as rows; the model asks.
- **Voice never deletes a todo.** `delete_todo` writes nothing: it returns the
  todo as a row whose `action` is `DELETE /api/brain/api/todos/<slug>`, and the
  app's Delete button is the only way it goes. Do not add a delete path to
  Voice, and do not let the model write actions: only rows `todos.py` builds
  (`blocks.clean(actions=True)` in `appchat`) may carry one; model rows from
  `attach_blocks` still lose theirs. Every todo change returns its row under
  `todos.BLOCKS`; the app session draws it, `agent._run_tool` strips it for
  WhatsApp and talk, which share the same tools.
- **Agent activity is READ ONLY, by design.** `agent_activity` reads tmux
  sessions, pane tails and recent Claude Code session files through
  g-command-web's `GET /api/sessions/activity`, with a five-minute grant the
  proxy mints per turn. Voice must never send input to, draft for or drive an
  agent session: Gabriel does that himself from Workspaces. Do not add a
  send-keys tool, an input route or a "type this for me" path.
- The transcript records `blocks` and `attachments` beside `text`;
  `load_tail` still reads only role and text, so the model never re-reads
  old block JSON.

## Live call (`/live`, speech to speech)
A phone-call conversation: the person talks, the Voice answers out loud within
about a second, and either can cut the other off. One WebSocket per call.
`live.py` runs the call; `turn.py` decides when the person has finished
speaking; `realtime.py` relays to a provider that does its own turn-taking.

Engines (`VOICE_LIVE_ENGINE`, default `openrouter`):
- `openrouter`: `openai/gpt-audio-mini` (`VOICE_LIVE_MODEL`) through chat
  completions with `modalities: [text, audio]`, `stream: true`, `pcm16`. The
  person's turn goes in as audio, the reply streams back as audio, tool calls
  work. Turn-taking is ours (`turn.py`); the person's words are
  transcribed beside it by `VOICE_LIVE_STT_MODEL` for captions, history and
  the transcript, never on the reply's critical path. Measured 2026-10-24 on
  h with the full prompt: smart-turn ends a turn about 220 ms into the
  silence, the model's first audio comes 0.8 to 0.9 s after the request, so
  the person hears a reply 1.0 to 1.2 s after they stop. A barge-in flush
  lands about 470 ms after they start talking over it. Cost: there is no
  prompt caching on this model through OpenRouter, so every model call pays
  for the whole prompt (about 3,250 tokens with the brief and tool schemas,
  $0.002). A plain turn is about $0.002, a tool turn (two calls) about $0.005.
- While a tool runs the reply opens with a cached filler ("One sec.") in the
  call's voice, generated once per voice into
  `~/.local/share/voice-live/fillers/<voice>/` and kept only if the model said
  exactly the line. No filler when the model already said something before
  the tool call ("Let me check").
- Before VAD, `live.EchoGuard` replaces residual reply echo with silence on
  the endpointer's existing 32 ms frames, including its preroll and turn audio.
  It uses outgoing PCM and `played` acknowledgements to search a measured lag
  window and learns coupling from correlated echo frames. Learning stops during
  VAD speech and for 300 ms after unexplained mic energy so an interruption
  cannot train it, including when no echo coupling has been learned yet.
  Once playback ends, only a matching echo tail is suppressed; other input
  passes unchanged. The provider-managed realtime engines keep their own VAD.
- The greeting is not a model call: a line for the hour ("Hey, you're up
  late.") read once per voice into the same folder (`line-<hash>.pcm`, see
  `GREETINGS`), never the same one twice running. Lines not read yet are read
  in the background, and until then the model greets.
- Saying goodbye ends the call: the model calls `end_call` (offered in every
  session, bench too, since it writes nothing) with its goodbye, the server
  waits for the client's done ack for it, sends `end` and closes. A bare
  `end_call` with no words plays a ready goodbye (`GOODBYES`, cached like the
  greetings) rather than paying a second model call to say "Bye.". Talking
  over the goodbye or starting a new turn keeps the call up.
- What the model is handed is shaped for speech, because rules in the prompt
  alone did not hold on gpt-audio-mini: the time comes ready-said
  (`spoken_time`, "twenty to midnight"), a short system note right before each
  turn repeats it and the stop rule (the shared history holds old calls whose
  times it otherwise copies), and `list_todos` returns a digest
  (`_todo_digest`: totals and three items, due dates as "overdue since the
  10th of October"; called again in the next turn with the same query it
  gives the next three, and a page counts only once some of the reply after
  it was heard). Putting the call rules first in the system prompt made it
  skip the tool and invent todos, so they stay last.
- `xai` (`grok-voice-latest`, needs `XAI_API_KEY`) and `openai`
  (`gpt-realtime-mini`, needs `OPENAI_API_KEY`): the OpenAI Realtime WebSocket
  protocol, relayed by `realtime.py`. The provider does turn detection and
  barge-in; we inject the prompt and tools and run the tools.

Client protocol (the contract the web page and the native apps speak; the
public face is g-command-web's `wss://<command host>/api/voice/live`, which
checks the `gcommand_auth` cookie and relays frames to `ws://127.0.0.1:8077/live`):
- Audio both ways is binary frames of PCM16 little-endian mono at 24 kHz. Mic
  chunks of 20 to 100 ms; the client applies echo cancellation (browser
  `echoCancellation`, Apple voice processing), the server never sees the
  speaker signal.
- Client to server, text frames:
  - `{"type":"hello","client":"web|ios|mac","session":"talk|app","voice"?,"greet"?}` first.
    `session` picks the shared history and transcript (`talk` for the web page,
    `app` so a call from the app lands in the app chat, `bench` for
    `live_bench.py` and automated tests: its own transcript, only the tools
    that read, and history emptied at the start of every call so runs compare). `greet` (default true) makes the Voice speak first.
  - `{"type":"played","id":n,"ms":N,"done":bool}`: how much of reply `n` the
    client has actually played. Sent about every 250 ms while playing, on
    `flush` and when playback drains (`done`), so the server knows when the
    reply ended and a cut-off reply is remembered only as far as it was heard.
  - `{"type":"interrupt"}`: the person tapped to stop the Voice.
  - `{"type":"mute","on":bool}`: a muted mic counts as silence.
  - `{"type":"say","text":"..."}`: a typed turn in the middle of a call.
  - `{"type":"bye"}`.
- Server to client, text frames:
  - `{"type":"ready","engine","model","voice","rate":24000}` after hello.
  - `{"type":"state","state":"listening|hearing|thinking|speaking"}`.
  - `{"type":"reply","id":n}` before the audio of reply `n`; binary audio
    follows; `{"type":"reply_done","id":n}` after its last frame.
  - `{"type":"flush","id":n}`: drop every buffered sample of reply `n` now
    (barge-in). No audio of `n` follows it, and no `reply_done`: the flush ends it.
  - `{"type":"user","text","turn":k}`: what the person said, once transcribed.
  - `{"type":"assistant","id":n,"text","final":bool}`: the reply's words,
    deltas then a final. Words can come before the `reply` event. A reply cut
    while it was still generating gets as its final only what was heard
    (what the history keeps), up to 0.6 s after the flush, so it can land
    after the next reply began: key captions by id.
  - `{"type":"tool","name","status":"start|done"}`.
  - `{"type":"metrics","id":n,"eos_ms","ttfa_ms","cost"}`: end of speech to
    first audio sent, model request to first audio, OpenRouter cost. A cut
    reply's stream never reports usage, so its cost is looked up from
    OpenRouter's generation stats (they appear 5 to 20 s late) and a second
    `metrics` for the same id replaces the first.
  - `{"type":"end","reason":"goodbye"}`: the Voice hung up after its goodbye
    played; the socket closes next. Show it as an ended call, not a drop.
  - `{"type":"error","message"}`; a fatal one is followed by close.

Measuring it: `python -m voice.live_bench` calls `/live` as `bench` with
Kokoro-rendered speech in real time and reports end of speech to first audio,
barge-in flush time, captions and cost per turn (about $0.015 a run;
`--selftest` is free, `--record call.wav` saves the call as heard). It is the sensor for the
`voice` charter's gates in g-levels (turn latency, barge-in, cost, spoken
register); `tests/test_voice_turn.py`, `test_voice_live.py` and
`test_voice_realtime.py` are the every-run turn-taking gate.

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
- `todos.py`: todo and calendar changes, date phrases, the delete guard (above)
- `whatsapp.py`: Telnyx WhatsApp transport: send (`POST /v2/messages/whatsapp`)
  + Ed25519 webhook verify + inbound parse. `parse_inbound` yields
  `(sender, text, media)` where `media` is a list of `{url, content_type}` dicts
  and `text` may be None for caption-less photos. `fetch_media(url)` downloads a
  Telnyx media URL (Bearer auth for telnyx.com hosts), caps at 5 MB, returns
  `(bytes, content_type)` or None. Telnyx is the BSP.
- `prompt.py`: identity + the gate rule
- `appchat.py`, `blocks.py`: the app session and its reply blocks (above)
- `live.py`, `turn.py`, `realtime.py`, `live_bench.py`: the live call, its
  endpointer (Silero VAD + smart-turn v3.2 on CPU, models sha256-pinned in
  `~/.local/share/voice-live/`), the realtime-provider relay and its bench
  (Live call, above)
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
