"""A native hidden-field prompt of the operating system, raised by the coordinator on the owner's own computer (skill `secret-entry`).

Which program shows the window is data in `skills/secret-entry/references/prompts.json`; this module only tries the entries for the running family in order, passes the title and the message as separate
arguments (no shell), and reads the answer from the program's own output. The typed value is returned to the caller and kept nowhere here: it is never printed, logged or put in an error. No answer is
invented: a missing program or a failure to start is `unavailable`, an untouched window is `timeout`."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from envrec_os import SKILLS_DIR, family

PROMPTS_FILE = SKILLS_DIR / "secret-entry" / "references" / "prompts.json"
MAX_FIELD = 1800
MAX_VALUE = 4096


def table() -> dict:
    return json.loads(PROMPTS_FILE.read_text(encoding="utf-8"))


def _fill(item: str, values: dict) -> str:
    for k, v in values.items():
        item = item.replace("{" + k + "}", v)
    return item


def _run(argv: list[str], env: dict, timeout: float) -> tuple[int | None, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except (OSError, subprocess.SubprocessError):
        return None, "unavailable"
    return done.returncode, (done.stdout or "")[: MAX_VALUE + 16]


def tools_here(fam: str | None = None, *, which: Callable[[str], str | None] = shutil.which) -> list[dict]:
    return [tool for tool in table().get(fam or family(), {}).get("tools", []) if which(tool["exe"])]


def available(fam: str | None = None, *, which: Callable[[str], str | None] = shutil.which) -> dict:
    tools = tools_here(fam, which=which)
    return {"available": bool(tools), "mechanism": tools[0]["id"] if tools else None}


def native_secret(title: str, message: str, *, timeout_s: float = 120.0, fam: str | None = None, run: Callable = _run, which: Callable[[str], str | None] = shutil.which) -> dict:
    """Show the hidden-field window and wait. Returns {"result": entered|declined|timeout|unavailable, "mechanism": id|None, "value": str|None}. Blocking: call it off the event loop. The value is
    only in the returned dict; the caller sends it to the node and drops it."""
    for tool in tools_here(fam, which=which):
        values = {"title": title[:200], "message": message[:MAX_FIELD], "timeout": str(int(timeout_s))}
        env = {**os.environ, **{k: _fill(v, values) for k, v in tool.get("env", {}).items()}}
        code, out = run([which(tool["exe"]), *[_fill(a, values) for a in tool["argv"]]], env, timeout_s + 15)
        if code is None:
            return {"result": "timeout" if out == "timeout" else "unavailable", "mechanism": tool["id"], "value": None}
        rc_map, prefix = tool.get("rc"), tool.get("value_prefix")
        text = out.rstrip("\r\n")
        if rc_map is not None:
            result = rc_map.get(str(code), "declined")
            value = text if result == "entered" and text else None
        elif prefix and text.startswith(prefix) and len(text) > len(prefix):
            result, value = "entered", text[len(prefix):]
        else:
            result, value = ("timeout" if text.strip() == "timeout" else "declined"), None
        if result == "entered" and (value is None or len(value) > MAX_VALUE):
            result, value = "declined", None
        return {"result": result, "mechanism": tool["id"], "value": value}
    return {"result": "unavailable", "mechanism": None, "value": None}
