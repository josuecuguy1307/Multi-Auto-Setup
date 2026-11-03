"""The mas-voice skill: the language-and-format rules for the chat's model. It is knowledge, not a protocol: it must load under the library's rules, reach the chat with the connection, and say where it stops."""
from __future__ import annotations

import json
from pathlib import Path

from envrec.skills.lib import Library

SKILLS = Path(__file__).resolve().parents[1] / "skills"


def test_mas_voice_loads_under_the_library_rules_and_is_neither_a_protocol_nor_a_runnable_skill():
    lib = Library().load()
    assert lib.errors == [], lib.errors
    s = lib.get("mas-voice")
    assert s.description.startswith("Use when") and s.name == s.path.name and s.meta.plan is None and s.meta.states is None


def test_it_is_handed_to_the_chat_with_its_connection():
    assert "mas-voice" in json.loads((SKILLS / "harness-app-chat" / "references" / "delivered.json").read_text())["skills"]


def test_it_states_its_boundary_the_language_rule_and_the_ask_with_options_rule():
    body = (SKILLS / "mas-voice" / "SKILL.md").read_text(encoding="utf-8")
    for must in ("It does not choose a tool, an option, a model", "Show it **as it is**", "Never translate it", "Answer in the language the owner writes in",
                 "fixed text stays English", "Every decision of his is a **question with options**", "You never answer, approve or choose for him", "never carry a meaning by a symbol or a color alone".capitalize()):
        assert must in body, must
    assert "\x1b" not in body and "mas_prepare" not in body and "mas_start" not in body  # it names no tool to call for an action: wording only
