"""M1 prototype: bridge a SIM7600 USB PCM call to the xAI Realtime voice API.

Usage:
  python -m telaius.voicebridge --no-call                      # test the xAI session only, save the greeting
  python -m telaius.voicebridge NUMBER [--lang ru|pl] [--voice rex] [--audio usb|relay] [--max-seconds N]
  python -m telaius.voicebridge NUMBER --task "Записаться на стрижку в субботу после 12" \
      --callee "Барбершоп Brzytwa" --context "Имя клиента: Алексей" --audio relay

Without --task the bot holds a free test chat. Each call writes calls/<date>_<time>_<callee>/ (gitignored)
with call.mp3 (both sides), transcript.txt and result.json (outcome, summary, transcript, end reason).

--audio usb    SIMCom USB PCM (AT+CPCMREG), rate follows AT+CPCMFRM (8 kHz on the hotfix firmware)
--audio relay  telaius-relay running on the module (relay/), 16 kHz both ways, no AT+CPCMREG

XAI_API_KEY is read from the environment or from .env in the repository root.
"""
import argparse
import asyncio
import base64
import json
import os
import sys
import threading
import time
import wave
from pathlib import Path

import usb.core
import websockets

from telaius.callstore import CALLS_DIR, call_dir, save_call
from telaius.modem import IF_AT, IF_PCM, AtChannel, PcmAligner, UsbPort, close_modem, open_modem
from telaius.prompts import LANG_HINT, TOOLS, bot_name, build_instructions

XAI_URL = "wss://api.x.ai/v1/realtime?model=grok-voice-latest"
REPO_ROOT = Path(__file__).resolve().parents[1]

t_start = time.monotonic()


def log(msg):
    print(f"[{time.monotonic() - t_start:7.2f}] {msg}", flush=True)


def env_value(name, default=None):
    """A setting from the environment, else from .env in the repository root, else `default`."""
    if os.environ.get(name):
        return os.environ[name]
    env = REPO_ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith(f"{name}=") and line.split("=", 1)[1].strip():
                return line.split("=", 1)[1].strip()
    return default


def api_key():
    return env_value("XAI_API_KEY") or sys.exit("XAI_API_KEY is not set (environment or .env)")


def session_config(rate, instructions, voice, hint, with_transcription):
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
            "tools": TOOLS,
            "audio": {"input": audio_in, "output": {"format": {"type": "audio/pcm", "rate": rate}}},
        },
    }


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


async def configure(ws, rate, instructions, voice, hint):
    # Transcription is optional; if the server rejects it, keep the call working without transcripts.
    for with_tr in (True, False):
        await ws.send(json.dumps(session_config(rate, instructions, voice, hint, with_tr)))
        while True:
            ev = json.loads(await ws.recv())
            if ev["type"] == "session.updated":
                log(f"session.updated (rate={rate}, voice={voice}, transcription={'on' if with_tr else 'off'})")
                return
            if ev["type"] == "error":
                log(f"session.update error: {ev.get('error', ev)}")
                break
    sys.exit("could not configure xAI session")


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
                log(f"first bot audio, {time.monotonic() - state['resp_t0']:.2f}s after user speech stopped")
                state["first_audio_logged"] = True
            playback.push(base64.b64decode(ev["delta"]))
        elif t == "input_audio_buffer.speech_started":
            state["speech_seen"] = True
            dropped = playback.clear()
            log(f"user speech started (barge-in, dropped {dropped / (rate * 2):.1f}s of bot audio)")
        elif t == "input_audio_buffer.speech_stopped":
            state["resp_t0"] = time.monotonic()
            state["first_audio_logged"] = False
            log("user speech stopped")
        elif "transcript" in t and t.endswith((".done", ".completed")):
            who = "user" if "input_audio" in t else "bot"
            log(f"{who}: {ev.get('transcript') or ev.get('text') or ''}")
            add_transcript(state, ev, who)
        elif t == "response.function_call_arguments.done" and ev.get("name") in ("end_call", "report_result"):
            try:
                args = json.loads(ev.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            state["result"] = {"outcome": args.get("outcome", "unknown"), "summary": args.get("summary", "")}
            log(f"bot {ev['name']}: outcome={state['result']['outcome']} summary={state['result']['summary']!r}")
            await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": ev["call_id"], "output": "{\"ok\": true}"}}))
            if ev["name"] == "end_call":
                state["end_requested"] = True
        elif t == "error":
            log(f"xai error: {ev.get('error', ev)}")


def connect():
    return websockets.connect(XAI_URL, additional_headers={"Authorization": f"Bearer {api_key()}"})


async def probe(spec, out_dir):
    rate = 8000
    async with connect() as ws:
        await configure(ws, rate, spec["instructions"], spec["voice"], LANG_HINT[spec["lang"]])
        playback, state = Playback(), {"transcript": [], "transcript_idx": {}, "t_call": time.monotonic()}
        events = asyncio.create_task(handle_events(ws, playback, state, rate))
        await ws.send(json.dumps({"type": "response.create"}))
        await asyncio.sleep(12)
        events.cancel()
        audio = playback.pop(playback.pending())
        save_wav(out_dir / "bot_probe.wav", rate, audio)
        log(f"saved {len(audio) / (rate * 2):.1f}s of bot audio to {out_dir / 'bot_probe.wav'}")


RELAY_RATE = 16000
GREETING_WAIT = 2.0  # seconds of silence after answer before the bot greets first


async def request_report(ws, state, reason):
    """The call ended without end_call (callee hung up, time limit, interrupt): ask the model for the result."""
    note = (f"[system] The phone call has ended ({reason}). Do not say anything. "
            "Call report_result with the outcome and a short summary for the owner.")
    await ws.send(json.dumps({"type": "conversation.item.create", "item": {
        "type": "message", "role": "user", "content": [{"type": "input_text", "text": note}]}}))
    await ws.send(json.dumps({"type": "response.create"}))
    for _ in range(100):
        await asyncio.sleep(0.1)
        if state.get("result"):
            return
    log("no report_result from the model")


async def call(spec, audio, max_seconds, out_root):
    number = spec["number"]
    out_dir = call_dir(spec.get("callee"), number, out_root)  # created only when the call is saved
    dev = open_modem()
    at = AtChannel(UsbPort(dev, IF_AT))
    pcm = UsbPort(dev, IF_PCM)
    rate = RELAY_RATE if audio == "relay" else at.pcm_rate()
    frame_len = rate * 2 // 50  # 20 ms
    log(f"audio path: {audio}, PCM rate: {rate} Hz, output: {out_dir}")

    at_lock = threading.Lock()
    loop = asyncio.get_running_loop()
    mic_q = asyncio.Queue()
    playback = Playback()
    state = {"transcript": [], "transcript_idx": {}, "t_call": time.monotonic()}
    stop = threading.Event()
    rx_all, tx_all = bytearray(), bytearray()
    record = {"number": number, "lang": spec["lang"], "voice": spec["voice"],
              "bot_name": bot_name(spec["lang"], spec["voice"]), "audio": audio, "rate": rate,
              "task": spec.get("task"), "callee": spec.get("callee"), "context": spec.get("context"),
              "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "answered": False, "network": None}

    def at_send(cmd, wait=3):
        with at_lock:
            return at.send(cmd, wait)

    aligner = [PcmAligner(rate)]  # replaced when the audio path is restarted

    def reader():
        while not stop.is_set():
            try:
                chunk = pcm.read()
            except usb.core.USBError as e:
                log(f"pcm read error: {e}")
                time.sleep(0.05)
                continue
            chunk = aligner[0].feed(chunk)
            if chunk:
                rx_all.extend(chunk)
                loop.call_soon_threadsafe(mic_q.put_nowait, chunk)

    def writer():
        # The modem expects a steady 20 ms cadence; send silence when the bot is quiet.
        t0 = time.monotonic()
        while not stop.is_set():
            frame = playback.pop(frame_len)
            frame += bytes(frame_len - len(frame))
            tx_all.extend(frame)
            try:
                pcm.write(frame, timeout_ms=200)
            except usb.core.USBError:
                pass
            t0 += 0.02
            delay = t0 - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    def at_watch():
        while not stop.is_set():
            with at_lock:
                at.poll(100)
                if "VOICE CALL: END" in at.buf or "NO CARRIER" in at.buf:
                    state["remote_hangup"] = True
            time.sleep(0.05)

    async def uplink(ws):
        # Batch 100 ms of call audio per append.
        pending = bytearray()
        while True:
            pending.extend(await mic_q.get())
            if len(pending) >= frame_len * 5:
                await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                          "audio": base64.b64encode(bytes(pending)).decode()}))
                pending.clear()

    async with connect() as ws:
        await configure(ws, rate, spec["instructions"], spec["voice"], LANG_HINT[spec["lang"]])
        threads, tasks, t_answer = [], [], None
        end_reason = "interrupted"
        try:
            at_send("ATE0")
            at_send("AT+CLCC=0")
            at_send(f"ATD{number};", wait=10)
            with at_lock:
                got = at.wait_for(["VOICE CALL: BEGIN", "NO CARRIER", "BUSY", "NO ANSWER", "VOICE CALL: END"], 60)
            if got != "VOICE CALL: BEGIN":
                log(f"call not answered: {got}")
                end_reason = f"not_answered: {got or 'timeout'}"
                return
            record["answered"], t_answer = True, time.monotonic()
            # Audio can only be moved to USB once the call is active. Start pumping if4 first:
            # firmware B06 drops USB audio for the call if the host does not consume it right away,
            # and the relay starts its session when our frames arrive.
            for fn in (reader, writer):
                threads.append(threading.Thread(target=fn, daemon=True))
                threads[-1].start()
            if audio == "relay":
                for _ in range(30):
                    await asyncio.sleep(0.1)
                    if rx_all:
                        break
                else:
                    log("no call audio from the relay after 3 s (is telaius-relay running on the module?)")
            else:
                at_send("AT+CPCMREG=1")
                # At 16 kHz on VoLTE the modem's capture sometimes fails at call start and no audio
                # arrives; re-arming the USB audio path can recover it.
                for attempt in range(3):
                    await asyncio.sleep(1.5)
                    if rx_all:
                        break
                    log(f"no call audio from the modem yet, re-arming USB audio (attempt {attempt + 1})")
                    at_send("AT+CPCMREG=0")
                    aligner[0] = PcmAligner(rate)
                    at_send("AT+CPCMREG=1")
            threads.append(threading.Thread(target=at_watch, daemon=True))
            threads[-1].start()
            cpsi = at_send("AT+CPSI?")  # LTE here means VoLTE, GSM means CSFB
            record["network"] = next((ln.split(":", 1)[1].split(",")[0].strip()
                                      for ln in cpsi.splitlines() if ln.startswith("+CPSI:")), None)
            state["t_call"] = time.monotonic()
            tasks = [asyncio.create_task(handle_events(ws, playback, state, rate)), asyncio.create_task(uplink(ws))]
            # Let the callee say "алло" first, like a person would; otherwise the greeting overlaps it and
            # gets cut by barge-in. If they speak, server VAD answers when they stop; after 2 s of silence
            # the bot starts on its own.
            t_wait = time.monotonic()
            while time.monotonic() - t_wait < GREETING_WAIT and not state.get("speech_seen"):
                await asyncio.sleep(0.05)
            if state.get("speech_seen"):
                log("callee spoke first; the bot greets when they stop")
            else:
                log(f"no hello within {GREETING_WAIT:.0f} s; the bot greets first")
                await ws.send(json.dumps({"type": "response.create"}))
            while time.monotonic() - t_answer < max_seconds:
                await asyncio.sleep(0.2)
                if state.get("remote_hangup"):
                    log("remote hung up")
                    end_reason = "remote_hangup"
                    break
                if state.get("end_requested") and playback.pending() == 0:
                    await asyncio.sleep(0.6)  # let the last frames leave the modem
                    log("hanging up after bot goodbye")
                    end_reason = "bot_goodbye"
                    break
            else:
                log("max duration reached")
                end_reason = "max_duration"
        finally:
            stop.set()
            for t in threads:
                t.join(1)
            at_send("AT+CHUP")
            if audio == "usb":
                at_send("AT+CPCMREG=0")
            close_modem(dev, [IF_AT, IF_PCM])
            if record["answered"] and not state.get("result"):
                try:
                    await request_report(ws, state, end_reason)
                except (websockets.ConnectionClosed, OSError) as e:
                    log(f"could not request the call result: {e}")
            for t in tasks:
                t.cancel()
            result = state.get("result")
            if not result:
                # e.g. the callee hung up during the greeting and the model sent no report
                result = {"outcome": "failed" if end_reason == "remote_hangup" else "unknown",
                          "summary": f"Итог от бота не получен (завершение: {end_reason})."}
            record.update({
                "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "duration_s": round(time.monotonic() - t_answer, 1) if t_answer else 0,
                "end_reason": end_reason, "outcome": result["outcome"], "summary": result["summary"],
                "transcript": state["transcript"],
            })
            save_call(out_dir, rate, rx_all, tx_all, record)
            log(f"result: {end_reason}, outcome={result['outcome']}, {record['duration_s']} s -> {out_dir}")
            if result["summary"]:
                log(f"summary: {result['summary']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("number", nargs="?")
    ap.add_argument("--no-call", action="store_true", help="only test the xAI session")
    ap.add_argument("--task", help="what the call should achieve, in plain words (without it: free test chat)")
    ap.add_argument("--callee", help="who is being called (name, role)")
    ap.add_argument("--context", help="facts the bot may use (dates, references, preferences)")
    ap.add_argument("--prompt-file", type=Path, help="full system prompt, replaces the built-in one")
    ap.add_argument("--formal", action="store_true",
                    help="calling an organization: open with the question, then say it's an AI assistant; formal address")
    ap.add_argument("--lang", choices=sorted(LANG_HINT), default="ru")
    ap.add_argument("--voice", default=env_value("TELAIUS_VOICE", "rex"),
                    help="xAI voice: rex, leo (male), eve, ara (female), sal; default TELAIUS_VOICE from .env, else rex")
    ap.add_argument("--audio", choices=["usb", "relay"], default="usb", help="call audio path (see above)")
    ap.add_argument("--max-seconds", type=float, default=120)
    ap.add_argument("--out-dir", type=Path, default=CALLS_DIR,
                    help="each call gets its own subfolder with call.mp3, transcript.txt and result.json")
    a = ap.parse_args()
    a.out_dir.mkdir(parents=True, exist_ok=True)
    spec = {"number": a.number, "lang": a.lang, "voice": a.voice, "task": a.task, "callee": a.callee,
            "context": a.context}
    spec["instructions"] = build_instructions(a.lang, a.voice, a.task, a.callee, a.context,
                                              a.prompt_file.read_text() if a.prompt_file else None, a.formal)
    if a.no_call:
        log("instructions:\n" + spec["instructions"])
        (REPO_ROOT / "recordings").mkdir(exist_ok=True)
        asyncio.run(probe(spec, REPO_ROOT / "recordings"))
    elif a.number:
        asyncio.run(call(spec, a.audio, a.max_seconds, a.out_dir))
    else:
        ap.error("NUMBER or --no-call required")


if __name__ == "__main__":
    main()
