import time
import unittest

from telaius.modem import AtChannel


class ChunkedPort:
    def __init__(self, chunks, delay=0):
        self.chunks = list(chunks)
        self.delay = delay
        self.writes = []

    def read(self, timeout_ms=100):
        time.sleep(min(self.delay, timeout_ms / 1000))
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, data):
        self.writes.append(data)


class AtChannelTests(unittest.TestCase):
    def test_reply_can_arrive_in_parts(self):
        port = ChunkedPort([b"\r\nManufacturer: SIMCOM\r\n", b"Model: SIM7600\r\n", b"OK\r\n"], delay=0.01)

        reply = AtChannel(port, echo=False).send("ATI", wait=0.1)

        self.assertEqual(port.writes, [b"ATI\r"])
        self.assertIn("Model: SIM7600", reply)
        self.assertTrue(reply.rstrip().endswith("OK"))

    def test_urc_does_not_hide_final_result(self):
        port = ChunkedPort([b"\r\nManufacturer: SIMCOM\r\n", b'OK\r\n+CMTI: "SM",1\r\n', b"+CSQ: 20,99\r\n"])

        reply = AtChannel(port, echo=False).send("ATI", wait=0.02)

        self.assertIn('+CMTI: "SM",1', reply)
        self.assertIn("OK", reply)
        self.assertEqual(port.chunks, [b"+CSQ: 20,99\r\n"])


if __name__ == "__main__":
    unittest.main()
