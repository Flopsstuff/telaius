"""The MCP server, through a real MCP client in the same process, on the fake modem and xAI."""
import asyncio
import json
import logging
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

try:
    from mcp import Client
except ImportError:  # the optional extra telaius[mcp] is not installed
    Client = None

from telaius.callspec import CallSpec, check_result_schema
from telaius.errors import ConfigError
from telaius.lock import ModemLock
from telaius.prompts import build_instructions, tools
from tests.fakes import FakeAtPort, FakeModem, FakeXai, patch_modem, patch_xai
from tests.test_sms import deliver_pdu

NUMBER = "+48000000000"
SCHEMA = {"type": "object", "properties": {"time": {"type": "string"}, "topic": {"type": "string"}},
          "required": ["time", "topic"]}


class ResultSchemaTests(unittest.TestCase):
    def test_the_schema_becomes_data_of_both_result_tools(self):
        for tool in tools(SCHEMA):
            params = tool["parameters"]
            self.assertEqual(params["properties"]["data"]["properties"]["time"]["type"], ["string", "null"])
            self.assertIn("data", params["required"])
            self.assertEqual(params["properties"]["unresolved"]["items"], {"type": "string"})

    def test_unresolved_is_there_without_a_schema_too(self):
        params = tools()[0]["parameters"]
        self.assertNotIn("data", params["properties"])
        self.assertIn("unresolved", params["required"])

    def test_the_prompt_says_not_to_invent_data(self):
        with_schema = build_instructions("en", "rex", task="Ask", result_schema=SCHEMA)
        self.assertIn("Never invent", with_schema)
        self.assertNotIn("Never invent", build_instructions("en", "rex", task="Ask"))

    def test_invalid_schemas_are_refused(self):
        deep = {"type": "object"}
        for _ in range(8):
            deep = {"type": "object", "properties": {"x": deep}}
        for bad in ({"type": "string"}, [], {"type": "object", "properties": {"x": {"type": "date"}}},
                    {"type": "object", "properties": {"x": {"type": "string", "description": "y" * 5000}}},
                    {"type": "object", "required": ["missing"]}, deep):
            with self.subTest(bad=str(bad)[:60]):
                with self.assertRaises(ConfigError):
                    check_result_schema(bad)

    def test_constraints_reach_the_instructions(self):
        spec = CallSpec(number=NUMBER, task="Ask", constraints="Never share the address")
        self.assertIn("Never share the address", spec.instructions())


@unittest.skipIf(Client is None, "the mcp extra is not installed")
class McpServerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        patcher = mock.patch.dict(os.environ, {"TELAIUS_RUNTIME_DIR": str(self.tmp / "run"),
                                               "TELAIUS_CONFIG": str(self.tmp / "telaius.toml"),
                                               "TELAIUS_ENV_FILE": str(self.tmp / ".env"),
                                               "TELAIUS_CALLS_DIR": str(self.tmp / "calls"),
                                               "TELAIUS_MESSAGES_DIR": str(self.tmp / "messages"),
                                               "TELAIUS_ALLOWLIST": NUMBER,
                                               "TELAIUS_POLICY_QUIET_START": "00:00",
                                               "TELAIUS_POLICY_QUIET_END": "00:00"})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (("GREETING_WAIT", 0.05), ("RELAY_AUDIO_WAIT", 0.5),
                            ("ANSWER_WAIT", 1.0), ("POLL", 0.05)):
            patcher = mock.patch(f"telaius.session.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("telaius", "mcp"):
            quiet = logging.getLogger(name)
            quiet.disabled = True
            self.addCleanup(setattr, quiet, "disabled", False)
        self.workers = []
        self.addCleanup(lambda: [w.join(30) for w in self.workers])
        self._run_workers_in_threads()
        from telaius import mcp_server
        self.server = mcp_server.server

    def _run_workers_in_threads(self):
        """make_call starts `python -m telaius.worker DIR`; here the same run_dir runs in a thread,
        so it sees the fakes. Signal handlers belong to the main thread, so the session runs bare."""
        import subprocess

        from telaius import worker

        real_popen = subprocess.Popen

        def popen(command, **kwargs):
            if command[1:3] != ["-m", "telaius.worker"]:  # ffmpeg, adb
                return real_popen(command, **kwargs)
            thread = threading.Thread(target=worker.run_dir, args=(command[-1],))
            self.workers.append(thread)
            thread.start()
            return SimpleNamespace(pid=os.getpid())

        for target, value in (("telaius.worker.subprocess.Popen", popen),
                              ("telaius.worker._run", lambda session: session.run())):
            patcher = mock.patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tools(self, *calls):
        """Run `(name, arguments)` tool calls in order through one client; returns their results."""
        async def go():
            async with Client(self.server) as client:
                return [await client.call_tool(name, args) for name, args in calls]
        return asyncio.run(go())

    def call(self, name, args=None):
        return self.tools((name, args or {}))[0]

    @staticmethod
    def value(result):
        assert not result.is_error, result.content[0].text
        return json.loads(result.content[0].text)

    def error(self, result):
        self.assertTrue(result.is_error, "expected a tool error")
        return result.content[0].text

    def wait_until_over(self, call_id):
        for _ in range(20):
            status = self.value(self.call("get_call_status", {"call_id": call_id, "wait_seconds": 5}))
            if status["state"] in ("ended", "failed"):
                return status
        self.fail("the call did not end")

    def test_all_eight_tools_are_listed(self):
        async def go():
            async with Client(self.server) as client:
                return {tool.name for tool in (await client.list_tools()).tools}
        self.assertEqual(asyncio.run(go()), {"get_modem_status", "make_call", "get_call_status", "get_call_result",
                                             "end_call", "list_calls", "send_sms", "list_sms"})

    def test_a_call_with_a_result_schema_returns_filled_data(self):
        patch_modem(self, FakeModem())
        xai = patch_xai(self, FakeXai(data={"time": "10:00", "topic": None}, unresolved=["topic"]))

        started = self.value(self.call("make_call", {"phone_number": NUMBER, "task": "Ask when he is free",
                                                     "language": "en", "result_schema": SCHEMA}))
        self.assertEqual(started["status"], "created")
        status = self.wait_until_over(started["call_id"])
        result = self.value(self.call("get_call_result", {"call_id": started["call_id"],
                                                          "include_transcript": True}))

        self.assertEqual((status["state"], status["final_status"]), ("ended", "completed"))
        self.assertNotIn("pid", status)
        self.assertEqual(result["outcome"], "done")
        self.assertEqual(result["data"], {"time": "10:00", "topic": None})
        self.assertEqual(result["unresolved"], ["topic"])
        self.assertEqual(result["end_reason"], "bot_goodbye")
        self.assertTrue(Path(result["transcript_file"]).exists())
        self.assertTrue(result["transcript"])
        update = json.loads(next(s for s in xai.sent if '"session.update"' in s))
        self.assertIn("data", update["session"]["tools"][0]["parameters"]["properties"])

    def test_end_call_stops_a_running_call_and_is_harmless_after(self):
        patch_modem(self, FakeModem())
        patch_xai(self, FakeXai(goodbye=False))
        call_id = self.value(self.call("make_call", {"phone_number": NUMBER, "task": "Talk",
                                                     "language": "ru", "max_seconds": 60}))["call_id"]
        for _ in range(50):
            if self.value(self.call("get_call_status", {"call_id": call_id, "wait_seconds": 1}))["state"] == "in_call":
                break

        self.assertTrue(self.value(self.call("end_call", {"call_id": call_id}))["ok"])
        status = self.wait_until_over(call_id)
        again = self.value(self.call("end_call", {"call_id": call_id}))

        self.assertEqual(status["final_status"], "cancelled")
        self.assertEqual((again["ok"], again["was_running"]), (True, False))
        self.assertEqual(self.value(self.call("get_call_result", {"call_id": call_id}))["end_reason"],
                         "cancelled: end_call")

    def test_a_number_off_the_allowlist_is_policy_denied(self):
        text = self.error(self.call("make_call", {"phone_number": "+48111111111", "task": "x", "language": "pl"}))
        self.assertIn("policy_denied", text)
        self.assertFalse((self.tmp / "calls").exists() and any((self.tmp / "calls").iterdir()))

    def test_a_busy_modem_is_modem_busy(self):
        with ModemLock("call 2026-01-01_12-00-00_other"):
            text = self.error(self.call("make_call", {"phone_number": NUMBER, "task": "x", "language": "ru"}))
            status = self.value(self.call("get_modem_status"))
        self.assertIn("modem_busy", text)
        self.assertIn("2026-01-01_12-00-00_other", text)
        self.assertTrue(status["busy"])

    def test_an_invalid_schema_is_invalid_input(self):
        text = self.error(self.call("make_call", {"phone_number": NUMBER, "task": "x", "language": "ru",
                                                  "result_schema": {"type": "array"}}))
        self.assertIn("invalid_input", text)

    def test_out_of_range_arguments_are_refused(self):
        self.error(self.call("make_call", {"phone_number": NUMBER, "task": "x", "language": "de"}))
        self.error(self.call("get_call_status", {"call_id": "x", "wait_seconds": 61}))

    def test_an_unknown_call_is_call_not_found(self):
        for name in ("get_call_status", "get_call_result", "end_call"):
            with self.subTest(name):
                self.assertIn("call_not_found", self.error(self.call(name, {"call_id": "2026-01-01_00-00-00_nobody"})))
        self.assertIn("call_not_found", self.error(self.call("get_call_result", {"call_id": "../etc"})))

    def test_list_calls_is_brief_and_newest_first(self):
        patch_modem(self, FakeModem(FakeAtPort(answer="NO CARRIER")))
        patch_xai(self, FakeXai())
        call_id = self.value(self.call("make_call", {"phone_number": NUMBER, "task": "Ask", "callee": "Anna",
                                                     "language": "pl"}))["call_id"]
        self.wait_until_over(call_id)

        calls = self.value(self.call("list_calls", {"limit": 5}))

        self.assertEqual([c["call_id"] for c in calls["calls"]], [call_id])
        self.assertEqual((calls["calls"][0]["final_status"], calls["calls"][0]["callee"]), ("no_answer", "Anna"))

    def test_send_and_list_sms(self):
        inbox = [(3, deliver_pdu("+48222222222", "Cześć", dcs=8))]
        modem = patch_modem(self, FakeModem(FakeAtPort(inbox=inbox)), modules=("telaius.sms",))

        sent, listed = self.tools(("send_sms", {"phone_number": NUMBER, "text": "Привет"}),
                                  ("list_sms", {"unread_only": True}))

        self.assertEqual(self.value(sent), {"ok": True, "parts": 1, "message_refs": [1]})
        self.assertEqual([m["text"] for m in self.value(listed)["messages"]], ["Cześć"])
        self.assertTrue(any(c.startswith("AT+CMGS=") for c in modem.at.commands))
        self.assertIn("policy_denied", self.error(self.call("send_sms", {"phone_number": "+48111111111",
                                                                         "text": "x"})))


if __name__ == "__main__":
    unittest.main()
