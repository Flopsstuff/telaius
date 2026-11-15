"""Bounded modem health summary used by the CLI and future MCP server."""
import subprocess

from telaius.errors import ModemBusy
from telaius.lock import ModemLock
from telaius.modem import IF_AT, AtChannel, close_modem, open_modem, open_port
from telaius.qmi import Qmi


def relay_status():
    try:
        result = subprocess.run(["adb", "shell", "pidof telaius-relay"], capture_output=True, text=True, timeout=2)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"available": False, "pid": None}
    return {"available": result.returncode == 0 and bool(result.stdout.strip()), "pid": result.stdout.strip() or None}


def _value(reply, prefix):
    line = next((line.strip() for line in reply.splitlines() if line.strip().startswith(prefix)), "")
    return line.split(":", 1)[1].strip() if ":" in line else None


def _identity(reply):
    # Pending URCs such as +CMTI arrive interleaved with the ATI reply.
    values = [line.strip() for line in reply.splitlines()
              if line.strip() and line.strip() != "OK" and not line.strip().startswith(("IMEI:", "+"))]
    return {"manufacturer": values[0].removeprefix("Manufacturer: ") if values else None,
            "model": values[1].removeprefix("Model: ") if len(values) > 1 else None}


def modem_status():
    lock = ModemLock("status")
    try:
        lock.acquire()
    except ModemBusy as error:
        return {"busy": True, "busy_detail": str(error), "modem": None, "firmware": None,
                "sim": None, "operator": None, "network": None, "signal": None,
                "ims": None, "relay": relay_status()}
    with lock:
        dev = open_modem()
        try:
            at = AtChannel(open_port(dev, IF_AT), echo=False)
            replies = {cmd: at.send(cmd, 0.6) for cmd in ("ATI", "AT+CGMR", "AT+CPIN?", "AT+COPS?", "AT+CPSI?", "AT+CSQ")}
        finally:
            close_modem(dev, [IF_AT])
        qmi = Qmi()
        try:
            ims = qmi.ims_status()
            ims.pop("uris", None)
        finally:
            qmi.close()
    cpsi = (_value(replies["AT+CPSI?"], "+CPSI") or "").split(",")
    csq = (_value(replies["AT+CSQ"], "+CSQ") or ",").split(",")
    operator = (_value(replies["AT+COPS?"], "+COPS") or "").split(",")
    return {"busy": False, "modem": _identity(replies["ATI"]),
            "firmware": _value(replies["AT+CGMR"], "+CGMR"),
            "sim": _value(replies["AT+CPIN?"], "+CPIN"),
            "operator": operator[2].strip('"') if len(operator) > 2 else None,
            "network": {"technology": cpsi[0] if cpsi else None, "state": cpsi[1] if len(cpsi) > 1 else None},
            "signal": {"rssi": int(csq[0]) if csq[0].isdigit() else None,
                       "ber": int(csq[1]) if len(csq) > 1 and csq[1].isdigit() else None},
            "ims": ims, "relay": relay_status()}
