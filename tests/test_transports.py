import os
import pty
import struct
import termios
import unittest
from unittest import mock

from telaius.modem import TtyPort, _transport
from telaius.qmi import CdcWdmPort, Qmi, open_qmi_port, tlv


class TtyPortTests(unittest.TestCase):
    def setUp(self):
        self.master, slave = pty.openpty()
        self.port = TtyPort(os.ttyname(slave))
        os.close(slave)

    def tearDown(self):
        self.port.close()
        os.close(self.master)

    def test_raw_binary_round_trip(self):
        payload = b"\x00\r\n\x03\xffraw"
        os.write(self.master, payload)
        self.assertEqual(self.port.read(100), payload)

        self.assertEqual(self.port.write(payload, 100), len(payload))
        self.assertEqual(os.read(self.master, len(payload)), payload)

    def test_timeout_returns_empty_bytes(self):
        self.assertEqual(self.port.read(10), b"")

    def test_raw_termios_flags(self):
        attrs = termios.tcgetattr(self.port.fd)
        self.assertFalse(attrs[0] & termios.ICRNL)
        self.assertFalse(attrs[1] & termios.OPOST)
        self.assertFalse(attrs[3] & (termios.ECHO | termios.ICANON))
        self.assertEqual(attrs[2] & termios.CSIZE, termios.CS8)


class TransportSelectionTests(unittest.TestCase):
    def test_forced_transport(self):
        with mock.patch.dict(os.environ, {"TELAIUS_TRANSPORT": "usb"}):
            self.assertEqual(_transport(), "usb")
        with mock.patch.dict(os.environ, {"TELAIUS_TRANSPORT": "tty"}):
            self.assertEqual(_transport(), "tty")

    def test_auto_prefers_tty_only_when_both_ports_exist(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "telaius.modem.glob.glob", side_effect=[["at"], ["pcm"]]
        ):
            self.assertEqual(_transport(), "tty")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch(
            "telaius.modem.glob.glob", side_effect=[["at"], []]
        ):
            self.assertEqual(_transport(), "usb")

    def test_invalid_transport_is_rejected(self):
        with mock.patch.dict(os.environ, {"TELAIUS_TRANSPORT": "serial"}):
            with self.assertRaises(ValueError):
                _transport()


class CdcWdmPortTests(unittest.TestCase):
    def test_read_returns_one_available_message(self):
        read_fd, write_fd = os.pipe()
        try:
            with mock.patch("telaius.qmi.os.open", return_value=os.dup(read_fd)):
                port = CdcWdmPort("/dev/fake-cdc-wdm")
            os.write(write_fd, b"qmux")
            self.assertEqual(port.read(0.1), b"qmux")
            port.close()
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_write_requires_the_whole_message(self):
        read_fd, write_fd = os.pipe()
        try:
            with mock.patch("telaius.qmi.os.open", return_value=os.dup(write_fd)):
                port = CdcWdmPort("/dev/fake-cdc-wdm")
            port.write(b"qmux")
            self.assertEqual(os.read(read_fd, 4), b"qmux")
            port.close()
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_write_retries_eagain(self):
        read_fd, write_fd = os.pipe()
        try:
            with mock.patch("telaius.qmi.os.open", return_value=os.dup(write_fd)):
                port = CdcWdmPort("/dev/fake-cdc-wdm")
            with mock.patch("telaius.qmi.os.write", side_effect=[BlockingIOError(), 4]) as write:
                port.write(b"qmux", timeout=0.1)
            self.assertEqual(write.call_count, 2)
            port.close()
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_qmi_auto_prefers_cdc_wdm(self):
        sentinel = object()
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch("telaius.qmi.os.path.exists", return_value=True), \
             mock.patch("telaius.qmi.CdcWdmPort", return_value=sentinel):
            self.assertIs(open_qmi_port(), sentinel)

    def test_qmi_forced_usb_keeps_raw_fallback(self):
        sentinel = object()
        with mock.patch.dict(os.environ, {"TELAIUS_TRANSPORT": "usb"}), \
             mock.patch("telaius.qmi.UsbQmiPort", return_value=sentinel) as usb_port:
            self.assertIs(open_qmi_port(), sentinel)
            usb_port.assert_called_once_with(None)


class QmiFramingTests(unittest.TestCase):
    def test_request_writes_and_parses_one_complete_qmux_message(self):
        result_tlv = tlv(0x02, struct.pack("<HH", 0, 0))
        response_sdu = struct.pack("<BBHH", 0x01, 1, 0x27, len(result_tlv)) + result_tlv
        response = struct.pack("<BHBBB", 1, 5 + len(response_sdu), 0, 0, 0) + response_sdu

        class FakeTransport:
            def __init__(self):
                self.writes = []
                self.responses = [response]

            def write(self, data, timeout):
                self.writes.append((data, timeout))

            def read(self, timeout):
                return self.responses.pop(0) if self.responses else b""

        qmi = Qmi.__new__(Qmi)
        qmi.transport = FakeTransport()
        qmi.txn = 1
        qmi.pending = []
        reply = qmi.request(0, 0, 0x27, timeout=0.1)

        frame, timeout = qmi.transport.writes[0]
        self.assertEqual(frame[0], 1)
        self.assertEqual(struct.unpack_from("<H", frame, 1)[0], len(frame) - 1)
        self.assertEqual(frame[4:6], b"\x00\x00")
        self.assertEqual(timeout, 0.1)
        self.assertEqual(Qmi.result(reply), (0, 0))


if __name__ == "__main__":
    unittest.main()
