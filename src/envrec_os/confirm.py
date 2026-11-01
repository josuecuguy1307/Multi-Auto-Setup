"""A native confirmation of the operating system, raised by the coordinator on top of the owner's chat (docs/HARNESS_APP_STANDARD.md, "approvals").

Which program shows the dialog, with which arguments, is data in `skills/os-confirm/references/dialogs.json`; this module only tries the entries for the running family in order, passes the
title and the message as separate arguments (no shell), and classifies the answer. No answer is invented: a missing program or a failure to start is `unavailable`, an untouched dialog is
`timeout`, and only the program's own output saying so is `approved`."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from envrec_os import SKILLS_DIR, family

DIALOGS_FILE = SKILLS_DIR / "os-confirm" / "references" / "dialogs.json"
MAX_FIELD = 1800


def table() -> dict:
    return json.loads(DIALOGS_FILE.read_text(encoding="utf-8"))


def _run(argv: list[str], env: dict, timeout: float) -> tuple[int | None, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except (OSError, subprocess.SubprocessError):
        return None, "unavailable"
    return done.returncode, (done.stdout or "").strip()[:200]


def _fill(item: str, values: dict) -> str:
    for k, v in values.items():
        item = item.replace("{" + k + "}", v)  # placeholders only: a value is never interpreted
    return item


def tools_here(fam: str | None = None, *, which: Callable[[str], str | None] = shutil.which) -> list[dict]:
    return [tool for tool in table().get(fam or family(), {}).get("tools", []) if which(tool["exe"])]


def available(fam: str | None = None, *, which: Callable[[str], str | None] = shutil.which) -> dict:
    """What would be used on this machine: {"available": bool, "mechanism": id or None}. Nothing is shown to the owner."""
    tools = tools_here(fam, which=which)
    return {"available": bool(tools), "mechanism": tools[0]["id"] if tools else None}


def _ref(values: dict) -> dict:
    return {**values, "ref": str(DIALOGS_FILE.parent)}


def _strong_run(spec: dict, which_key: str, values: dict, *, run, which, timeout: float) -> tuple[int | None, str]:
    step = spec[which_key]
    v = _ref(values)
    env = {**os.environ, **{k: _fill(x, v) for k, x in step.get("env", {}).items()}}
    return run([which(spec["exe"]), *[_fill(a, v) for a in step["argv"]]], env, timeout)


def strong_available(tool: dict, *, run=_run, which: Callable[[str], str | None] = shutil.which) -> bool:
    """Can this machine verify its owner (Touch ID or the account password; Windows Hello)? A probe that shows nothing."""
    spec = tool.get("strong")
    if not spec or not which(spec["exe"]):
        return False
    return _strong_run(spec, "probe", {"reason": "", "title": "", "message": ""}, run=run, which=which, timeout=60.0)[1] == spec["probe_ok"]


def native_confirm(title: str, message: str, *, timeout_s: float = 120.0, fam: str | None = None, run: Callable = _run, which: Callable[[str], str | None] = shutil.which, strong: bool = True, reason: str | None = None) -> dict:
    """Show the dialog and wait. When the machine can verify its owner (Touch ID, the account password, Windows Hello) that proof is REQUIRED after the button and the dialog says so; when it cannot, the
    button alone is accepted and the answer is labeled weaker, in the dialog and in the result. Returns {"result": approved|declined|timeout|unavailable, "mechanism": id|None, "strong": id|None,
    "assurance": "strong"|"weak"|None}. Blocking: call it off the event loop."""
    table_ = table()
    for tool in tools_here(fam, which=which):
        use_strong = strong and strong_available(tool, run=run, which=which)
        note = tool["strong"]["next_note"] if use_strong else table_.get("weak_note", "")
        values = {"title": title[:200], "message": (message + ("\n\n" + note if note else ""))[:MAX_FIELD], "timeout": str(int(timeout_s)), "reason": (reason or title)[:200]}
        env = {**os.environ, **{k: _fill(v, values) for k, v in tool.get("env", {}).items()}}
        code, out = run([which(tool["exe"]), *[_fill(a, values) for a in tool["argv"]]], env, timeout_s + 15)
        rc_map = tool.get("rc")
        result = rc_map.get(str(code), "declined") if rc_map and code is not None else (out if out in ("approved", "declined", "timeout") else "unavailable" if code is None and out == "unavailable" else "timeout" if out == "timeout" else "declined")
        if result != "approved":
            return {"result": result, "mechanism": tool["id"], "strong": None, "assurance": None}
        if not use_strong:
            return {"result": "approved", "mechanism": tool["id"], "strong": None, "assurance": "weak"}
        spec = tool["strong"]
        code2, out2 = _strong_run(spec, "ask", values, run=run, which=which, timeout=timeout_s + 15)
        ok = out2 == spec["approved"]
        return {"result": "approved" if ok else "declined" if code2 is not None else "timeout", "mechanism": tool["id"], "strong": spec["id"] if ok else None, "assurance": "strong" if ok else None}
    return {"result": "unavailable", "mechanism": None, "strong": None, "assurance": None}
