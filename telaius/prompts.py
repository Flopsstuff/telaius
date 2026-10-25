"""Bot instructions and tools for a call, assembled from the config files in prompts/.

prompts/<lang>.toml holds the texts of one language (chat, intros, task template and rules, recording
facts, voice tags, gender lines); prompts/settings.toml holds the owner's name, the bot name per voice
gender, voice genders, transcription hints and the tool descriptions. Set TELAIUS_PROMPTS_DIR to use
another folder.

Without a task the bot holds a free test chat with the owner. With --task it calls on the owner's
behalf to get one thing done, using only the facts in the task and context.
"""
import os
import tomllib
from pathlib import Path

PROMPTS_DIR = Path(os.environ.get("TELAIUS_PROMPTS_DIR") or Path(__file__).resolve().parents[1] / "prompts")


def _load(name):
    with open(PROMPTS_DIR / f"{name}.toml", "rb") as f:
        return tomllib.load(f)


SETTINGS = _load("settings")
LANGS = sorted(p.stem for p in PROMPTS_DIR.glob("*.toml") if p.stem != "settings")
TEXTS = {lang: _load(lang) for lang in LANGS}

# lang -> transcription language hint for grok-transcribe (None = auto)
LANG_HINT = {lang: SETTINGS.get("lang_hint", {}).get(lang) for lang in LANGS}
VOICE_GENDER = SETTINGS.get("voices", {})

OUTCOMES = ["done", "partial", "failed", "no_task"]
_DESC = SETTINGS["tools"]
_RESULT_PARAMS = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": OUTCOMES, "description": _DESC["outcome"]},
        "summary": {"type": "string", "description": _DESC["summary"]},
    },
    "required": ["outcome", "summary"],
}
TOOLS = [
    {"type": "function", "name": "end_call", "description": _DESC["end_call"], "parameters": _RESULT_PARAMS},
    {"type": "function", "name": "report_result", "description": _DESC["report_result"], "parameters": _RESULT_PARAMS},
]


def _gender(voice):
    return VOICE_GENDER.get(voice.lower())


def bot_name(lang, voice):
    names = SETTINGS["bot_name"]
    return names.get(_gender(voice) or "", {}).get(lang, names["default"])


def build_instructions(lang, voice, task=None, callee=None, context=None, prompt=None, formal=False):
    """System instructions for one call. `prompt` (from --prompt-file) replaces the built-in text;
    `formal` is for organizations: question first, AI disclosure right after it, formal address."""
    t = TEXTS[lang]
    gender = _gender(voice)
    words = {"name": bot_name(lang, voice), "owner": SETTINGS["owner"][lang],
             "owner_full": SETTINGS["owner_full"][lang],
             **t.get("forms", {}).get("female" if gender == "female" else "male", {})}
    words["intro"] = (t["intro_formal"] if formal and task else t["intro"]).format(**words)
    if prompt:
        parts = [prompt.strip()]
    elif task:
        k = t["task"]
        parts = [k["intro"].format(**words), k["formal" if formal else "informal"].format(**words),
                 k["task"].format(task.strip()),
                 k["callee"].format(callee.strip()) if callee else k["no_callee"]]
        if context:
            parts.append(k["context"].format(context.strip()))
        parts.append(k["rules"])
    else:
        parts = [t["chat"].format(**words)]
    parts.append(t["recording"])  # also with --prompt-file: the bot must never deny the recording
    if not prompt:
        parts.append(t["expressive"])
    if gender in t.get("gender", {}):
        parts.append(t["gender"][gender])
    return "\n".join(parts)
