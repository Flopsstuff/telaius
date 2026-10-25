"""Minimal QMI client for the SIM7600 over Linux cdc-wdm or raw libusb, plus a CLI.

Linux sends whole QMUX messages through /dev/cdc-wdm0. The macOS fallback uses CDC encapsulated
commands on interface 5 (SEND_ENCAPSULATED_COMMAND / GET_ENCAPSULATED_RESPONSE control transfers).
Used to read the IMS registration state (IMSA), which no AT command on this firmware reports, and
to list/switch carrier MBN profiles (PDC).

Usage:
  python -m telaius.qmi status            # IMS registration + active MBN
  python -m telaius.qmi mbn-list          # all software MBN profiles in the firmware
  python -m telaius.qmi mbn-select HEXID  # select + activate a profile (the modem resets)
"""
import argparse
import os
import select
import struct
import time

import usb.core
import usb.util

from telaius.modem import open_usb_modem

IF_QMI = 5
SVC_CTL, SVC_IMSA, SVC_PDC = 0x00, 0x21, 0x24
PDC_SOFTWARE = 1
IMS_REG_STATES = {0: "NOT_REGISTERED", 1: "REGISTERING", 2: "REGISTERED", 3: "LIMITED"}
SRV_STATES = {0: "unavailable", 1: "limited", 2: "full"}
CDC_WDM = "/dev/cdc-wdm0"


def tlv(t, v):
    return struct.pack("<BH", t, len(v)) + v


def parse_tlvs(b):
    out, i = {}, 0
    while i + 3 <= len(b):
        t, n = struct.unpack_from("<BH", b, i)
        out[t] = b[i + 3:i + 3 + n]
        i += 3 + n
    return out


class CdcWdmPort:
    """QMI transport through Linux qmi_wwan's character device."""

    def __init__(self, path=CDC_WDM):
        self.path = path
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except PermissionError as e:
            raise PermissionError(
                f"cannot open {path}; grant group dialout access with the Telaius udev rule"
            ) from e

    def write(self, data, timeout=3.0):
        deadline = time.monotonic() + timeout
        poller = select.poll()
        poller.register(self.fd, select.POLLOUT)
        while True:
            remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
            if not poller.poll(remaining_ms):
                raise TimeoutError(f"write timeout on {self.path}")
            try:
                written = os.write(self.fd, data)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"write timeout on {self.path}")
                continue
            if written != len(data):
                raise OSError(f"short QMI write to {self.path}: {written}/{len(data)} bytes")
            return

    def read(self, timeout):
        poller = select.poll()
        poller.register(self.fd, select.POLLIN)
        if not poller.poll(max(0, int(timeout * 1000))):
            return b""
        try:
            return os.read(self.fd, 4096)
        except BlockingIOError:
            return b""

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


class UsbQmiPort:
    """Raw libusb QMI fallback for macOS or Linux without cdc-wdm."""

    def __init__(self, dev=None):
        self.dev = dev or open_usb_modem()
        try:
            self._kernel_detached = self.dev.is_kernel_driver_active(IF_QMI)
        except NotImplementedError:
            self._kernel_detached = False
        if self._kernel_detached:
            self.dev.detach_kernel_driver(IF_QMI)
        usb.util.claim_interface(self.dev, IF_QMI)
        # QMI only answers with DTR asserted (same quirk as qmi_wwan's SET_DTR).
        self.dev.ctrl_transfer(0x21, 0x22, 0x01, IF_QMI, None)

    def close(self):
        usb.util.release_interface(self.dev, IF_QMI)
        if self._kernel_detached:
            try:
                self.dev.attach_kernel_driver(IF_QMI)
            except (NotImplementedError, usb.core.USBError):
                pass
        usb.util.dispose_resources(self.dev)

    def read(self, timeout):
        end = time.time() + timeout
        while True:
            # Drain RESPONSE_AVAILABLE notifications; firmware B06 stops answering if they pile up.
            try:
                self.dev.read(0x8B, 64, timeout=20)
            except usb.core.USBError:
                pass
            try:
                r = bytes(self.dev.ctrl_transfer(0xA1, 0x01, 0, IF_QMI, 4096, timeout=200))
                if r:
                    return r
            except usb.core.USBError:
                pass
            if time.time() > end:
                return b""
            time.sleep(0.02)

    def write(self, data, timeout=3.0):
        self.dev.ctrl_transfer(0x21, 0x00, 0, IF_QMI, data)


def open_qmi_port(dev=None):
    forced = os.environ.get("TELAIUS_TRANSPORT", "").lower()
    if forced and forced not in {"tty", "usb"}:
        raise ValueError("TELAIUS_TRANSPORT must be tty or usb")
    if dev is not None or forced == "usb":
        return UsbQmiPort(dev)
    if forced == "tty" or os.path.exists(CDC_WDM):
        return CdcWdmPort()
    return UsbQmiPort()


class Qmi:
    def __init__(self, dev=None, transport=None):
        self.transport = transport or open_qmi_port(dev)
        try:
            self.txn = 1
            self.pending = []
            time.sleep(0.5)
            while self._recv_raw(0.3):
                pass
            # CTL Sync drops stale clients; after a modem reset CTL stays silent until it succeeds.
            for _ in range(3):
                try:
                    self.request(SVC_CTL, 0, 0x0027, timeout=1.5)
                    break
                except TimeoutError:
                    pass
        except BaseException:
            self.transport.close()
            raise

    def close(self):
        self.transport.close()

    def _recv_raw(self, timeout):
        return self.transport.read(timeout)

    @staticmethod
    def _parse(raw):
        # QMUX header: 0x01, length(2), flags, service, client id; CTL has a 1-byte txn, others 2 bytes.
        svc, cid = raw[4], raw[5]
        if svc == SVC_CTL:
            flags, txn, msg, tl = struct.unpack_from("<BBHH", raw, 6)
            body = raw[12:12 + tl]
        else:
            flags, txn, msg, tl = struct.unpack_from("<BHHH", raw, 6)
            body = raw[13:13 + tl]
        return dict(svc=svc, cid=cid, flags=flags, txn=txn, msg=msg, tlvs=parse_tlvs(body))

    def request(self, svc, cid, msg, tlvs=b"", timeout=3.0):
        txn = self.txn
        self.txn = (self.txn + 1) & 0xFF or 1
        fmt = "<BBHH" if svc == SVC_CTL else "<BHHH"
        sdu = struct.pack(fmt, 0, txn, msg, len(tlvs)) + tlvs
        self.transport.write(struct.pack("<BHBBB", 1, 5 + len(sdu), 0, svc, cid) + sdu, timeout)
        end = time.time() + timeout
        while time.time() < end:
            raw = self._recv_raw(end - time.time())
            if not raw:
                break
            m = self._parse(raw)
            indication = m["flags"] & (0x02 if svc == SVC_CTL else 0x04)
            if m["svc"] == svc and m["txn"] == txn and m["msg"] == msg and not indication:
                return m
            self.pending.append(m)
        raise TimeoutError(f"no QMI response svc={svc:#x} msg={msg:#x}")

    def wait_ind(self, svc, msg, timeout=5.0):
        end = time.time() + timeout
        while True:
            for m in self.pending:
                if m["svc"] == svc and m["msg"] == msg:
                    self.pending.remove(m)
                    return m
            if time.time() > end:
                return None
            raw = self._recv_raw(0.5)
            if raw:
                self.pending.append(self._parse(raw))

    @staticmethod
    def result(m):
        r = m["tlvs"].get(0x02)
        return struct.unpack("<HH", r) if r else None

    def alloc(self, svc):
        m = self.request(SVC_CTL, 0, 0x0022, tlv(0x01, bytes([svc])))
        if self.result(m) != (0, 0):
            raise RuntimeError(f"QMI client alloc for service {svc:#x} failed: {self.result(m)}")
        return m["tlvs"][0x01][1]

    def release(self, svc, cid):
        try:
            self.request(SVC_CTL, 0, 0x0023, tlv(0x01, bytes([svc, cid])))
        except TimeoutError:
            pass

    # --- IMSA ---

    def ims_status(self):
        cid = self.alloc(SVC_IMSA)
        try:
            reg = self.request(SVC_IMSA, cid, 0x0020)["tlvs"]
            srv = self.request(SVC_IMSA, cid, 0x0021)["tlvs"]
        finally:
            self.release(SVC_IMSA, cid)
        state = struct.unpack("<I", reg[0x12])[0] if 0x12 in reg else None
        # TLV 0x15: count, then length-prefixed URI strings
        raw, uris, i = reg.get(0x15, b""), [], 1
        for _ in range(raw[0] if raw else 0):
            uris.append(raw[i + 1:i + 1 + raw[i]].decode(errors="replace"))
            i += 1 + raw[i]
        return {
            "registered": bool(reg.get(0x10, b"\0")[0]),
            "state": IMS_REG_STATES.get(state, state),
            "uris": uris,
            "sms": SRV_STATES.get(struct.unpack("<I", srv[0x10])[0]) if 0x10 in srv else None,
            "voip": SRV_STATES.get(struct.unpack("<I", srv[0x11])[0]) if 0x11 in srv else None,
        }

    # --- PDC (MBN profiles) ---

    def _pdc(self, fn):
        cid = self.alloc(SVC_PDC)
        try:
            self.request(SVC_PDC, cid, 0x0020, tlv(0x10, b"\x01"))  # register for indications
            return fn(cid, struct.pack("<I", 0x7E1A))
        finally:
            self.release(SVC_PDC, cid)

    def mbn_active(self):
        def run(cid, tok):
            self.request(SVC_PDC, cid, 0x0022, tlv(0x01, struct.pack("<I", PDC_SOFTWARE)) + tlv(0x10, tok))
            ind = self.wait_ind(SVC_PDC, 0x0022)
            act = ind and ind["tlvs"].get(0x11)
            return act[1:].hex() if act else None
        return self._pdc(run)

    def mbn_list(self):
        def run(cid, tok):
            self.request(SVC_PDC, cid, 0x0024, tlv(0x10, tok) + tlv(0x11, struct.pack("<I", PDC_SOFTWARE)))
            ind = self.wait_ind(SVC_PDC, 0x0024)
            ids, b = [], (ind or {}).get("tlvs", {}).get(0x11, b"")
            i = 1
            for _ in range(b[0] if b else 0):
                _, ln = struct.unpack_from("<IB", b, i)
                ids.append(b[i + 5:i + 5 + ln])
                i += 5 + ln
            out = []
            for cfg_id in ids:
                self.request(SVC_PDC, cid, 0x0028,
                             tlv(0x01, struct.pack("<IB", PDC_SOFTWARE, len(cfg_id)) + cfg_id) + tlv(0x10, tok))
                info = self.wait_ind(SVC_PDC, 0x0028)
                desc = info["tlvs"][0x12][1:].decode(errors="replace") if info and 0x12 in info["tlvs"] else "?"
                out.append((cfg_id.hex(), desc))
            return out
        return self._pdc(run)

    def mbn_select(self, hex_id):
        cfg_id = bytes.fromhex(hex_id)

        def run(cid, tok):
            self.request(SVC_PDC, cid, 0x0023,
                         tlv(0x01, struct.pack("<IB", PDC_SOFTWARE, len(cfg_id)) + cfg_id) + tlv(0x10, tok))
            sel = self.wait_ind(SVC_PDC, 0x0023)
            self.request(SVC_PDC, cid, 0x0027, tlv(0x01, struct.pack("<I", PDC_SOFTWARE)) + tlv(0x10, tok))
            act = self.wait_ind(SVC_PDC, 0x0027, timeout=10)
            return sel and sel["tlvs"].get(0x01), act and act["tlvs"].get(0x01)
        return self._pdc(run)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["status", "mbn-list", "mbn-select"])
    ap.add_argument("id", nargs="?")
    a = ap.parse_args()
    q = Qmi()
    try:
        if a.cmd == "status":
            print("IMS:", q.ims_status())
            print("active MBN:", q.mbn_active())
        elif a.cmd == "mbn-list":
            active = q.mbn_active()
            for cfg_id, desc in q.mbn_list():
                print(("* " if cfg_id == active else "  ") + f"{cfg_id}  {desc}")
        else:
            if not a.id:
                ap.error("mbn-select needs a profile id from mbn-list")
            sel, act = q.mbn_select(a.id)
            print(f"select err={sel.hex() if sel else None} activate err={act.hex() if act else None}; the modem resets now")
    finally:
        try:
            q.close()
        except (OSError, usb.core.USBError):
            pass  # the modem may already be resetting after mbn-select


if __name__ == "__main__":
    main()
