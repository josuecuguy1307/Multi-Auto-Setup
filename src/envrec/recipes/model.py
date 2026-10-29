"""The recipe format and its static checks. A recipe is JSON inside a skill's sidecar; its canonical hash is what the owner approves."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from envrec.recipes.shell import SHELLS

PLACEHOLDERS = {"root", "port"}
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
MAX_STEPS, MAX_ARG = 40, 400
# Things that are never part of a workspace self-setup, whatever the shell: destructive, privileged, persistent, networked or encoded. Substring match on the lower-cased command.
FORBIDDEN = ("format ", "diskpart", "shutdown", "reboot", "restart-computer", "rm -rf /", "net user", "net localgroup", "reg delete", "reg add", "schtasks", "sc create", "sc config",
             "icacls", "takeown", "bcdedit", "invoke-expression", "iex ", "-encodedcommand", " -enc ", "curl", "wget", "invoke-webrequest", "invoke-restmethod", "bitsadmin", "certutil",
             "start-bitstransfer", "authorized_keys", "sshd_config", "passwd", "sudo", "runas", "startup", "launchd", "systemctl enable", "crontab", "netsh advfirewall", "set-executionpolicy")


def canonical(recipe: dict) -> str:
    return json.dumps(recipe, sort_keys=True, separators=(",", ":"))


def recipe_hash(recipe: dict) -> str:
    return hashlib.sha256(canonical(recipe).encode()).hexdigest()


def render(argv: list[str], values: dict[str, str]) -> list[str]:
    """Fill the placeholders. A placeholder outside the closed set, or left unfilled, is an error."""
    out = []
    for a in argv:
        for name in _PLACEHOLDER.findall(a):
            if name not in values:
                raise ValueError(f"unknown placeholder {{{name}}}")
        out.append(_PLACEHOLDER.sub(lambda m: values[m.group(1)], a))
    return out


def problems(recipe: Any) -> list[str]:
    """Every reason this recipe is not acceptable (empty: acceptable). Static: it looks at the text, it runs nothing."""
    if not isinstance(recipe, dict):
        return ["the recipe must be an object"]
    out: list[str] = []
    shell, root, port, steps = recipe.get("shell"), recipe.get("root"), recipe.get("port"), recipe.get("steps")
    if shell not in SHELLS:
        out.append(f"shell must be one of {SHELLS}")
    if not isinstance(root, str) or len(re.split(r"[\\/]+", root.strip("\\/"))) < 2 or ".." in root or "{" in root:
        out.append("root must be a path with at least two parts and no `..` (everything the recipe writes lives under it)")
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        out.append("port must be a number from 1024 to 65535")
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        return out + [f"steps must be a list of 1 to {MAX_STEPS} steps"]
    seen: set[str] = set()
    for i, s in enumerate(steps):
        sid = s.get("id") if isinstance(s, dict) else None
        tag = f"step {sid or i}"
        if not isinstance(s, dict) or not isinstance(sid, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", sid) or sid in seen:
            out.append(f"{tag}: needs a unique id of lower-case letters, digits and underscores")
            continue
        seen.add(sid)
        cmds = []
        for field in ("run", "undo"):
            if field in s:
                v = s[field]
                if not (isinstance(v, list) and v and all(isinstance(a, str) and 0 < len(a) <= MAX_ARG for a in v)):
                    out.append(f"{tag}: {field} must be a list of non-empty strings (an argv), never a shell line in one string with pipes")
                else:
                    cmds.append((field, v))
        put = s.get("put")
        if put is not None:
            if not (isinstance(put, dict) and put.get("artifact") in ("worker", "runtime", "model") and isinstance(put.get("dest"), str) and isinstance(put.get("receive"), list)
                    and all(isinstance(a, str) for a in put["receive"])):
                out.append(f"{tag}: put needs artifact (worker, runtime or model), dest and receive (the argv that stores stdin at dest)")
            else:
                cmds.append(("put.receive", put["receive"]))
                if "{root}" not in put["dest"] or ".." in put["dest"]:
                    out.append(f"{tag}: put.dest must be under {{root}}")
        if "run" not in s and put is None:
            out.append(f"{tag}: needs run or put")
        if s.get("write"):
            if "undo" not in s:
                out.append(f"{tag}: a write step needs an undo")
            if not any("{root}" in a for _, argv in cmds for a in argv) and not (put and "{root}" in put.get("dest", "")):
                out.append(f"{tag}: a write step must act under {{root}}")
        for field, argv in cmds:
            text = " ".join(argv).lower()
            for bad in FORBIDDEN:
                if bad in text:
                    out.append(f"{tag}: {field} contains `{bad.strip()}`, which a workspace self-setup never needs")
            allowed = PLACEHOLDERS | ({"dest"} if field == "put.receive" else set())  # {dest} exists only where a file is stored
            for name in _PLACEHOLDER.findall(" ".join(argv)):
                if name not in allowed:
                    out.append(f"{tag}: unknown placeholder {{{name}}}; only {sorted(allowed)} exist")
    for key in ("health", "absent"):
        v = recipe.get(key)
        if not (isinstance(v, list) and v and all(isinstance(a, str) for a in v)):
            out.append(f"{key} must be an argv (a read-only command that checks the server / that nothing is left)")
        elif any(b in " ".join(v).lower() for b in FORBIDDEN):
            out.append(f"{key}: contains a command a workspace self-setup never needs")
    if not any(isinstance(s, dict) and s.get("write") and s.get("id") in ("start", "start_server") for s in steps):
        out.append("there must be a write step with id `start` that starts the server")
    return out


def describe(recipe: dict) -> list[str]:
    """One line per step with the exact command (placeholders filled), for the owner's approval of the recipe and for the executor's plan."""
    from envrec.recipes.shell import quote
    values = {"root": recipe["root"], "port": str(recipe["port"])}
    out = []
    for s in recipe["steps"]:
        what = ("send the " + s["put"]["artifact"] + " file to " + render([s["put"]["dest"]], values)[0]) if "put" in s else quote(render(s["run"], values), recipe["shell"])
        out.append(f"  {'WRITE' if s.get('write') else 'read '} {s['id']}: {what}" + (f"   undo: {quote(render(s['undo'], values), recipe['shell'])}" if s.get("undo") else ""))
    out.append(f"  read  health: {quote(render(recipe['health'], values), recipe['shell'])}")
    out.append(f"  read  absent: {quote(render(recipe['absent'], values), recipe['shell'])}")
    return out
