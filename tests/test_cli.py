import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from telaius import cli
from telaius.config import load_config, setting
from telaius.errors import PolicyDenied
from telaius.policy import Policy, in_quiet_hours, normalize_number


class PolicyTests(unittest.TestCase):
    def test_number_normalization(self):
        self.assertEqual(normalize_number("000 000-000"), "+48000000000")
        self.assertEqual(normalize_number("0048 000-000-000"), "+48000000000")

    def test_default_policy_denies_every_destination(self):
        with self.assertRaises(PolicyDenied):
            Policy().check_call("123456789", 30,
                                dt.datetime(2026, 11, 15, 12, tzinfo=ZoneInfo("Europe/Warsaw")))

    def test_quiet_hours_include_both_sides_of_midnight(self):
        zone = ZoneInfo("Europe/Warsaw")
        self.assertTrue(in_quiet_hours(dt.datetime(2026, 1, 1, 22, 0, tzinfo=zone)))
        self.assertTrue(in_quiet_hours(dt.datetime(2026, 1, 2, 8, 59, tzinfo=zone)))
        self.assertFalse(in_quiet_hours(dt.datetime(2026, 1, 2, 9, 0, tzinfo=zone)))

    def test_duration_is_clipped_to_hard_policy_limit(self):
        number, seconds, clipped = Policy({"allow_any": True}).check_call(
            "000000000", 600, dt.datetime(2026, 11, 15, 12, tzinfo=ZoneInfo("Europe/Warsaw")))
        self.assertEqual((number, seconds, clipped), ("+48000000000", 300, True))


class ConfigTests(unittest.TestCase):
    def test_environment_overrides_toml(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "telaius.toml"
            path.write_text('voice = "rex"\n')
            with mock.patch.dict(os.environ, {"TELAIUS_CONFIG": str(path), "TELAIUS_VOICE": "eve"}, clear=False):
                config = load_config()
                self.assertEqual(setting("voice", config=config), "eve")

    def test_policy_allowlist_can_come_from_environment(self):
        args = cli.parser().parse_args(["call", "000000000"])
        with tempfile.TemporaryDirectory() as tmp, \
             mock.patch.dict(os.environ, {"TELAIUS_ALLOWLIST": "+48000000000"}, clear=False):
            spec = cli._config_spec(args, {}, Path(tmp))
        self.assertEqual(spec.number, "+48000000000")


class CliTests(unittest.TestCase):
    def test_status_json_is_one_document(self):
        expected = {"busy": False, "ims": {"registered": True}}
        with mock.patch("telaius.cli.modem_status", return_value=expected), \
             mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
            self.assertEqual(cli.main(["status", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue()), expected)

    def test_policy_denial_has_documented_exit_code_and_shape(self):
        with mock.patch("telaius.cli.load_config", return_value={}), \
             mock.patch("sys.stdout", new_callable=__import__("io").StringIO) as out:
            self.assertEqual(cli.main(["--json", "call", "000000000"]), 4)
        self.assertEqual(json.loads(out.getvalue())["error"]["code"], "policy_denied")


if __name__ == "__main__":
    unittest.main()
