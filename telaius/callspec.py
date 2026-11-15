"""What one call is asked to do: the input of a CallSession, stored as spec.json.

The instructions are rebuilt from the spec rather than stored, so a background worker reads the
same prompt files as the caller; only --prompt-file text travels inside the spec.
"""
import dataclasses
import json

from telaius.errors import ConfigError
from telaius.prompts import LANGS, build_instructions, tools

AUDIO_PATHS = ("auto", "usb", "relay")
SCHEMA_MAX_BYTES = 4096
SCHEMA_MAX_DEPTH = 8
SCHEMA_TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}


def _depth(value):
    if isinstance(value, dict):
        return 1 + max(map(_depth, value.values()), default=0)
    if isinstance(value, list):
        return 1 + max(map(_depth, value), default=0)
    return 0


def _check_types(schema, where="result_schema"):
    if not isinstance(schema, dict):
        raise ConfigError(f"{where} must be a JSON object")
    kinds = schema.get("type", [])
    for kind in [kinds] if isinstance(kinds, str) else kinds:
        if kind not in SCHEMA_TYPES:
            raise ConfigError(f"{where}: unknown type {kind!r}")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ConfigError(f"{where}.properties must be an object")
    for name, child in properties.items():
        _check_types(child, f"{where}.{name}")
    if "items" in schema:
        _check_types(schema["items"], f"{where}.items")
    required = schema.get("required", [])
    if not isinstance(required, list) or not set(required) <= set(properties):
        raise ConfigError(f"{where}.required must list names from its properties")


def check_result_schema(schema):
    """A result_schema is a small JSON Schema of `type: object`; the bot fills it in as `data`."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ConfigError("result_schema must be a JSON Schema with \"type\": \"object\"")
    if len(json.dumps(schema, ensure_ascii=False).encode()) > SCHEMA_MAX_BYTES:
        raise ConfigError(f"result_schema is larger than {SCHEMA_MAX_BYTES} bytes")
    if _depth(schema) > SCHEMA_MAX_DEPTH:
        raise ConfigError(f"result_schema is nested deeper than {SCHEMA_MAX_DEPTH} levels")
    _check_types(schema)


@dataclasses.dataclass
class CallSpec:
    number: str = None
    lang: str = "ru"
    voice: str = "rex"
    task: str = None
    callee: str = None
    context: str = None
    constraints: str = None
    formal: bool = False
    prompt: str = None  # full system prompt, replaces the built-in one
    audio: str = "auto"
    max_seconds: float = 120
    result_schema: dict = None

    def __post_init__(self):
        if self.lang not in LANGS:
            raise ConfigError(f"unknown language {self.lang!r} (have {', '.join(LANGS)})")
        if self.audio not in AUDIO_PATHS:
            raise ConfigError(f"audio must be one of {', '.join(AUDIO_PATHS)}, not {self.audio!r}")
        if self.max_seconds <= 0:
            raise ConfigError("max_seconds must be positive")
        if self.result_schema is not None:
            check_result_schema(self.result_schema)

    def instructions(self):
        return build_instructions(self.lang, self.voice, self.task, self.callee, self.context,
                                  self.prompt, self.formal, self.constraints, self.result_schema)

    def tools(self):
        return tools(self.result_schema)

    def to_dict(self):
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data):
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ConfigError(f"unknown call spec fields: {', '.join(unknown)}")
        return cls(**data)
