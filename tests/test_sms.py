import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo

from telaius.policy import Policy
from telaius.sms import (_pack_septets, _semi_octets, decode_deliver, encode_submit,
                         gsm7_septets, join_messages)


def deliver_pdu(sender, text, dcs=0, concat=None, alphanumeric=False):
    first = 0x40 if concat else 0
    if alphanumeric:
        address = _pack_septets(gsm7_septets(sender))
        address_length, toa = len(gsm7_septets(sender)), 0xD0
    else:
        address = _semi_octets(sender)
        address_length, toa = len(sender.lstrip("+")), 0x91
    udh = bytes((5, 0, 3, *concat)) if concat else b""
    if dcs == 0:
        values = gsm7_septets(text)
        if udh:
            header_septets = (len(udh) * 8 + 6) // 7
            user = _pack_septets(values, udh, header_septets * 7)
            length = header_septets + len(values)
        else:
            user, length = _pack_septets(values), len(values)
    elif dcs == 8:
        user = udh + text.encode("utf-16-be"); length = len(user)
    else:
        user = udh + bytes.fromhex(text); length = len(user)
    stamp = bytes.fromhex("62115121000000")
    body = bytes((first, address_length, toa)) + address + bytes((0, dcs)) + stamp + bytes((length,)) + user
    return (b"\0" + body).hex().upper()


class SubmitTests(unittest.TestCase):
    def test_short_gsm7_and_extension_table(self):
        parts = encode_submit("+48000000000", "Hello {}[]~\\^|€", reference=7)
        self.assertEqual((len(parts), parts[0].encoding, parts[0].total), (1, "gsm7", 1))
        self.assertEqual(len(gsm7_septets("{}[]~\\^|€")), 18)

    def test_gsm7_boundaries_count_extension_characters_as_two_septets(self):
        self.assertEqual(len(encode_submit("+48000000000", "a" * 160, reference=7)), 1)
        parts = encode_submit("+48000000000", "a" * 157 + "{}", reference=7)
        self.assertEqual([part.total for part in parts], [2, 2])
        self.assertIn("050003070201", parts[0].pdu)

    def test_ucs2_cyrillic_polish_and_emoji(self):
        for text in ("Привет", "ąćęłńóśźż", "hello 😀"):
            part = encode_submit("+48000000000", text, reference=8)[0]
            self.assertEqual(part.encoding, "ucs2")
            self.assertIn(text.encode("utf-16-be").hex().upper(), part.pdu)

    def test_ucs2_multipart_does_not_split_surrogate_pair(self):
        parts = encode_submit("+48000000000", "я" * 69 + "😀" + "я", reference=9)
        self.assertEqual(len(parts), 2)
        self.assertTrue(all(part.total == 2 for part in parts))


class DeliverTests(unittest.TestCase):
    def test_numeric_and_alphanumeric_sender(self):
        numeric = decode_deliver(deliver_pdu("+48000000000", "Hello {}"))
        alpha = decode_deliver(deliver_pdu("INFO", "Service", alphanumeric=True))
        self.assertEqual((numeric["sender"], numeric["text"]), ("+48000000000", "Hello {}"))
        self.assertEqual((alpha["sender"], alpha["text"]), ("INFO", "Service"))

    def test_ucs2_and_8bit(self):
        self.assertEqual(decode_deliver(deliver_pdu("+48000000000", "Zażółć", 8))["text"], "Zażółć")
        self.assertEqual(decode_deliver(deliver_pdu("+48000000000", "DEADBEEF", 4))["text"], "DEADBEEF")

    def test_multipart_join_orders_parts_and_reports_gaps(self):
        messages = []
        for identifier, seq, text in (("SM:3", 3, "C"), ("SM:1", 1, "A")):
            item = decode_deliver(deliver_pdu("+48000000000", text, 0, (12, 3, seq)))
            messages.append({**item, "id": identifier, "storage": "SM"})
        joined = join_messages(messages)[0]
        self.assertEqual((joined["text"], joined["ids"], joined["complete"], joined["missing"]),
                         ("AC", ["SM:1", "SM:3"], False, [2]))


class SmsPolicyTests(unittest.TestCase):
    def test_daily_limit_uses_journal(self):
        now = dt.datetime(2026, 11, 15, 12, tzinfo=ZoneInfo("Europe/Warsaw"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp); record = {"direction": "out", "time": now.isoformat()}
            (path / "sms.jsonl").write_text(json.dumps(record) + "\n")
            policy = Policy({"allowlist": ["+48000000000"], "sms_per_day": 2})
            self.assertEqual(policy.check_sms("+48000000000", path, now), "+48000000000")
            (path / "sms.jsonl").write_text((json.dumps(record) + "\n") * 2)
            with self.assertRaisesRegex(Exception, "daily SMS limit"):
                policy.check_sms("+48000000000", path, now)


if __name__ == "__main__":
    unittest.main()
