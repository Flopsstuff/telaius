"""Outbound-call safety policy shared by the CLI and future integrations."""
import datetime as dt
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from telaius.errors import ConfigError, PolicyDenied


def normalize_number(number):
    value = re.sub(r"[\s-]", "", number or "")
    if value.startswith("00"):
        value = "+" + value[2:]
    elif re.fullmatch(r"\d{9}", value):
        value = "+48" + value
    if not re.fullmatch(r"\+\d{7,15}", value):
        raise ConfigError("phone number must be E.164, 00-prefixed, or 9 Polish digits")
    return value


def _clock(value):
    try:
        hour, minute = map(int, value.split(":"))
        return dt.time(hour, minute)
    except (AttributeError, TypeError, ValueError) as error:
        raise ConfigError(f"invalid quiet-hours time {value!r}") from error


def in_quiet_hours(now, start="22:00", end="09:00"):
    current, start, end = now.timetz().replace(tzinfo=None), _clock(start), _clock(end)
    return start <= current < end if start < end else current >= start or current < end


class Policy:
    def __init__(self, config=None, calls_dir=None):
        cfg = config or {}
        self.allow_any = bool(cfg.get("allow_any", False))
        self.allowlist = {normalize_number(n) for n in cfg.get("allowlist", [])}
        self.max_seconds = int(cfg.get("max_seconds", 300))
        self.calls_per_day = int(cfg.get("calls_per_day", 20))
        self.timezone = cfg.get("timezone", "Europe/Warsaw")
        quiet = cfg.get("quiet_hours", {})
        self.quiet_start = quiet.get("start", "22:00")
        self.quiet_end = quiet.get("end", "09:00")
        self.calls_dir = Path(calls_dir or "calls")

    def check_call(self, number, max_seconds, now=None):
        number = normalize_number(number)
        if not self.allow_any and number not in self.allowlist:
            raise PolicyDenied("destination is not in the configured allowlist")
        now = now or dt.datetime.now(ZoneInfo(self.timezone))
        if in_quiet_hours(now, self.quiet_start, self.quiet_end):
            raise PolicyDenied(f"calls are disabled during quiet hours {self.quiet_start}-{self.quiet_end}")
        prefix = now.strftime("%Y-%m-%d_")
        if self.calls_dir.exists() and sum(p.name.startswith(prefix) for p in self.calls_dir.iterdir()) >= self.calls_per_day:
            raise PolicyDenied(f"daily call limit ({self.calls_per_day}) reached")
        return number, min(float(max_seconds), self.max_seconds), float(max_seconds) > self.max_seconds
