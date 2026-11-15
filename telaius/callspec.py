"""What one call is asked to do: the input of a CallSession, stored as spec.json.

The instructions are rebuilt from the spec rather than stored, so a background worker reads the
same prompt files as the caller; only --prompt-file text travels inside the spec.
"""
import dataclasses

from telaius.errors import ConfigError
from telaius.prompts import LANGS, build_instructions

AUDIO_PATHS = ("usb", "relay")


@dataclasses.dataclass
class CallSpec:
    number: str = None
    lang: str = "ru"
    voice: str = "rex"
    task: str = None
    callee: str = None
    context: str = None
    formal: bool = False
    prompt: str = None  # full system prompt, replaces the built-in one
    audio: str = "usb"
    max_seconds: float = 120

    def __post_init__(self):
        if self.lang not in LANGS:
            raise ConfigError(f"unknown language {self.lang!r} (have {', '.join(LANGS)})")
        if self.audio not in AUDIO_PATHS:
            raise ConfigError(f"audio must be one of {', '.join(AUDIO_PATHS)}, not {self.audio!r}")
        if self.max_seconds <= 0:
            raise ConfigError("max_seconds must be positive")

    def instructions(self):
        return build_instructions(self.lang, self.voice, self.task, self.callee, self.context,
                                  self.prompt, self.formal)

    def to_dict(self):
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data):
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ConfigError(f"unknown call spec fields: {', '.join(unknown)}")
        return cls(**data)
