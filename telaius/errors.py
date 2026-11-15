"""Expected failures of the library, so nothing below main() calls sys.exit.

Each class carries a short `code` that stays stable for machine-readable interfaces
(CLI --json, later the MCP server), independent of the message text.
"""


class TelaiusError(Exception):
    code = "error"


class ConfigError(TelaiusError):
    code = "config_error"


class ModemNotFound(TelaiusError):
    code = "modem_not_found"


class ModemBusy(TelaiusError):
    code = "modem_busy"


class ModemNotReady(TelaiusError):
    code = "modem_not_ready"


class PolicyDenied(TelaiusError):
    code = "policy_denied"


class XaiError(TelaiusError):
    code = "xai_error"


class CallNotFound(TelaiusError):
    code = "call_not_found"
