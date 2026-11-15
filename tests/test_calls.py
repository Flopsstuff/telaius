"""The call directory, the modem lock and the background worker."""
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from telaius import worker
from telaius.callspec import CallSpec
from telaius.callstore import (call_dir, read_spec, read_status, request_cancel,
                               write_spec, write_status)
from telaius.errors import CallNotFound, ConfigError, ModemBusy
from telaius.lock import ModemLock, lock_path
from telaius.session import stamp
from tests.fakes import FakeAtPort, FakeModem, FakeXai, patch_modem, patch_xai

NUMBER = "+48000000000"
REPO_ROOT = Path(__file__).resolve().parents[1]


class TmpRootTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "calls"
        self.root.mkdir()
        patcher = mock.patch.dict(os.environ, {"TELAIUS_RUNTIME_DIR": str(self.tmp / "run")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def spec(self, **over):
        return CallSpec(**{"number": NUMBER, "callee": "Tester", "task": "Проверить ядро",
                           "audio": "relay", "max_seconds": 20, **over})

    def new_dir(self):
        directory = call_dir("Tester", NUMBER, self.root)
        directory.mkdir(parents=True)
        return directory


class CallSpecTests(unittest.TestCase):
    def test_round_trip_through_spec_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = CallSpec(number=NUMBER, lang="pl", voice="eve", task="Zapisać się", formal=True,
                            audio="relay", max_seconds=90)
            write_spec(Path(tmp), spec)
            self.assertEqual(read_spec(Path(tmp)), spec)

    def test_a_spec_is_checked_when_it_is_built(self):
        for bad in ({"lang": "xx"}, {"audio": "bluetooth"}, {"max_seconds": 0}):
            with self.subTest(**bad):
                with self.assertRaises(ConfigError):
                    CallSpec(number=NUMBER, **bad)

    def test_unknown_fields_are_rejected(self):
        with self.assertRaises(ConfigError):
            CallSpec.from_dict({"number": NUMBER, "ringtone": "loud"})

    def test_instructions_follow_the_prompt_files(self):
        self.assertIn("Проверить", CallSpec(number=NUMBER, task="Проверить").instructions())


class CallDirTests(TmpRootTests):
    def test_the_name_says_who_was_called_and_when(self):
        directory = call_dir("Маша, коллега", NUMBER, self.root, started=time.time())
        self.assertRegex(directory.name, r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_masha$")

    def test_two_calls_in_the_same_second_get_their_own_directories(self):
        started = time.time()
        first = call_dir("Tester", NUMBER, self.root, started=started)
        first.mkdir()
        second = call_dir("Tester", NUMBER, self.root, started=started)
        self.assertNotEqual(first, second)
        self.assertEqual(second.name, first.name + "-2")



class StatusTests(TmpRootTests):
    def test_status_is_rewritten_whole(self):
        directory = self.new_dir()
        write_status(directory, {"state": "dialing", "pid": os.getpid()})
        write_status(directory, {"state": "in_call", "pid": os.getpid()})
        self.assertEqual(read_status(directory)["state"], "in_call")
        self.assertFalse(list(directory.glob("*.tmp")))

    def test_a_missing_call_is_reported_as_such(self):
        with self.assertRaises(CallNotFound):
            read_status(self.root / "2026-01-01_00-00-00_nobody")

    def test_a_dead_worker_is_reported_as_failed(self):
        directory = self.new_dir()
        dead = subprocess.Popen([sys.executable, "-c", ""])
        dead.wait()
        write_status(directory, {"state": "in_call", "pid": dead.pid, "final_status": None})

        status = read_status(directory)

        self.assertEqual((status["state"], status["final_status"]), ("failed", "failed"))
        self.assertEqual(status["end_reason"], "error: worker died")

    def test_a_finished_call_keeps_its_result_after_the_worker_is_gone(self):
        directory = self.new_dir()
        dead = subprocess.Popen([sys.executable, "-c", ""])
        dead.wait()
        write_status(directory, {"state": "ended", "pid": dead.pid, "final_status": "completed"})
        self.assertEqual(read_status(directory)["final_status"], "completed")


class RequestCancelTests(TmpRootTests):
    def test_a_running_call_gets_the_flag_file(self):
        directory = self.new_dir()
        write_status(directory, {"state": "in_call", "pid": os.getpid()})

        request_cancel(directory, "cancelled: owner")

        self.assertEqual((directory / "cancel").read_text(), "cancelled: owner")

    def test_a_finished_call_is_left_alone(self):
        directory = self.new_dir()
        write_status(directory, {"state": "ended", "pid": os.getpid(), "final_status": "completed"})

        request_cancel(directory)

        self.assertFalse((directory / "cancel").exists())


class ModemLockTests(TmpRootTests):
    def test_the_second_holder_is_told_who_has_the_modem(self):
        with ModemLock("call 2026-01-01_12-00-00_tester"):
            with self.assertRaises(ModemBusy) as busy:
                ModemLock("at console").acquire()
        self.assertIn("call 2026-01-01_12-00-00_tester", str(busy.exception))
        self.assertIn(str(os.getpid()), str(busy.exception))

    def test_the_lock_is_free_again_after_release(self):
        ModemLock("first").acquire().release()
        ModemLock("second").acquire().release()

    def test_the_runtime_directory_can_be_moved(self):
        self.assertEqual(lock_path(), self.tmp / "run" / "modem.lock")

    def test_a_dead_holder_does_not_keep_the_modem(self):
        child = subprocess.run(
            [sys.executable, "-c", "from telaius.lock import ModemLock; ModemLock('child').acquire(); print('held')"],
            cwd=REPO_ROOT, env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
            capture_output=True, text=True, timeout=60)
        self.assertEqual(child.stdout.strip(), "held", child.stderr)

        ModemLock("after the crash").acquire().release()  # the kernel dropped the dead flock


class WorkerTests(TmpRootTests):
    def setUp(self):
        super().setUp()
        for name, value in (("GREETING_WAIT", 0.05), ("RELAY_AUDIO_WAIT", 0.5),
                            ("ANSWER_WAIT", 1.0), ("POLL", 0.05)):
            patcher = mock.patch(f"telaius.session.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)
        quiet = logging.getLogger("telaius")
        quiet.disabled = True
        self.addCleanup(setattr, quiet, "disabled", False)

    def detached(self):
        """A call directory prepared by start_call, without a real worker process."""
        with mock.patch("telaius.worker.subprocess.Popen") as popen:
            popen.return_value.pid = os.getpid()
            directory = worker.start_call(self.spec(), self.root)
        return directory, popen

    def test_start_call_returns_at_once_with_the_call_prepared(self):
        directory, popen = self.detached()

        self.assertEqual(read_spec(directory).number, NUMBER)
        status = read_status(directory)
        self.assertEqual((status["state"], status["call_id"]), ("created", directory.name))
        self.assertEqual(status["pid"], os.getpid())
        command = popen.call_args[0][0]
        self.assertEqual(command[:2], [sys.executable, "-m"])
        self.assertEqual(command[2:], ["telaius.worker", str(directory)])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_a_worker_that_dies_before_its_session_starts_reads_as_dead(self):
        popen = subprocess.Popen
        with mock.patch("telaius.worker.subprocess.Popen",
                        lambda _, **kw: popen([sys.executable, "-c", "raise SystemExit(1)"], **kw)):
            directory = worker.start_call(self.spec(), self.root)

        deadline = time.monotonic() + 10
        while read_status(directory)["state"] == "created" and time.monotonic() < deadline:
            time.sleep(0.05)

        status = read_status(directory)
        self.assertEqual((status["state"], status["final_status"]), ("failed", "failed"))
        self.assertEqual(status["end_reason"], "error: worker died")

    def test_start_call_refuses_while_the_modem_is_busy(self):
        with ModemLock("call 2026-01-01_12-00-00_tester"):
            with self.assertRaises(ModemBusy):
                worker.start_call(self.spec(), self.root)
        self.assertEqual(list(self.root.iterdir()), [], "no call was created")

    def test_the_worker_runs_the_call_it_is_given(self):
        patch_modem(self, FakeModem())
        patch_xai(self, FakeXai())
        directory, _ = self.detached()

        self.assertEqual(worker.run_dir(directory), 0)

        status = read_status(directory)
        self.assertEqual((status["state"], status["final_status"]), ("ended", "completed"))
        self.assertEqual(json.loads((directory / "result.json").read_text())["outcome"], "done")

    def test_an_unanswered_call_tells_the_caller_through_the_exit_code(self):
        patch_modem(self, FakeModem(FakeAtPort(answer="NO CARRIER")))
        patch_xai(self, FakeXai())
        directory, _ = self.detached()

        self.assertEqual(worker.run_dir(directory), 10)
        self.assertEqual(read_status(directory)["final_status"], "no_answer")

    def test_a_worker_that_loses_the_modem_race_fails_its_own_call(self):
        patch_modem(self, FakeModem())
        patch_xai(self, FakeXai())
        directory, _ = self.detached()

        with ModemLock("call 2026-01-01_12-00-00_tester"):
            self.assertEqual(worker.run_dir(directory), 1)

        status = read_status(directory)
        self.assertEqual((status["state"], status["final_status"]), ("failed", "failed"))
        self.assertIn("call 2026-01-01_12-00-00_tester", status["end_reason"])

    def test_a_call_cancelled_before_the_worker_starts_is_not_placed(self):
        modem = patch_modem(self, FakeModem())
        patch_xai(self, FakeXai())
        directory, _ = self.detached()
        request_cancel(directory, "cancelled: owner")

        self.assertEqual(worker.run_dir(directory), 0)

        self.assertFalse([c for c in modem.at.commands if c.startswith("ATD")])
        self.assertEqual(read_status(directory)["final_status"], "cancelled")


class StampTests(unittest.TestCase):
    def test_timestamps_carry_the_offset(self):
        self.assertRegex(stamp(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}$")


if __name__ == "__main__":
    unittest.main()
