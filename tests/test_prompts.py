"""Prompt config key parity and build_instructions completeness across languages."""
import re
import unittest

from telaius.prompts import LANGS, SETTINGS, TEXTS, VOICE_GENDER, build_instructions

_PLACEHOLDER = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}|\{\}")


def _toml_keys(value, prefix=""):
    keys = set()
    if isinstance(value, dict):
        for name, child in value.items():
            path = f"{prefix}.{name}" if prefix else name
            if isinstance(child, dict):
                keys |= _toml_keys(child, path)
            else:
                keys.add(path)
    return keys


class PromptParityTests(unittest.TestCase):
    def test_langs_include_en_ru_pl(self):
        self.assertEqual(set(LANGS), {"en", "pl", "ru"})

    def test_prompt_files_share_the_same_keys(self):
        by_lang = {lang: _toml_keys(TEXTS[lang]) for lang in LANGS}
        reference = by_lang[LANGS[0]]
        for lang, keys in by_lang.items():
            self.assertEqual(keys, reference, f"{lang} keys differ from {LANGS[0]}")

    def test_settings_have_english_owner_and_bot_name(self):
        self.assertIn("en", SETTINGS["owner"])
        self.assertIn("en", SETTINGS["owner_full"])
        self.assertIn("en", SETTINGS["bot_name"]["female"])
        self.assertEqual(SETTINGS["lang_hint"].get("en"), "en")
        self.assertIn("Russian", SETTINGS["tools"]["summary"])

    def test_build_instructions_fills_placeholders(self):
        voices = sorted({*VOICE_GENDER, "rex", "eve"})
        cases = [
            dict(task=None, formal=False, constraints=None),
            dict(task="Confirm Saturday appointment", formal=False, constraints=None),
            dict(task="Ask about opening hours", formal=True, constraints=None),
            dict(task="Book a table for two", formal=False,
                 constraints="Do not share the owner's phone number"),
            dict(task="Ask for a quote", formal=True,
                 constraints="No payments; no personal data beyond the task"),
        ]
        for lang in LANGS:
            for voice in voices:
                for case in cases:
                    with self.subTest(lang=lang, voice=voice, **case):
                        text = build_instructions(
                            lang, voice,
                            task=case["task"],
                            callee="Alex" if case["task"] else None,
                            context="Prefers mornings" if case["task"] else None,
                            formal=case["formal"],
                            constraints=case["constraints"],
                        )
                        leftover = _PLACEHOLDER.findall(text)
                        self.assertEqual(leftover, [], f"unfilled placeholders: {leftover}")
                        if case["constraints"]:
                            self.assertIn(case["constraints"], text)


if __name__ == "__main__":
    unittest.main()
