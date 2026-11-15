"""A modem and an xAI Realtime server that exist only in the test process.

The modem fake sits at the port level (read/write bytes), so the real AtChannel parsing, the PCM
aligner and the reader/writer threads are the ones under test. The xAI fake replaces
realtime.connect() and answers the few event types a call depends on.
"""
import asyncio
import base64
import json
import threading
import time


class FakeAtPort:
    """Answers AT commands like the SIM7600 does, including the call progress codes."""

    def __init__(self, answer="VOICE CALL: BEGIN", answer_delay=0.1, hangup_after=None, rate=8000, inbox=()):
        self.answer, self.answer_delay = answer, answer_delay
        self.hangup_after = hangup_after  # seconds after the answer: the callee hangs up
        self.rate = rate
        self.inbox = list(inbox)  # SMS on the SIM: (index, deliver PDU)
        self.sms_refs = 0
        self.commands = []
        self._out = bytearray()
        self._due = []  # (monotonic deadline, text)
        self._lock = threading.Lock()

    def _reply(self, text):
        with self._lock:
            self._out.extend(f"\r\n{text}\r\n".encode())

    def _later(self, delay, text):
        with self._lock:
            self._due.append((time.monotonic() + delay, text))

    def write(self, data, timeout_ms=1000):
        cmd = data.decode(errors="replace").strip()
        self.commands.append(cmd)
        if data.endswith(b"\x1a"):
            self.sms_refs += 1
            self._reply(f"+CMGS: {self.sms_refs}\r\nOK")
        elif cmd.startswith("AT+CMGS="):
            with self._lock:
                self._out.extend(b"\r\n> ")
        elif cmd.startswith("AT+CPMS?"):
            self._reply(f'+CPMS: "SM",{len(self.inbox)},30,"SM",{len(self.inbox)},30,"SM",{len(self.inbox)},30\r\nOK')
        elif cmd.startswith("AT+CMGL="):
            listed = "".join(f"+CMGL: {i},0,,{len(pdu) // 2 - 1}\r\n{pdu}\r\n" for i, pdu in self.inbox)
            self._reply(listed + "OK")
        elif cmd.startswith("AT+CPCMFRM?"):
            self._reply(f"+CPCMFRM: {1 if self.rate == 16000 else 0}\r\nOK")
        elif cmd.startswith("AT+CPSI?"):
            self._reply("+CPSI: LTE,Online,260-06,0x1234,123456,100,EUTRAN-BAND3\r\nOK")
        elif cmd.startswith("ATD"):
            self._reply("OK")
            if self.answer:
                self._later(self.answer_delay, self.answer)
            if self.answer == "VOICE CALL: BEGIN" and self.hangup_after is not None:
                self._later(self.answer_delay + self.hangup_after, "VOICE CALL: END")
        else:
            self._reply("OK")
        return len(data)

    def read(self, timeout_ms=100):
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            with self._lock:
                now = time.monotonic()
                ready = [t for t in self._due if t[0] <= now]
                if ready:
                    self._due = [t for t in self._due if t[0] > now]
                    for _, text in ready:
                        self._out.extend(f"\r\n{text}\r\n".encode())
                if self._out:
                    out, self._out = bytes(self._out), bytearray()
                    return out
            if time.monotonic() >= deadline:
                return b""
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

    def close(self):
        pass


class FakePcmPort:
    """Call audio: silence in, writes accepted, paced like the real 20 ms frames."""

    def __init__(self, rate=16000):
        self.frame = bytes(rate * 2 // 50)
        self.written = bytearray()

    def read(self, timeout_ms=100):
        time.sleep(0.02)
        return self.frame

    def write(self, data, timeout_ms=1000):
        self.written.extend(data)

    def close(self):
        pass


class FakeModem:
    """What open_modem() returns; open_port() hands out its two ports."""

    def __init__(self, at=None, pcm=None, rate=16000):
        self.at = at or FakeAtPort()
        self.pcm = pcm or FakePcmPort(rate)
        self.closed = False


def patch_modem(test, modem, modules=("telaius.session",)):
    """Point the modem calls of `modules` (calls; add telaius.sms for SMS) at `modem` for `test`."""
    from unittest import mock

    from telaius.modem import IF_AT

    for module in modules:
        for name, value in (("open_modem", lambda: modem),
                            ("open_port", lambda dev, ifnum: dev.at if ifnum == IF_AT else dev.pcm),
                            ("close_modem", lambda dev, ifnums: setattr(dev, "closed", True))):
            patcher = mock.patch(f"{module}.{name}", value)
            patcher.start()
            test.addCleanup(patcher.stop)
    return modem


class FakeXai:
    """The xAI Realtime server: greets, talks, and reports through the bot's tools.

    `goodbye` makes the bot call end_call after its first turn; without it the bot keeps talking
    and only answers the report_result request the session sends after the call ended.
    """

    AUDIO_MS = 200

    def __init__(self, rate=16000, goodbye=True, outcome="done", summary="Записал",
                 configure_error=False, data=None, unresolved=()):
        self.rate, self.goodbye = rate, goodbye
        self.outcome, self.summary = outcome, summary
        self.data, self.unresolved = data, list(unresolved)
        self.configure_error = configure_error
        self.sent = []
        self.turns = 0
        self._q = None

    # realtime.connect() replacement
    def __call__(self):
        return self

    async def __aenter__(self):
        self._q = asyncio.Queue()
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self._q.get()

    async def recv(self):
        return await self._q.get()

    async def _put(self, event):
        await self._q.put(json.dumps(event))

    async def send(self, raw):
        self.sent.append(raw)
        ev = json.loads(raw)
        t = ev["type"]
        if t == "session.update":
            if self.configure_error:
                await self._put({"type": "error", "error": "transcription not available"})
            else:
                await self._put({"type": "session.updated"})
        elif t == "response.create":
            self.turns += 1
            await self._speak("Здравствуйте! Это Telaius.")
            if self.goodbye:
                await self._tool("end_call", self.outcome, self.summary)
        elif t == "conversation.item.create":
            content = ev["item"].get("content") or [{}]
            text = content[0].get("text", "")
            if "The phone call has ended" in text:
                await self._tool("report_result", self.outcome, self.summary)

    async def _speak(self, text):
        audio = bytes(self.rate * 2 * self.AUDIO_MS // 1000)
        await self._put({"type": "response.output_audio.delta",
                         "delta": base64.b64encode(audio).decode()})
        await self._put({"type": "response.output_audio_transcript.done",
                         "item_id": f"bot-{self.turns}", "transcript": text})

    async def _tool(self, name, outcome, summary):
        args = {"outcome": outcome, "summary": summary, "unresolved": self.unresolved}
        if self.data is not None:
            args["data"] = self.data
        await self._put({"type": "response.function_call_arguments.done", "name": name,
                         "call_id": f"call-{name}", "arguments": json.dumps(args)})


def patch_xai(test, xai):
    """Replace realtime.connect() with `xai` for the duration of `test`."""
    from unittest import mock

    patcher = mock.patch("telaius.realtime.connect", xai)
    patcher.start()
    test.addCleanup(patcher.stop)
    return xai
