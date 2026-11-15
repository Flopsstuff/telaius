"""One phone call, start to finish: dial, bridge the audio to xAI, report, save.

A session is observable and interruptible from outside while it runs, which is what the CLI and
the MCP server need: every state change is written to the call's status.json, and `cancel()` (or
the `cancel` flag file, or SIGTERM to a worker) ends the call the same way a bot goodbye does —
hang up, ask the model for a result, save the record.

Interrupts and errors are not allowed to lose the call: whatever happens after the callee
answered, the session still hangs up, asks for a result and writes result.json, with
`final_status` saying how it went.

The event loop stays free. Blocking AT commands run through asyncio.to_thread (serialized with
the AT watcher by a lock); call audio keeps its own reader/writer threads, because the modem
needs a steady 20 ms cadence that loop scheduling cannot promise.
"""
import asyncio
import base64
import json
import os
import threading
import time
from pathlib import Path

import usb.core
import websockets

from telaius import realtime
from telaius.callstore import CANCEL_FILE, call_dir, save_call, write_spec, write_status
from telaius.config import load_config
from telaius.errors import ConfigError
from telaius.logs import log
from telaius.modem import IF_AT, IF_PCM, AtChannel, PcmAligner, close_modem, open_modem, open_port
from telaius.prompts import LANG_HINT, bot_name
from telaius.policy import from_config

RELAY_RATE = 16000
GREETING_WAIT = 2.0  # seconds of silence after answer before the bot greets first
ANSWER_WAIT = 60.0  # seconds to wait for the callee to pick up
RELAY_AUDIO_WAIT = 3.0  # seconds to wait for the first audio frames from the relay
POLL = 0.2  # in-call loop tick: how fast a cancel or a hangup is noticed
STATUS_BEAT = 5.0  # how often status.json gets a fresh elapsed while nothing else changes

# what status.json can say: `ended` and `failed` are terminal, final_status tells them apart
STATES = ("created", "dialing", "ringing", "in_call", "reporting", "ended", "failed")
FINAL_STATUSES = ("completed", "no_answer", "busy", "cancelled", "failed")


def stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


class CallSession:
    """Runs the call described by `spec` inside `directory` (created if missing)."""

    def __init__(self, spec, directory, on_state=None):
        self.spec = spec
        self.dir = Path(directory)
        self.on_state = on_state
        self.state = "created"
        self.end_reason = "interrupted"
        self.final_status = "failed"
        self.rate = None
        self.record = {}
        self._cancelled = threading.Event()
        self._cancel_reason = "cancelled"
        self._status = {}
        self._dev = self._at = self._pcm = None
        self._threads, self._tasks = [], []
        self._stop = threading.Event()
        self._at_lock = threading.Lock()
        self._rx, self._tx = bytearray(), bytearray()
        self._playback = realtime.Playback()
        self.ev = {}
        self._t_answer = None
        self._t0 = time.monotonic()

    # --- outside view -------------------------------------------------------

    def cancel(self, reason="cancelled"):
        """End the call early. Safe from another thread or a signal handler."""
        if not self._cancelled.is_set():
            self._cancel_reason = reason
            self._cancelled.set()
            log.info("cancel requested (%s)", reason)

    @property
    def cancelled(self):
        return self._cancelled.is_set()

    def _cancel_wanted(self):
        """True if cancel() was called or another process dropped the cancel flag file."""
        if not self._cancelled.is_set() and (self.dir / CANCEL_FILE).exists():
            try:
                reason = (self.dir / CANCEL_FILE).read_text().strip()
            except OSError:
                reason = ""
            self.cancel(reason or "cancelled")
        return self._cancelled.is_set()

    def _touch_status(self):
        """Keep elapsed moving in status.json, so a watcher can tell a call from a stuck call."""
        self._status["elapsed"] = round(time.monotonic() - self._t0, 2)
        write_status(self.dir, self._status)

    def _set_state(self, state, **fields):
        self.state = state
        self._status.update(state=state, elapsed=round(time.monotonic() - self._t0, 2), **fields)
        write_status(self.dir, self._status)
        if self.on_state:
            self.on_state(self)

    # --- the call -----------------------------------------------------------

    async def run(self):
        """Place the call and return its record (what goes into result.json)."""
        if not self.spec.number:
            raise ConfigError("a call needs a phone number")
        self._t0 = time.monotonic()
        self.dir.mkdir(parents=True, exist_ok=True)
        write_spec(self.dir, self.spec)
        self.record = {"call_id": self.dir.name, "number": self.spec.number, "lang": self.spec.lang,
                       "voice": self.spec.voice, "bot_name": bot_name(self.spec.lang, self.spec.voice),
                       "audio": "relay" if self.spec.audio == "auto" else self.spec.audio,
                       "audio_requested": self.spec.audio, "task": self.spec.task, "callee": self.spec.callee,
                       "context": self.spec.context, "started_at": stamp(), "answered": False, "network": None}
        self._status = {"call_id": self.dir.name, "pid": os.getpid(), "started_at": self.record["started_at"],
                        "answered_at": None, "ended_at": None, "final_status": None,
                        "end_reason": None, "error": None}
        self._set_state("created")
        try:
            return await self._call()
        except BaseException as e:
            self.end_reason = f"error: {e}"
            self._set_state("failed", final_status="failed", end_reason=self.end_reason,
                            error=f"{type(e).__name__}: {e}", ended_at=stamp())
            raise

    def _open_ports(self):
        dev = open_modem()
        try:
            return dev, AtChannel(open_port(dev, IF_AT)), open_port(dev, IF_PCM)
        except BaseException:
            close_modem(dev, [IF_AT, IF_PCM])
            raise

    def _at_send(self, cmd, wait=3):
        with self._at_lock:
            return self._at.send(cmd, wait)

    async def _send(self, cmd, wait=3):
        return await asyncio.to_thread(self._at_send, cmd, wait)

    async def _call(self):
        self._dev, self._at, self._pcm = await asyncio.to_thread(self._open_ports)
        try:
            self.rate = RELAY_RATE if self.spec.audio in ("auto", "relay") else await asyncio.to_thread(self._at.pcm_rate)
            self.record["rate"] = self.rate
            self._frame_len = self.rate * 2 // 50  # 20 ms
            self.ev = realtime.new_state()
            log.info("audio path: %s, PCM rate: %d Hz, output: %s", self.spec.audio, self.rate, self.dir)
            async with realtime.connect() as ws:
                await realtime.configure(ws, self.rate, self.spec.instructions(), self.spec.voice,
                                         LANG_HINT[self.spec.lang], self.spec.tools())
                try:
                    await self._converse(ws)
                except (KeyboardInterrupt, asyncio.CancelledError):
                    self.cancel("cancelled")
                    self.end_reason, self.final_status = self._cancel_reason, "cancelled"
                except Exception as e:
                    log.exception("the call failed: %s", e)
                    self.end_reason, self.final_status = f"error: {e}", "failed"
                finally:
                    await self._hangup()
                    await self._report(ws)
        finally:
            self._release_modem()
        return await self._save()

    async def _wait_answer(self):
        keys = ["VOICE CALL: BEGIN", "NO CARRIER", "BUSY", "NO ANSWER", "VOICE CALL: END"]

        def poll_once():
            with self._at_lock:
                return self._at.wait_for(keys, 0.5)

        deadline = time.monotonic() + ANSWER_WAIT
        while time.monotonic() < deadline:
            if self._cancel_wanted():
                return None
            got = await asyncio.to_thread(poll_once)
            if got:
                return got
        return None

    async def _converse(self, ws):
        spec = self.spec
        loop = asyncio.get_running_loop()
        mic_q = asyncio.Queue()
        aligner = [PcmAligner(self.rate)]  # replaced when the audio path is restarted

        def reader():
            while not self._stop.is_set():
                try:
                    chunk = self._pcm.read()
                except (OSError, usb.core.USBError) as e:
                    log.warning("pcm read error: %s", e)
                    time.sleep(0.05)
                    continue
                chunk = aligner[0].feed(chunk)
                if chunk:
                    self._rx.extend(chunk)
                    loop.call_soon_threadsafe(mic_q.put_nowait, chunk)

        def writer():
            # The modem expects a steady 20 ms cadence; send silence when the bot is quiet.
            t0 = time.monotonic()
            while not self._stop.is_set():
                frame_len = self._frame_len
                frame = self._playback.pop(frame_len)
                frame += bytes(frame_len - len(frame))
                self._tx.extend(frame)
                try:
                    self._pcm.write(frame, timeout_ms=200)
                except (OSError, usb.core.USBError):
                    pass
                t0 += 0.02
                delay = t0 - time.monotonic()
                if delay > 0:
                    time.sleep(delay)

        def at_watch():
            while not self._stop.is_set():
                with self._at_lock:
                    self._at.poll(100)
                    if "VOICE CALL: END" in self._at.buf or "NO CARRIER" in self._at.buf:
                        self.ev["remote_hangup"] = True
                time.sleep(0.05)

        async def uplink():
            # Batch 100 ms of call audio per append.
            pending = bytearray()
            while True:
                pending.extend(await mic_q.get())
                if len(pending) >= self._frame_len * 5:
                    await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                              "audio": base64.b64encode(bytes(pending)).decode()}))
                    pending.clear()

        def start(fn):
            self._threads.append(threading.Thread(target=fn, daemon=True))
            self._threads[-1].start()

        if self._cancel_wanted():  # cancelled between creating the call and dialing
            log.info("cancelled before dialing")
            self.end_reason, self.final_status = self._cancel_reason, "cancelled"
            return
        self._set_state("dialing")
        await self._send("ATE0")
        await self._send("AT+CLCC=0")
        # A run that died mid-call leaves the modem in a call with USB audio armed; start clean.
        await self._send("AT+CHUP")
        await self._send("AT+CPCMREG=0")
        await self._send(f"ATD{spec.number};", wait=10)
        self._set_state("ringing")
        got = await self._wait_answer()
        if got != "VOICE CALL: BEGIN":
            if self.cancelled:
                log.info("cancelled before the callee answered")
                self.end_reason, self.final_status = self._cancel_reason, "cancelled"
            else:
                log.info("call not answered: %s", got)
                self.end_reason = f"not_answered: {got or 'timeout'}"
                self.final_status = "busy" if got == "BUSY" else "no_answer"
            return
        self.record["answered"], self._t_answer = True, time.monotonic()
        self._set_state("in_call", answered_at=stamp())
        # Audio can only be moved to USB once the call is active. Start pumping if4 first:
        # firmware B06 drops USB audio for the call if the host does not consume it right away,
        # and the relay starts its session when our frames arrive.
        start(reader)
        start(writer)
        if spec.audio in ("auto", "relay"):
            for _ in range(int(RELAY_AUDIO_WAIT / 0.1)):
                await asyncio.sleep(0.1)
                if self._rx:
                    break
            else:
                log.warning("no call audio from the relay after %.0f s (is telaius-relay running on the module?)",
                            RELAY_AUDIO_WAIT)
                if spec.audio == "auto":
                    self.rate = await asyncio.to_thread(self._at.pcm_rate)
                    self._frame_len = self.rate * 2 // 50
                    aligner[0] = PcmAligner(self.rate)
                    self.record["audio"] = "usb"
                    self.record["rate"] = self.rate
                    await realtime.configure(ws, self.rate, self.spec.instructions(), self.spec.voice,
                                             LANG_HINT[self.spec.lang], self.spec.tools())
                    await self._send("AT+CPCMREG=1")
                    log.info("relay unavailable; continuing the call through USB at %d Hz", self.rate)
        else:
            await self._send("AT+CPCMREG=1")
            # At 16 kHz on VoLTE the modem's capture sometimes fails at call start and no audio
            # arrives; re-arming the USB audio path can recover it.
            for attempt in range(3):
                await asyncio.sleep(1.5)
                if self._rx:
                    break
                log.info("no call audio from the modem yet, re-arming USB audio (attempt %d)", attempt + 1)
                await self._send("AT+CPCMREG=0")
                aligner[0] = PcmAligner(self.rate)
                await self._send("AT+CPCMREG=1")
        start(at_watch)
        cpsi = await self._send("AT+CPSI?")  # LTE here means VoLTE, GSM means CSFB
        self.record["network"] = next((ln.split(":", 1)[1].split(",")[0].strip()
                                       for ln in cpsi.splitlines() if ln.startswith("+CPSI:")), None)
        self.ev["t_call"] = time.monotonic()
        self._tasks = [asyncio.create_task(realtime.handle_events(ws, self._playback, self.ev, self.rate)),
                       asyncio.create_task(uplink())]
        # Let the callee say "алло" first, like a person would; otherwise the greeting overlaps it and
        # gets cut by barge-in. If they speak, server VAD answers when they stop; after 2 s of silence
        # the bot starts on its own.
        t_wait = time.monotonic()
        while time.monotonic() - t_wait < GREETING_WAIT and not self.ev.get("speech_seen"):
            await asyncio.sleep(0.05)
        if self.ev.get("speech_seen"):
            log.info("callee spoke first; the bot greets when they stop")
        else:
            log.info("no hello within %.0f s; the bot greets first", GREETING_WAIT)
            await ws.send(json.dumps({"type": "response.create"}))
        self.final_status = "completed"
        beat = time.monotonic() + STATUS_BEAT
        while time.monotonic() - self._t_answer < spec.max_seconds:
            await asyncio.sleep(POLL)
            if time.monotonic() >= beat:
                self._touch_status()
                beat += STATUS_BEAT
            if self._cancel_wanted():
                log.info("ending the call on request (%s)", self._cancel_reason)
                self.end_reason, self.final_status = self._cancel_reason, "cancelled"
                return
            if self.ev.get("remote_hangup"):
                log.info("remote hung up")
                self.end_reason = "remote_hangup"
                return
            if self.ev.get("end_requested") and self._playback.pending() == 0:
                await asyncio.sleep(0.6)  # let the last frames leave the modem
                log.info("hanging up after bot goodbye")
                self.end_reason = "bot_goodbye"
                return
        log.info("max duration reached")
        self.end_reason = "max_duration"

    async def _hangup(self):
        self._stop.set()

        def finish():
            for t in self._threads:
                t.join(1)
            try:
                self._at_send("AT+CHUP")
                if self.record.get("audio") == "usb":
                    self._at_send("AT+CPCMREG=0")
            except (OSError, usb.core.USBError, TimeoutError) as e:
                log.warning("could not hang up cleanly: %s", e)

        await asyncio.to_thread(finish)

    async def _report(self, ws):
        if self.record.get("answered") and not self.ev.get("result"):
            self._set_state("reporting", end_reason=self.end_reason)
            try:
                await realtime.request_report(ws, self.ev, self.end_reason)
            except (websockets.ConnectionClosed, OSError) as e:
                log.warning("could not request the call result: %s", e)
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _release_modem(self):
        if self._dev is not None:
            close_modem(self._dev, [IF_AT, IF_PCM])
            self._dev = self._at = self._pcm = None

    async def _save(self):
        result = self.ev.get("result")
        if not result:
            # e.g. the callee hung up during the greeting and the model sent no report
            result = {"outcome": "failed" if self.end_reason == "remote_hangup" else "unknown",
                      "summary": f"Итог от бота не получен (завершение: {self.end_reason})."}
        self.record.update({
            "ended_at": stamp(),
            "duration_s": round(time.monotonic() - self._t_answer, 1) if self._t_answer else 0,
            "end_reason": self.end_reason, "final_status": self.final_status,
            "outcome": result["outcome"], "summary": result["summary"],
            "data": result.get("data"), "unresolved": result.get("unresolved", []),
            "transcript": self.ev.get("transcript", []),
        })
        await asyncio.to_thread(save_call, self.dir, self.rate, self._rx, self._tx, self.record)
        self._set_state("failed" if self.final_status == "failed" else "ended",
                        final_status=self.final_status, end_reason=self.end_reason,
                        ended_at=self.record["ended_at"])
        log.info("result: %s, outcome=%s, %s s -> %s", self.end_reason, result["outcome"],
                 self.record["duration_s"], self.dir)
        if result["summary"]:
            log.info("summary: %s", result["summary"])
        return self.record


async def run_call(spec, out_root, on_state=None, policy=None):
    """One foreground call in a fresh directory under `out_root`. Returns the record."""
    spec, clipped = (policy or from_config(load_config(), out_root)).apply_call(spec)
    if clipped:
        log.warning("max_seconds limited to %g by policy", spec.max_seconds)
    return await CallSession(spec, call_dir(spec.callee, spec.number, out_root), on_state=on_state).run()
