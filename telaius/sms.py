"""SMS PDU encoding, decoding, storage operations, and local message journal."""
from __future__ import annotations

import datetime as dt
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path

from telaius.config import calls_path, load_config, messages_path
from telaius.errors import ModemNotReady
from telaius.lock import ModemLock
from telaius.modem import IF_AT, AtChannel, close_modem, open_modem, open_port
from telaius.policy import from_config, normalize_number

GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./"
    "0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿"
    "abcdefghijklmnopqrstuvwxyzäöñüà"
)
GSM7_EXT = {"\f": 0x0A, "^": 0x14, "{": 0x28, "}": 0x29, "\\": 0x2F,
            "[": 0x3C, "~": 0x3D, "]": 0x3E, "|": 0x40, "€": 0x65}
GSM7_REVERSE = {char: index for index, char in enumerate(GSM7_BASIC)}
GSM7_EXT_REVERSE = {value: key for key, value in GSM7_EXT.items()}


@dataclass
class SmsPart:
    pdu: str
    tpdu_length: int
    encoding: str
    sequence: int
    total: int


def gsm7_septets(text):
    values = []
    for char in text:
        if char in GSM7_REVERSE and char != "\x1b":
            values.append(GSM7_REVERSE[char])
        elif char in GSM7_EXT:
            values.extend((0x1B, GSM7_EXT[char]))
        else:
            raise UnicodeEncodeError("gsm0338", char, 0, 1, "character is not in GSM-7")
    return values


def _pack_septets(values, prefix=b"", bit_offset=None):
    bits = len(prefix) * 8 if bit_offset is None else bit_offset
    output = bytearray(prefix)
    for value in values:
        for bit in range(7):
            position = bits + bit
            if position // 8 == len(output):
                output.append(0)
            if value & (1 << bit):
                output[position // 8] |= 1 << (position % 8)
        bits += 7
    return bytes(output)


def _unpack_septets(data, count, bit_offset=0):
    values = []
    for index in range(count):
        position = bit_offset + index * 7
        value = 0
        for bit in range(7):
            absolute = position + bit
            if absolute // 8 < len(data) and data[absolute // 8] & (1 << (absolute % 8)):
                value |= 1 << bit
        values.append(value)
    return values


def decode_gsm7(values):
    output, escaped = [], False
    for value in values:
        if escaped:
            output.append(GSM7_EXT_REVERSE.get(value, "�")); escaped = False
        elif value == 0x1B:
            escaped = True
        else:
            output.append(GSM7_BASIC[value] if value < len(GSM7_BASIC) else "�")
    if escaped:
        output.append("�")
    return "".join(output)


def _semi_octets(number):
    digits = number.lstrip("+")
    padded = digits + ("F" if len(digits) % 2 else "")
    return bytes(int(padded[i + 1] + padded[i], 16) for i in range(0, len(padded), 2))


def _decode_address(data, length, toa):
    if toa & 0x70 == 0x50:
        octets = (length + 1) // 2
        septets = octets * 8 // 7
        return decode_gsm7(_unpack_septets(data[:octets], septets)), octets
    octets = (length + 1) // 2
    digits = "".join(f"{byte:02X}"[1] + f"{byte:02X}"[0] for byte in data[:octets])[:length]
    return ("+" if toa & 0x90 == 0x90 else "") + digits, octets


def _split_gsm(text, limit):
    parts, current, used = [], [], 0
    for char in text:
        width = 2 if char in GSM7_EXT else 1
        if current and used + width > limit:
            parts.append("".join(current)); current, used = [], 0
        current.append(char); used += width
    if current or not parts:
        parts.append("".join(current))
    return parts


def encode_submit(number, text, reference=None):
    number = normalize_number(number)
    try:
        count = len(gsm7_septets(text)); encoding = "gsm7"
    except UnicodeEncodeError:
        count = len(text.encode("utf-16-be")) // 2; encoding = "ucs2"
    limit = 160 if encoding == "gsm7" else 70
    if count <= limit:
        texts = [text]
    elif encoding == "gsm7":
        texts = _split_gsm(text, 153)
    else:
        texts, current, units = [], [], 0
        for char in text:
            width = len(char.encode("utf-16-be")) // 2
            if current and units + width > 67:
                texts.append("".join(current)); current, units = [], 0
            current.append(char); units += width
        if current or not texts:
            texts.append("".join(current))
    reference = random.randrange(256) if reference is None else reference
    parts = []
    for sequence, piece in enumerate(texts, 1):
        multipart = len(texts) > 1
        udh = bytes((5, 0, 3, reference, len(texts), sequence)) if multipart else b""
        first = 0x11 | (0x40 if multipart else 0)
        address = _semi_octets(number)
        tpdu = bytearray((first, 0, len(number) - 1, 0x91)) + address + bytearray((0, 0x00 if encoding == "gsm7" else 0x08, 0xAA))
        if encoding == "gsm7":
            values = gsm7_septets(piece)
            if udh:
                header_septets = (len(udh) * 8 + 6) // 7
                payload = _pack_septets(values, udh, header_septets * 7)
                tpdu.append(header_septets + len(values))
            else:
                payload = _pack_septets(values)
                tpdu.append(len(values))
        else:
            payload = udh + piece.encode("utf-16-be")
            tpdu.append(len(payload))
        tpdu += payload
        pdu = bytes((0,)) + tpdu
        parts.append(SmsPart(pdu.hex().upper(), len(tpdu), encoding, sequence, len(texts)))
    return parts


def _timestamp(raw):
    values = [int(f"{byte:02X}"[1] + f"{byte:02X}"[0]) for byte in raw[:6]]
    zone_byte = raw[6] & 0xF7
    zone_digits = f"{zone_byte:02X}"[1] + f"{zone_byte:02X}"[0]
    quarters = int(zone_digits)
    if raw[6] & 0x08:
        quarters = -quarters
    zone = dt.timezone(dt.timedelta(minutes=quarters * 15))
    return dt.datetime(2000 + values[0], *values[1:], tzinfo=zone).isoformat()


def decode_deliver(pdu):
    data = bytes.fromhex(re.sub(r"\s+", "", pdu)); position = 0
    smsc_length = data[position]; position += 1 + smsc_length
    first = data[position]; position += 1
    if first & 0x03:
        raise ValueError("PDU is not SMS-DELIVER")
    address_length, toa = data[position], data[position + 1]; position += 2
    sender, consumed = _decode_address(data[position:], address_length, toa); position += consumed
    _, dcs = data[position], data[position + 1]; position += 2
    timestamp = _timestamp(data[position:position + 7]); position += 7
    udl = data[position]; position += 1
    user = data[position:]
    concat = None; header_octets = 0
    if first & 0x40:
        header_octets = user[0] + 1
        cursor = 1
        while cursor < header_octets:
            iei, length = user[cursor], user[cursor + 1]; value = user[cursor + 2:cursor + 2 + length]
            if iei == 0 and length == 3:
                concat = {"ref": value[0], "total": value[1], "seq": value[2]}
            cursor += 2 + length
    alphabet = (dcs >> 2) & 3
    if alphabet == 0:
        header_septets = (header_octets * 8 + 6) // 7
        text = decode_gsm7(_unpack_septets(user, udl - header_septets, header_septets * 7))
        encoding = "gsm7"
    elif alphabet == 2:
        text = user[header_octets:udl].decode("utf-16-be", errors="replace"); encoding = "ucs2"
    else:
        text = user[header_octets:udl].hex().upper(); encoding = "8bit"
    return {"sender": sender, "timestamp": timestamp, "text": text, "encoding": encoding, "concat": concat}


def join_messages(messages):
    grouped, output = {}, []
    for message in messages:
        concat = message.get("concat")
        if not concat:
            output.append({**message, "ids": [message["id"]]}); continue
        key = (message.get("storage"), message["sender"], concat["ref"], concat["total"])
        grouped.setdefault(key, []).append(message)
    for parts in grouped.values():
        parts.sort(key=lambda item: item["concat"]["seq"])
        expected = parts[0]["concat"]["total"]
        received = {item["concat"]["seq"] for item in parts}
        output.append({**parts[0], "text": "".join(item["text"] for item in parts),
                       "ids": [item["id"] for item in parts], "complete": received == set(range(1, expected + 1)),
                       "missing": sorted(set(range(1, expected + 1)) - received)})
    return sorted(output, key=lambda item: item.get("timestamp", ""), reverse=True)


class SmsService:
    def __init__(self, messages_dir):
        self.messages_dir = Path(messages_dir)

    def _channel(self):
        dev = open_modem()
        return dev, AtChannel(open_port(dev, IF_AT), echo=False)

    @staticmethod
    def _ok(response, command):
        if "ERROR" in response or "OK" not in response:
            raise ModemNotReady(f"{command} failed: {response.strip() or 'timeout'}")

    def send(self, number, text, policy, timeout=60):
        number = policy.check_sms(number, self.messages_dir)
        parts = encode_submit(number, text); refs = []; error = None
        dev, at = self._channel()
        try:
            self._ok(at.send("AT+CMGF=0"), "AT+CMGF=0")
            for part in parts:
                at.buf = ""
                at.port.write(f"AT+CMGS={part.tpdu_length}\r".encode())
                prompt = at.wait_for((">", "+CMS ERROR", "ERROR"), 3)
                if prompt != ">":
                    raise ModemNotReady(f"modem did not return the SMS input prompt: {at.buf.strip()}")
                at.buf = ""
                at.port.write((part.pdu + "\x1a").encode())
                if not at.wait_for(("+CMGS:", "+CMS ERROR", "ERROR"), timeout):
                    raise ModemNotReady("SMS submission timed out")
                match = re.search(r"\+CMGS:\s*(\d+)", at.buf)
                if not match:
                    raise ModemNotReady(f"SMS submission failed: {at.buf.strip()}")
                refs.append(int(match.group(1)))
        except Exception as exc:
            error = str(exc); raise
        finally:
            close_modem(dev, [IF_AT])
            self.journal("out", number, text, len(parts), refs, error)
        return {"ok": True, "parts": len(parts), "message_refs": refs,
                "warning": "message has more than 6 parts" if len(parts) > 6 else None}

    def journal(self, direction, number, text, parts, refs=None, error=None, **extra):
        self.messages_dir.mkdir(parents=True, exist_ok=True)
        record = {"direction": direction, "number": number, "text": text,
                  "time": dt.datetime.now(dt.timezone.utc).isoformat(), "parts": parts,
                  "message_refs": refs or [], "error": error}
        record.update(extra)
        with (self.messages_dir / "sms.jsonl").open("a", encoding="utf-8") as target:
            target.write(json.dumps(record, ensure_ascii=False) + "\n")

    def list(self, unread=False, all_storages=False):
        dev, at = self._channel(); messages = []
        try:
            self._ok(at.send("AT+CMGF=0"), "AT+CMGF=0")
            current = at.send("AT+CPMS?")
            storages = ["SM", "ME"] if all_storages else ([re.search(r'\+CPMS:\s*"([A-Z]+)"', current).group(1)] if re.search(r'\+CPMS:\s*"([A-Z]+)"', current) else ["SM"])
            for storage in storages:
                self._ok(at.send(f'AT+CPMS="{storage}"'), "AT+CPMS")
                response = at.send(f"AT+CMGL={0 if unread else 4}", wait=20)
                lines = [line.strip() for line in response.splitlines() if line.strip()]
                for index, line in enumerate(lines):
                    match = re.match(r"\+CMGL:\s*(\d+),(\d+),", line)
                    if match and index + 1 < len(lines):
                        metadata = {"id": f"{storage}:{match.group(1)}", "storage": storage,
                                    "index": int(match.group(1)), "unread": int(match.group(2)) == 0}
                        try:
                            decoded = decode_deliver(lines[index + 1])
                        except (IndexError, UnicodeError, ValueError) as error:
                            messages.append({**metadata, "error": str(error)})
                        else:
                            messages.append({**decoded, **metadata})
        finally:
            close_modem(dev, [IF_AT])
        joined = join_messages(messages)
        journal = self.messages_dir / "sms.jsonl"
        known = set()
        if journal.exists():
            for line in journal.read_text().splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("direction") == "in":
                    known.update(record.get("ids", []))
        for message in joined:
            if "error" not in message and not set(message["ids"]).issubset(known):
                self.journal("in", message["sender"], message["text"], len(message["ids"]),
                             ids=message["ids"], received_at=message["timestamp"])
        return joined

    def read(self, identifier):
        storage, index = _identifier(identifier)
        dev, at = self._channel()
        try:
            self._ok(at.send("AT+CMGF=0"), "AT+CMGF=0"); self._ok(at.send(f'AT+CPMS="{storage}"'), "AT+CPMS")
            response = at.send(f"AT+CMGR={index}", wait=10)
            lines = [line.strip() for line in response.splitlines() if line.strip()]
            pdu = next((lines[i + 1] for i, line in enumerate(lines[:-1]) if line.startswith("+CMGR:")), None)
            if not pdu: raise ModemNotReady(f"SMS {identifier} was not found")
            return {**decode_deliver(pdu), "id": identifier}
        finally: close_modem(dev, [IF_AT])

    def delete(self, identifiers=None, read=False, all_messages=False):
        targets = identifiers or []
        if read or all_messages:
            targets = [identifier for message in self.list() if all_messages or not message.get("unread") for identifier in message["ids"]]
        dev, at = self._channel()
        try:
            for identifier in targets:
                storage, index = _identifier(identifier); self._ok(at.send(f'AT+CPMS="{storage}"'), "AT+CPMS")
                self._ok(at.send(f"AT+CMGD={index}"), "AT+CMGD")
        finally: close_modem(dev, [IF_AT])
        return {"ok": True, "deleted": targets}


def _identifier(value):
    match = re.fullmatch(r"(SM|ME):(\d+)", str(value).upper())
    if not match: raise ValueError("SMS ID must be SM:<index> or ME:<index>")
    return match.group(1), int(match.group(2))


def send_sms(number, text, config=None):
    config = load_config() if config is None else config
    service = SmsService(messages_path(config))
    with ModemLock("send SMS"):
        return service.send(number, text, from_config(config, calls_path(config)))


def list_sms(unread=False, all_storages=False, config=None):
    config = load_config() if config is None else config
    with ModemLock("list SMS"):
        return SmsService(messages_path(config)).list(unread, all_storages)


def read_sms(identifier, config=None):
    config = load_config() if config is None else config
    with ModemLock("read SMS"):
        return SmsService(messages_path(config)).read(identifier)


def delete_sms(identifiers=None, read=False, all_messages=False, config=None):
    config = load_config() if config is None else config
    with ModemLock("delete SMS"):
        return SmsService(messages_path(config)).delete(identifiers, read, all_messages)
