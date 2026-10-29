"""Turn free-form model text into exactly one validated Action."""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from envrec.contracts import Action, readable_error

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


class ActionParseError(ValueError):
    """The reply was not one valid Action. The message is short enough to feed back to the model."""


def extract_json_object(text: str) -> str:
    """First balanced {...} in the text, ignoring <think> blocks, code fences and prose around it."""
    text = _THINK.sub("", text)
    start = text.find("{")
    if start < 0:
        raise ActionParseError("no JSON object found in the reply")
    depth, in_str, escaped = 0, False, False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ActionParseError("JSON object is not closed")


def parse_action(text: str) -> Action:
    return parse_action_repairing(text, None)[0]


def parse_action_repairing(text: str, tool_names: set[str] | None) -> tuple[Action, str | None]:
    """Parse one Action. With `tool_names` (the closed menu) two common small-model form slips are repaired:

      {"action": "<a menu tool name>", ...}   -> {"action": "call_tool", "tool": "<name>", ...}
      {"tool": "<a menu tool name>", ...}     -> {"action": "call_tool", "tool": "<name>", ...}

    Nothing outside the menu is ever repaired, and the repair is reported so it can be counted.
    """
    raw = extract_json_object(text)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ActionParseError(f"not valid JSON ({exc.msg} at char {exc.pos})") from None
    if not isinstance(data, dict):
        raise ActionParseError("the reply must be a JSON object")
    note = None
    if tool_names:
        name = data.get("action")
        if isinstance(name, str) and name in tool_names and "tool" not in data:
            data = {**data, "action": "call_tool", "tool": name}
            note = f"action {name!r} is a menu tool name: read as call_tool"
        elif "action" not in data and isinstance(data.get("tool"), str) and data["tool"] in tool_names:
            data = {**data, "action": "call_tool"}
            note = "no action given: read as call_tool"
    try:
        return Action.model_validate(data), note
    except ValidationError as exc:
        raise ActionParseError(readable_error(exc)) from None
