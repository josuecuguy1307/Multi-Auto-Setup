"""Who answers what, read from data (`skills/os-confirm/references/approval-policy.json`): the kinds of owner decision that open the operating system's own dialog, and the ones that are ONE chat question
bound to a plan hash. A kind that is not listed keeps the mechanisms its channel lists."""

from __future__ import annotations

import json
from pathlib import Path

POLICY_FILE = Path(__file__).resolve().parents[2] / "skills" / "os-confirm" / "references" / "approval-policy.json"
CHAT_CHOICE = "chat_choice"  # the gesture value recorded for an approval answered in the chat: never `os_dialog`


def load(path: Path | None = None) -> dict:
    return json.loads((path or POLICY_FILE).read_text(encoding="utf-8"))


def mechanism_for(kind: str, doc: dict | None = None) -> str | None:
    return (doc or load())["kinds"].get(kind)


def options(doc: dict | None = None) -> list[dict]:
    o = (doc or load())["chat_choice_options"]
    return [{"id": "approve", "label": o["approve"], "detail": "the machine runs exactly the steps shown here and nothing else"}, {"id": "reject", "label": o["reject"], "detail": "nothing is changed"}]
