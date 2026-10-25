"""SIM7600 access over raw libusb.

macOS has no serial driver for the SIM7600 composite device (1e0e:9001), so the vendor
interfaces are claimed directly: if2 carries AT commands, if4 carries call audio as raw
PCM (s16le mono, 8 or 16 kHz depending on AT+CPCMFRM) once AT+CPCMREG=1 is sent.
"""
import array
import glob
import sys
import time

import usb.backend.libusb1
import usb.core
import usb.util

VID, PID = 0x1E0E, 0x9001
IF_AT = 2
IF_PCM = 4

FINAL_RESULTS = ("OK", "ERROR", "NO CARRIER", "BUSY", "NO ANSWER")


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _backend():
    for pattern in ("/opt/homebrew/lib/libusb-1.0*.dylib", "/usr/local/lib/libusb-1.0*.dylib"):
        for path in glob.glob(pattern):
            backend = usb.backend.libusb1.get_backend(find_library=lambda _: path)
            if backend:
                return backend
    return None  # let pyusb search the default library path (Linux)


def open_modem():
    dev = usb.core.find(idVendor=VID, idProduct=PID, backend=_backend())
    if dev is None:
        sys.exit(f"modem {VID:04x}:{PID:04x} not found on USB")
    return dev


def close_modem(dev, ifnums):
    for ifnum in ifnums:
        usb.util.release_interface(dev, ifnum)
        if ifnum in getattr(dev, "_telaius_detached", ()):
            try:
                dev.attach_kernel_driver(ifnum)
            except (NotImplementedError, usb.core.USBError):
                pass
    usb.util.dispose_resources(dev)


class UsbPort:
    """One claimed vendor interface with its bulk IN/OUT endpoints."""

    def __init__(self, dev, ifnum):
        intf = dev.get_active_configuration()[(ifnum, 0)]
        try:
            kernel_active = dev.is_kernel_driver_active(ifnum)
        except NotImplementedError:
            kernel_active = False
        if kernel_active:
            dev.detach_kernel_driver(ifnum)
            detached = getattr(dev, "_telaius_detached", set())
            detached.add(ifnum)
            dev._telaius_detached = detached
        usb.util.claim_interface(dev, ifnum)
        self.ep_in = self._bulk(intf, usb.util.ENDPOINT_IN)
        self.ep_out = self._bulk(intf, usb.util.ENDPOINT_OUT)
        # Some Qualcomm serial functions only talk once DTR/RTS are asserted (CDC SET_CONTROL_LINE_STATE).
        try:
            dev.ctrl_transfer(0x21, 0x22, 0x03, ifnum, None, timeout=500)
        except usb.core.USBError:
            pass

    @staticmethod
    def _bulk(intf, direction):
        return usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == direction
            and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)

    def read(self, timeout_ms=100):
        """Return available bytes, or b"" on timeout."""
        try:
            return bytes(self.ep_in.read(4096, timeout=timeout_ms))
        except usb.core.USBTimeoutError:
            return b""

    def write(self, data, timeout_ms=1000):
        self.ep_out.write(data, timeout=timeout_ms)


class PcmAligner:
    """Keeps the if4 byte stream on 16-bit sample boundaries.

    A stray or lost byte (a leftover from the previous session at call start, or a partial USB write
    later) shifts every following sample by one byte and turns speech into loud noise. Audio is
    released in 30 ms windows, each checked first: if it is noisy but the same bytes shifted by one are
    smooth (speech is smooth), one byte is dropped before releasing it. Nothing misaligned gets out,
    at the cost of up to 30 ms of extra latency. Silence decides nothing.
    """

    NOISY = 1500  # mean |sample delta| of misaligned audio is in the thousands, speech in the hundreds

    def __init__(self, rate):
        self.win = rate * 2 * 30 // 1000  # bytes per 30 ms window (even)
        self.buf = b""

    @staticmethod
    def _roughness(data):
        s = array.array("h", data[:len(data) // 2 * 2])
        return sum(abs(s[i] - s[i - 1]) for i in range(1, len(s))) / max(len(s) - 1, 1)

    def feed(self, chunk):
        self.buf += chunk
        out = bytearray()
        # keep one spare byte so the shifted view of a window is always available
        while len(self.buf) > self.win:
            here = self._roughness(self.buf[:self.win])
            if here > self.NOISY:
                shifted = self._roughness(self.buf[1:self.win + 1])
                if shifted * 4 < here:
                    log(f"PCM stream misaligned by 1 byte (roughness {here:.0f} vs {shifted:.0f}), realigning")
                    self.buf = self.buf[1:]
                    continue
            out += self.buf[:self.win]
            self.buf = self.buf[self.win:]
        return bytes(out)


class AtChannel:
    """AT command channel; logs every line and keeps the text received since the last command in `buf`."""

    def __init__(self, port, echo=True):
        self.port, self.echo, self.buf = port, echo, ""

    def poll(self, timeout_ms=100):
        text = self.port.read(timeout_ms).decode(errors="replace")
        if self.echo:
            for line in text.splitlines():
                if line.strip():
                    log(f"AT< {line.strip()}")
        self.buf += text
        return text

    def send(self, cmd, wait=3):
        """Send a command and read until a final result code or `wait` seconds."""
        if self.echo:
            log(f"AT> {cmd}")
        self.buf = ""
        self.port.write((cmd + "\r").encode())
        end = time.time() + wait
        while time.time() < end:
            self.poll()
            lines = self.buf.strip().splitlines()
            if lines and (lines[-1].strip() in FINAL_RESULTS or "+CME ERROR" in lines[-1]):
                break
        return self.buf

    def wait_for(self, keys, seconds):
        """Poll until one of `keys` shows up in `buf`; return it, or None on timeout."""
        end = time.time() + seconds
        while time.time() < end:
            self.poll()
            for key in keys:
                if key in self.buf:
                    return key
        return None

    def pcm_rate(self):
        """Current USB PCM sample rate. The modem refuses CPCMFRM=1 -> 0 without a reset, so callers follow it."""
        return 16000 if "+CPCMFRM: 1" in self.send("AT+CPCMFRM?") else 8000
