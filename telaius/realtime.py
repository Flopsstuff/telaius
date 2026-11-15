"""The xAI Realtime side of a call: session setup, server events, bot audio, the result tools.

The modem side lives in session.py; this module knows nothing about phone calls beyond the two
tools the bot uses to report (`end_call`, `report_result`).
"""
import asyncio
import base64
import json
import threading
import time
import wave

import websockets

from telaius.config import api_key
from telaius.errors import XaiError
from telaius.logs import log
from telaius.prompts import LANG_HINT, tools as result_tools

XAI_URL = "wss://api.x.ai/v1/realtime?model=grok-voice-latest"
REPORT_WAIT = 10.0  # seconds to wait for report_result after the call ended


def connect():
    return websockets.connect(XAI_URL, additional_headers={"Authorization": f"Bearer {api_key()}"})


def session_config(rate, instructions, voice, hint, with_transcription, tools=None):
    audio_in = {"format": {"type": "audio/pcm", "rate": rate}}
    if with_transcription:
        audio_in["transcription"] = {"model": "grok-transcribe"}
        if hint:
            audio_in["transcription"]["language_hint"] = hint
    return {
        "type": "session.update",
        "session": {
            "voice": voice,
            "instructions": instructions,
            "turn_detection": {"type": "server_vad"},
            "tools": tools or result_tools(),
            "audio": {"input": audio_in, "output": {"format": {"type": "audio/pcm", "rate": rate}}},
        },
    }


async def configure(ws, rate, instructions, voice, hint, tools=None):
    # Transcription is optional; if the server rejects it, keep the call working without transcripts.
    for with_tr in (True, False):
        await ws.send(json.dumps(session_config(rate, instructions, voice, hint, with_tr, tools)))
        while True:
            ev = json.loads(await ws.recv())
            if ev["type"] == "session.updated":
                log.info("session.updated (rate=%s, voice=%s, transcription=%s)",
                         rate, voice, "on" if with_tr else "off")
                return
            if ev["type"] == "error":
                log.warning("session.update error: %s", ev.get("error", ev))
                break
    raise XaiError("could not configure the xAI session")


def save_wav(path, rate, data):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(data))


class Playback:
    """Bot audio waiting to be written to the modem; cleared on barge-in."""

    def __init__(self):
        self.buf = bytearray()
        self.lock = threading.Lock()

    def push(self, data):
        with self.lock:
            self.buf.extend(data)

    def pop(self, n):
        with self.lock:
            out = bytes(self.buf[:n])
            del self.buf[:n]
        return out

    def clear(self):
        with self.lock:
            dropped = len(self.buf)
            self.buf.clear()
        return dropped

    def pending(self):
        with self.lock:
            return len(self.buf)


def add_transcript(state, ev, who):
    """Keep the final text per conversation item; interim .done events for the same item overwrite it."""
    text = (ev.get("transcript") or ev.get("text") or "").strip()
    if not text:
        return
    key = ev.get("item_id") or f"{who}-{len(state['transcript'])}"
    idx = state["transcript_idx"].get(key)
    if idx is None:
        state["transcript_idx"][key] = len(state["transcript"])
        state["transcript"].append({"t": round(time.monotonic() - state["t_call"], 2), "role": who, "text": text})
    else:
        state["transcript"][idx]["text"] = text


def new_state():
    return {"transcript": [], "transcript_idx": {}, "t_call": time.monotonic()}


async def handle_events(ws, playback, state, rate):
    """Consume server events: queue audio, record transcripts, react to barge-in and the result tools."""
    async for raw in ws:
        if isinstance(raw, bytes):
            playback.push(raw)
            continue
        ev = json.loads(raw)
        t = ev["type"]
        if t in ("response.output_audio.delta", "response.audio.delta"):
            if state.get("resp_t0") is not None and not state.get("first_audio_logged"):
                log.info("first bot audio, %.2fs after user speech stopped", time.monotonic() - state["resp_t0"])
                state["first_audio_logged"] = True
            playback.push(base64.b64decode(ev["delta"]))
        elif t == "input_audio_buffer.speech_started":
            state["speech_seen"] = True
            dropped = playback.clear()
            log.info("user speech started (barge-in, dropped %.1fs of bot audio)", dropped / (rate * 2))
        elif t == "input_audio_buffer.speech_stopped":
            state["resp_t0"] = time.monotonic()
            state["first_audio_logged"] = False
            log.info("user speech stopped")
        elif "transcript" in t and t.endswith((".done", ".completed")):
            who = "user" if "input_audio" in t else "bot"
            log.info("%s: %s", who, ev.get("transcript") or ev.get("text") or "")
            add_transcript(state, ev, who)
        elif t == "response.function_call_arguments.done" and ev.get("name") in ("end_call", "report_result"):
            try:
                args = json.loads(ev.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            unresolved = args.get("unresolved")
            state["result"] = {"outcome": args.get("outcome", "unknown"), "summary": args.get("summary", ""),
                               "data": args.get("data") if isinstance(args.get("data"), dict) else None,
                               "unresolved": [str(item) for item in unresolved] if isinstance(unresolved, list) else []}
            log.info("bot %s: outcome=%s summary=%r", ev["name"], state["result"]["outcome"],
                     state["result"]["summary"])
            await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": ev["call_id"], "output": "{\"ok\": true}"}}))
            if ev["name"] == "end_call":
                state["end_requested"] = True
        elif t == "error":
            log.warning("xai error: %s", ev.get("error", ev))


async def request_report(ws, state, reason):
    """The call ended without end_call (callee hung up, time limit, cancelled): ask for the result."""
    note = (f"[system] The phone call has ended ({reason}). Do not say anything. "
            "Call report_result with the outcome, a short summary for the owner and what stayed unresolved.")
    await ws.send(json.dumps({"type": "conversation.item.create", "item": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": note}]}}))
    await ws.send(json.dumps({"type": "response.create"}))
    deadline = time.monotonic() + REPORT_WAIT
    while time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        if state.get("result"):
            return
    log.warning("no report_result from the model")


async def probe(spec, out_dir, seconds=12):
    """No call: open a session, let the bot greet, save its audio. Checks the key and the prompt."""
    rate = 8000
    async with connect() as ws:
        await configure(ws, rate, spec.instructions(), spec.voice, LANG_HINT[spec.lang], spec.tools())
        playback, state = Playback(), new_state()
        events = asyncio.create_task(handle_events(ws, playback, state, rate))
        await ws.send(json.dumps({"type": "response.create"}))
        await asyncio.sleep(seconds)
        events.cancel()
        audio = playback.pop(playback.pending())
        save_wav(out_dir / "bot_probe.wav", rate, audio)
        log.info("saved %.1fs of bot audio to %s", len(audio) / (rate * 2), out_dir / "bot_probe.wav")
