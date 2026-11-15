"""Whole calls against the fake modem and the fake xAI: answered, not answered, cancelled, failed."""
import asyncio
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from telaius.callspec import CallSpec
from telaius.callstore import call_dir, read_status, request_cancel
from telaius.errors import PolicyDenied, XaiError
from telaius.session import CallSession, run_call
from tests.fakes import FakeAtPort, FakeModem, FakePcmPort, FakeXai, patch_modem, patch_xai

NUMBER = "+48000000000"  # the fake modem never dials anything


class CallBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "calls"
        self.root.mkdir()
        self.patch_env({"TELAIUS_RUNTIME_DIR": str(Path(tmp.name) / "run"),
                        "TELAIUS_ALLOW_ANY": "true",
                        "TELAIUS_POLICY_QUIET_START": "00:00",
                        "TELAIUS_POLICY_QUIET_END": "00:00"})
        # the real waits are tuned for a human on the phone; tests do not have the time
        for name, value in (("GREETING_WAIT", 0.05), ("RELAY_AUDIO_WAIT", 0.5),
                            ("ANSWER_WAIT", 1.0), ("POLL", 0.05)):
            self.patch(f"telaius.session.{name}", value)
        quiet = logging.getLogger("telaius")
        quiet.disabled = True
        self.addCleanup(setattr, quiet, "disabled", False)

    def patch(self, target, value):
        patcher = mock.patch(target, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def patch_env(self, values):
        patcher = mock.patch.dict(os.environ, values)
        patcher.start()
        self.addCleanup(patcher.stop)

    def spec(self, **over):
        return CallSpec(**{"number": NUMBER, "callee": "Tester", "task": "Проверить ядро",
                           "audio": "relay", "max_seconds": 20, **over})


class AnsweredCallTests(CallBase):
    async def test_auto_falls_back_to_usb_when_relay_is_silent(self):
        at = FakeAtPort(rate=8000)

        class RelayThenUsb(FakePcmPort):
            def read(self, timeout_ms=100):
                if "AT+CPCMREG=1" not in at.commands:
                    time.sleep(0.02)
                    return b""
                return super().read(timeout_ms)

        modem = patch_modem(self, FakeModem(at=at, pcm=RelayThenUsb(8000)))
        xai = patch_xai(self, FakeXai(rate=8000))

        record = await run_call(self.spec(audio="auto"), self.root)

        self.assertEqual((record["audio"], record["rate"]), ("usb", 8000))
        self.assertIn("AT+CPCMREG=1", modem.at.commands)
        self.assertEqual(sum('"type": "session.update"' in item for item in xai.sent), 2)

    async def test_bot_goodbye_saves_record_status_and_files(self):
        modem = patch_modem(self, FakeModem())
        patch_xai(self, FakeXai())

        record = await run_call(self.spec(), self.root)

        self.assertTrue(record["answered"])
        self.assertEqual(record["end_reason"], "bot_goodbye")
        self.assertEqual(record["final_status"], "completed")
        self.assertEqual((record["outcome"], record["summary"]), ("done", "Записал"))
        self.assertEqual([t["role"] for t in record["transcript"]], ["bot"])
        self.assertEqual(record["network"], "LTE")

        directory = self.root / record["call_id"]
        for name in ("spec.json", "status.json", "result.json", "transcript.txt"):
            self.assertTrue((directory / name).exists(), name)
        status = read_status(directory)
        self.assertEqual((status["state"], status["final_status"]), ("ended", "completed"))
        self.assertEqual(status["end_reason"], "bot_goodbye")
        self.assertEqual(status["pid"], os.getpid())
        self.assertTrue(status["answered_at"] and status["ended_at"])
        self.assertIn("AT+CHUP", modem.at.commands)
        self.assertTrue(modem.closed)
        self.assertTrue(modem.pcm.written, "bot audio never reached the modem")

    async def test_remote_hangup_asks_the_model_for_the_result(self):
        patch_modem(self, FakeModem(FakeAtPort(hangup_after=0.4)))
        xai = patch_xai(self, FakeXai(goodbye=False, outcome="partial", summary="Перезвонить"))

        record = await run_call(self.spec(), self.root)

        self.assertEqual(record["end_reason"], "remote_hangup")
        self.assertEqual(record["final_status"], "completed")
        self.assertEqual((record["outcome"], record["summary"]), ("partial", "Перезвонить"))
        self.assertTrue(any("The phone call has ended" in sent for sent in xai.sent))

    async def test_max_duration_ends_the_call(self):
        patch_modem(self, FakeModem())
        patch_xai(self, FakeXai(goodbye=False))

        record = await run_call(self.spec(max_seconds=0.5), self.root)

        self.assertEqual(record["end_reason"], "max_duration")
        self.assertEqual(record["final_status"], "completed")

    async def test_elapsed_keeps_moving_while_the_call_runs(self):
        patch_modem(self, FakeModem())
        patch_xai(self, FakeXai(goodbye=False))
        self.patch("telaius.session.STATUS_BEAT", 0.1)
        directory = call_dir("Tester", NUMBER, self.root)
        session = CallSession(self.spec(max_seconds=1.5), directory)
        seen = []

        async def watch():
            while session.state != "in_call":
                await asyncio.sleep(0.02)
            for _ in range(3):
                await asyncio.sleep(0.2)
                seen.append(read_status(directory)["elapsed"])

        await asyncio.gather(session.run(), watch())

        self.assertEqual(seen, sorted(seen))
        self.assertLess(seen[0], seen[-1])

    async def test_the_event_loop_runs_while_at_commands_block(self):
        patch_modem(self, FakeModem(FakeAtPort(answer_delay=0.8)))
        patch_xai(self, FakeXai())
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        tick = asyncio.create_task(ticker())
        await run_call(self.spec(), self.root)
        tick.cancel()

        # dialling alone blocks a thread for ~0.8 s; on a blocked loop the ticker would barely run
        self.assertGreater(ticks, 20)


class UnansweredCallTests(CallBase):
    async def test_the_reason_the_callee_did_not_answer_is_recorded(self):
        for answer, final_status, end_reason in (
            ("NO CARRIER", "no_answer", "not_answered: NO CARRIER"),
            ("BUSY", "busy", "not_answered: BUSY"),
            (None, "no_answer", "not_answered: timeout"),
        ):
            with self.subTest(answer=answer):
                patch_modem(self, FakeModem(FakeAtPort(answer=answer)))
                patch_xai(self, FakeXai())

                record = await run_call(self.spec(), self.root)

                self.assertFalse(record["answered"])
                self.assertEqual(record["final_status"], final_status)
                self.assertEqual(record["end_reason"], end_reason)
                self.assertEqual(record["outcome"], "unknown")
                self.assertEqual(read_status(self.root / record["call_id"])["state"], "ended")


class CancelTests(CallBase):
    async def test_cancel_during_the_conversation_ends_it_politely(self):
        modem = patch_modem(self, FakeModem())
        patch_xai(self, FakeXai(goodbye=False))
        directory = call_dir("Tester", NUMBER, self.root)
        session = CallSession(self.spec(), directory)

        async def cancel_from_outside():
            while session.state != "in_call":
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.1)
            request_cancel(directory, "cancelled: test")  # the flag file another process would write

        record, _ = await asyncio.gather(session.run(), cancel_from_outside())

        self.assertEqual(record["end_reason"], "cancelled: test")
        self.assertEqual(record["final_status"], "cancelled")
        self.assertEqual(record["outcome"], "done", "the bot was still asked for a result")
        self.assertIn("AT+CHUP", modem.at.commands)
        status = read_status(directory)
        self.assertEqual((status["state"], status["final_status"]), ("ended", "cancelled"))

    async def test_cancel_method_is_enough(self):
        patch_modem(self, FakeModem())
        patch_xai(self, FakeXai(goodbye=False))
        session = CallSession(self.spec(), call_dir("Tester", NUMBER, self.root))

        async def cancel_soon():
            while session.state != "in_call":
                await asyncio.sleep(0.02)
            session.cancel("cancelled: owner")

        record, _ = await asyncio.gather(session.run(), cancel_soon())

        self.assertEqual(record["end_reason"], "cancelled: owner")
        self.assertEqual(record["final_status"], "cancelled")

    async def test_a_call_cancelled_before_dialling_never_dials(self):
        modem = patch_modem(self, FakeModem())
        patch_xai(self, FakeXai())
        directory = call_dir("Tester", NUMBER, self.root)
        directory.mkdir(parents=True)
        (directory / "cancel").write_text("cancelled: changed my mind")

        record = await CallSession(self.spec(), directory).run()

        self.assertFalse(record["answered"])
        self.assertEqual(record["final_status"], "cancelled")
        self.assertEqual(record["end_reason"], "cancelled: changed my mind")
        self.assertFalse([c for c in modem.at.commands if c.startswith("ATD")])


class FailedCallTests(CallBase):
    async def test_foreground_entry_enforces_policy_outside_the_cli(self):
        with mock.patch.dict(os.environ, {"TELAIUS_ALLOW_ANY": "", "TELAIUS_ALLOWLIST": ""}):
            with self.assertRaises(PolicyDenied):
                await run_call(self.spec(), self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_an_unusable_xai_session_fails_the_call(self):
        patch_modem(self, FakeModem())
        patch_xai(self, FakeXai(configure_error=True))
        directory = call_dir("Tester", NUMBER, self.root)

        with self.assertRaises(XaiError):
            await CallSession(self.spec(), directory).run()

        status = read_status(directory)
        self.assertEqual((status["state"], status["final_status"]), ("failed", "failed"))
        self.assertIn("could not configure", status["end_reason"])
        self.assertFalse((directory / "result.json").exists())

    async def test_an_error_after_the_answer_still_saves_a_record(self):
        modem = patch_modem(self, FakeModem())
        patch_xai(self, FakeXai(goodbye=False))
        directory = call_dir("Tester", NUMBER, self.root)
        real_send = CallSession._send

        async def send(session, cmd, wait=3):
            if cmd == "AT+CPSI?":  # the modem disappears mid-call
                raise OSError("the modem went away")
            return await real_send(session, cmd, wait)

        self.patch("telaius.session.CallSession._send", send)
        record = await CallSession(self.spec(), directory).run()

        self.assertTrue(record["answered"])
        self.assertEqual(record["final_status"], "failed")
        self.assertEqual(record["end_reason"], "error: the modem went away")
        self.assertTrue((directory / "result.json").exists())
        status = read_status(directory)
        self.assertEqual((status["state"], status["final_status"]), ("failed", "failed"))
        self.assertTrue(modem.closed)


if __name__ == "__main__":
    unittest.main()
