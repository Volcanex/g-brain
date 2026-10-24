"""One PCM24 call over the xAI or OpenAI Realtime WebSocket protocol."""
import asyncio
import base64
import json
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed


# Checked 2026-10-24: https://docs.x.ai/developers/models/realtime-api
XAI_PER_MINUTE = 0.08
XAI_TEXT_INPUT = 0.004
# USD / million tokens; transcription is billed separately, so this is an estimate.
# https://developers.openai.com/api/docs/pricing (Realtime mini audio/text rates)
OPENAI_AUDIO_IN, OPENAI_AUDIO_OUT, OPENAI_AUDIO_CACHED = 10.0, 20.0, 0.30
OPENAI_TEXT_IN, OPENAI_TEXT_OUT, OPENAI_TEXT_CACHED = 0.60, 2.40, 0.06


@dataclass
class _Reply:
    id: int
    response_id: str
    requested_at: float
    speech_stopped: float | None
    item_id: str | None = None
    text: str = ""
    audio_bytes: int = 0
    played_ms: int = 0
    played_done: bool = False
    interrupted: bool = False
    transcript_done: bool = False
    persisted: bool = False
    done: bool = False
    first_audio: float | None = None
    pending_tools: int = 0
    continued: bool = False
    finished: asyncio.Event = field(default_factory=asyncio.Event)


class RealtimeCall:
    def __init__(self, *, provider: str, api_key: str, model: str,
                 voice: str | None, instructions: str, tools: list[dict],
                 run_tool, send_json, send_audio, on_turn, greet: bool = True,
                 url: str | None = None):
        if provider not in {"xai", "openai"}:
            raise ValueError("provider must be xai or openai")
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self.voice = voice
        self.instructions = instructions
        self.tools = [{"type": "function", **tool["function"]} for tool in tools]
        self.run_tool = run_tool
        self.send_json = send_json
        self.send_audio = send_audio
        self.on_turn = on_turn
        self.greet = greet
        host = "api.x.ai" if provider == "xai" else "api.openai.com"
        self.url = url or f"wss://{host}/v1/realtime?{urlencode({'model': model})}"
        self._ws = None
        self._closed = False
        self._muted = False
        self._state = None
        self._replies = {}
        self._responses = {}
        self._active = None
        self._turn = 0
        self._users = set()
        self._input_text = {}
        self._tool_calls = set()
        self._tasks = set()
        self._output_lock = asyncio.Lock()
        self._speech_stopped = None
        self._requested_at = None
        self._meter_at = None
        self._text_cost = 0.0

    def _session(self):
        audio = {
            "input": {"format": {"type": "audio/pcm", "rate": 24000}},
            "output": {"format": {"type": "audio/pcm", "rate": 24000}},
        }
        session = {"instructions": self.instructions, "tools": self.tools,
                   "audio": audio}
        if self.provider == "openai":
            session.update(type="realtime", output_modalities=["audio"])
            audio["input"]["transcription"] = {"model": "gpt-4o-mini-transcribe"}
            audio["input"]["turn_detection"] = {
                "type": "semantic_vad", "create_response": True,
                "interrupt_response": False,
            }
            if self.voice:
                audio["output"]["voice"] = self.voice
        else:
            # xAI keeps VAD and voice at session level, even with GA audio formats.
            session["turn_detection"] = {"type": "server_vad"}
            audio["input"]["transcription"] = {"model": "grok-transcribe"}
            if self.voice:
                session["voice"] = self.voice
        return session

    async def _send(self, event):
        if self._ws is not None and not self._closed:
            await self._ws.send(json.dumps(event))

    async def _set_state(self, state):
        if state != self._state:
            self._state = state
            await self.send_json({"type": "state", "state": state})

    async def _create_response(self):
        self._requested_at = time.monotonic()
        await self._set_state("thinking")
        await self._send({"type": "response.create"})

    async def start(self) -> None:
        self._ws = await connect(
            self.url, additional_headers={"Authorization": f"Bearer {self.api_key}"},
            max_size=None,
        )
        self._meter_at = time.monotonic()
        try:
            await self._send({"type": "session.update", "session": self._session()})
            async with asyncio.timeout(15):
                while True:
                    event = json.loads(await self._ws.recv())
                    if event["type"] == "session.updated":
                        break
                    if event["type"] == "error":
                        await self._error(event)
                        raise RuntimeError("Realtime session configuration failed")
            await self._set_state("listening")
            if self.greet:
                await self._create_response()
        except BaseException:
            await self.close()
            raise

    async def feed(self, pcm16: bytes) -> None:
        if pcm16 and not self._muted:
            await self._send({"type": "input_audio_buffer.append",
                              "audio": base64.b64encode(pcm16).decode("ascii")})

    async def mute(self, on: bool) -> None:
        self._muted = on

    async def say(self, text: str) -> None:
        if not text.strip():
            return
        await self.interrupt()
        await self._user(text)
        await self._send({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": text}],
        }})
        if self.provider == "xai":
            self._text_cost += XAI_TEXT_INPUT
        self._speech_stopped = None
        await self._create_response()

    def _playing(self):
        return next((r for r in reversed(self._replies.values())
                     if r.audio_bytes and not r.played_done and not r.interrupted), None)

    async def played(self, reply_id: int, ms: int, done: bool) -> None:
        reply = self._replies.get(reply_id)
        if reply is None:
            return
        reply.played_ms = max(reply.played_ms, max(0, int(ms)))
        reply.played_done = reply.played_done or done
        if reply.interrupted:
            await self._persist(reply)
        elif reply.played_done and reply.transcript_done:
            await self._persist(reply)
        if (self._state == "speaking" and not self._playing()
                and (self._active is None or self._active.done)):
            await self._set_state("listening")

    def _heard(self, reply):
        if not reply.interrupted and reply.played_done:
            return reply.text
        duration_ms = reply.audio_bytes / 48  # PCM16 mono, 24 samples/ms.
        fraction = min(1.0, reply.played_ms / duration_ms) if duration_ms else 0
        return reply.text[:int(len(reply.text) * fraction)].rstrip()

    async def _persist(self, reply):
        if reply.persisted:
            return
        reply.persisted = True
        if not reply.text:
            return
        text = self._heard(reply)
        if reply.interrupted:
            await self.send_json({"type": "assistant", "id": reply.id,
                                  "text": text, "final": True})
        if text:
            self.on_turn("assistant", text)

    async def _interrupt(self):
        # Serialize flushing against audio forwarding: no bytes may follow flush.
        async with self._output_lock:
            reply = self._playing()
            active = self._active
            if reply:
                reply.interrupted = True
                await self.send_json({"type": "flush", "id": reply.id})
                if reply.item_id:
                    await self._send({"type": "conversation.item.truncate",
                                      "item_id": reply.item_id, "content_index": 0,
                                      "audio_end_ms": reply.played_ms})
            if active and not active.done and not active.interrupted:
                active.interrupted = True
                await self._send({"type": "response.cancel"})
            elif active and active is reply and not active.done:
                await self._send({"type": "response.cancel"})
            if active and active.pending_tools:
                active.interrupted = True

    async def interrupt(self) -> None:
        await self._interrupt()
        await self._set_state("listening")

    async def _user(self, text, item_id=None):
        if item_id and item_id in self._users:
            return
        if item_id:
            self._users.add(item_id)
        # Flush acknowledgements normally arrive before the next transcription.
        for reply in self._replies.values():
            if reply.interrupted:
                await self._persist(reply)
        self._turn += 1
        await self.send_json({"type": "user", "text": text, "turn": self._turn})
        self.on_turn("user", text)

    async def _error(self, event):
        error = event.get("error") or {}
        await self.send_json({"type": "error", "message": error.get("message", "Realtime provider error")})

    def _cost(self, usage):
        if self.provider == "xai":
            now = time.monotonic()
            cost = (now - self._meter_at) * XAI_PER_MINUTE / 60 + self._text_cost
            self._meter_at, self._text_cost = now, 0.0
            return cost
        incoming = usage.get("input_token_details") or {}
        outgoing = usage.get("output_token_details") or {}
        cached = incoming.get("cached_tokens_details") or {}
        cost = 0.0
        for modality, price_in, price_out, price_cached in (
            ("audio_tokens", OPENAI_AUDIO_IN, OPENAI_AUDIO_OUT, OPENAI_AUDIO_CACHED),
            ("text_tokens", OPENAI_TEXT_IN, OPENAI_TEXT_OUT, OPENAI_TEXT_CACHED),
        ):
            cached_n = cached.get(modality, 0)
            cost += max(0, incoming.get(modality, 0) - cached_n) * price_in
            cost += cached_n * price_cached + outgoing.get(modality, 0) * price_out
        return cost / 1_000_000

    async def _tool(self, reply, event):
        name = event["name"]
        try:
            try:
                result = await self.run_tool(name, event.get("arguments", "{}"))
            except Exception:
                result = {"error": "Tool execution failed"}
            await self._send({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": event["call_id"],
                "output": json.dumps(result),
            }})
            await self.send_json({"type": "tool", "name": name, "status": "done"})
            await reply.finished.wait()
            reply.pending_tools -= 1
            if not reply.pending_tools and not reply.interrupted and not self._closed:
                if not reply.continued:
                    reply.continued = True
                    await self._create_response()
        except ConnectionClosed:
            pass

    async def _event(self, event):
        kind = event["type"]
        if kind == "input_audio_buffer.speech_started":
            await self._interrupt()
            await self._set_state("hearing")
            return
        if kind == "input_audio_buffer.speech_stopped":
            self._speech_stopped = self._requested_at = time.monotonic()
            await self._set_state("thinking")
            return
        if kind in {"error", "conversation.item.input_audio_transcription.failed"}:
            await self._error(event)
            return
        if kind.startswith("conversation.item.input_audio_transcription."):
            item_id = event.get("item_id")
            if kind.endswith(".updated"):
                self._input_text[item_id] = event.get("transcript", "")
            elif kind.endswith(".delta"):
                self._input_text[item_id] = self._input_text.get(item_id, "") + event.get("delta", "")
            elif kind.endswith((".completed", ".done")):
                text = event.get("transcript", self._input_text.get(item_id, ""))
                self._input_text.pop(item_id, None)
                await self._user(text, item_id)
            return
        if kind == "response.created":
            response_id = event["response"]["id"]
            reply = _Reply(len(self._replies) + 1, response_id,
                           self._requested_at or time.monotonic(), self._speech_stopped)
            self._requested_at = None
            self._responses[response_id] = self._active = reply
            self._replies[reply.id] = reply
            await self.send_json({"type": "reply", "id": reply.id})
            await self._set_state("thinking")
            return
        response_id = event.get("response_id") or event.get("response", {}).get("id")
        reply = self._responses.get(response_id)
        if reply is None:
            return
        if kind == "response.output_item.added":
            item = event["item"]
            if item.get("type") == "message" and item.get("role") == "assistant":
                reply.item_id = item["id"]
        elif kind in {"response.output_audio.delta", "response.audio.delta"}:
            async with self._output_lock:
                if reply.interrupted:
                    return
                reply.item_id = event.get("item_id", reply.item_id)
                audio = base64.b64decode(event["delta"])
                if reply.first_audio is None:
                    reply.first_audio = time.monotonic()
                    if self._speech_stopped == reply.speech_stopped:
                        self._speech_stopped = None
                    await self._set_state("speaking")
                reply.audio_bytes += len(audio)
                await self.send_audio(audio)
        elif kind in {"response.output_audio_transcript.delta", "response.audio_transcript.delta"}:
            if not reply.interrupted:
                reply.text += event.get("delta", "")
                await self.send_json({"type": "assistant", "id": reply.id,
                                      "text": event.get("delta", ""), "final": False})
        elif kind in {"response.output_audio_transcript.done", "response.audio_transcript.done"}:
            if not reply.interrupted:
                reply.text = event.get("transcript", reply.text)
                reply.transcript_done = True
                await self.send_json({"type": "assistant", "id": reply.id,
                                      "text": reply.text, "final": True})
                if reply.played_done:
                    await self._persist(reply)
        elif kind == "response.function_call_arguments.done":
            if reply.interrupted or event["call_id"] in self._tool_calls:
                return
            self._tool_calls.add(event["call_id"])
            reply.pending_tools += 1
            await self.send_json({"type": "tool", "name": event["name"], "status": "start"})
            task = asyncio.create_task(self._tool(reply, event))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        elif kind == "response.done" and not reply.done:
            reply.done = True
            reply.finished.set()
            await self.send_json({"type": "reply_done", "id": reply.id})
            metrics = {"type": "metrics", "id": reply.id,
                       "cost": self._cost(event["response"].get("usage") or {})}
            for key, start in (("eos_ms", reply.speech_stopped), ("ttfa_ms", reply.requested_at)):
                if start is not None and reply.first_audio is not None:
                    metrics[key] = round(max(0, reply.first_audio - start) * 1000)
            await self.send_json(metrics)
            if event["response"].get("status") == "failed":
                await self._error({"error": event["response"].get("status_details", {}).get("error")})
            if not self._playing() and not reply.pending_tools and not reply.interrupted:
                await self._set_state("listening")

    async def run(self) -> None:
        try:
            async for message in self._ws:
                await self._event(json.loads(message))
        except ConnectionClosed as exc:
            if not self._closed:
                await self.send_json({"type": "error", "message": f"Realtime connection closed ({exc.code})"})
        finally:
            await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._ws is not None:
            await self._ws.close()
        for reply in self._replies.values():
            if not reply.persisted:
                if not reply.played_done:
                    reply.interrupted = True
                await self._persist(reply)
