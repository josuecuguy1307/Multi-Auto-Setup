"""`mas` with no arguments: the first-run menu. For the OWNER, at a terminal, for THIS computer only.

It decides nothing and writes nothing: a step runs the command that already exists, through the real command line parser, and that command shows its plan and takes the typed yes. The menu
never passes --yes. What each step is, its undo and what it needs first are data (skills/os-menu/references/menu.json). Where the machine is touched the per-OS layer does it (services)."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

import envrec_os
from envrec_mas import strings as S
from envrec_style import term

MENU_FILE = envrec_os.SKILLS_DIR / "os-menu" / "references" / "menu.json"


def load_menu() -> dict:
    return json.loads(MENU_FILE.read_text(encoding="utf-8"))


@dataclass
class Ports:
    """Everything outside the menu's own logic, so a test can replace it. The real ones are in `real_ports`."""
    read: Callable[[str], str]
    write: Callable[[str], None]
    run_cli: Callable[[list[str]], int]  # the real `mas` command line, minus the program name
    done: Callable[[str, str], bool]  # (role, check name) -> already done on this computer
    detect_role: Callable[[], str | None]
    addresses: Callable[[], list[tuple[str, str]]]  # (interface name, address)
    banner: Callable[[], str]
    state_args: list[str] = field(default_factory=list)  # ["--state-dir", path] when the owner gave one


def command_text(template: str, bind: str | None) -> str:
    return template.replace("{bind}", bind or "ADDRESS")


def _argv(command: str) -> list[str]:
    parts = command.split()
    assert parts[0] == "mas"
    return parts[1:]


def _checks(role: str, state: Path):
    """The names `done` may take in menu.json."""
    from envrec_node.state import NodeState
    from envrec_os import service as svc
    from envrec_mas import lifecycle

    def service_installed() -> bool:
        return svc.installed(svc.plan(role, lifecycle.service_argv(role, state)))

    def claude_desktop_connected() -> bool:
        from envrec_coordinator.connect import Env, detect
        return any(r["host"] == "claude-desktop" and r["state"] == "connected" for r in detect(Env.system(state)))

    def joined() -> bool:
        return NodeState(state).pin() is not None

    return {"service_installed": service_installed, "claude_desktop_connected": claude_desktop_connected, "joined": joined}


def real_ports(state: Path, state_args: list[str]) -> Ports:
    from envrec_mas import coordconfig
    from envrec_mas.cli import main as cli_main

    def done(role: str, check: str) -> bool:
        try:
            return bool(_checks(role, state)[check]())
        except Exception:  # a check that cannot run means "not known to be done": the step stays offered, and its own command refuses what must not happen
            return False

    def detect_role() -> str | None:
        from envrec_node.state import NodeState
        if NodeState(state).pin() is not None:
            return "node"
        if coordconfig.read(state) or (state / "coordinator").exists():
            return "coordinator"
        return None

    def addresses() -> list[tuple[str, str]]:
        return [(i.name, i.address) for i in envrec_os.interfaces()]

    return Ports(read=input, write=print, run_cli=lambda argv: cli_main(state_args + argv), done=done, detect_role=detect_role, addresses=addresses,
                 banner=term.for_stream, state_args=state_args)


def _ask_role(p: Ports) -> str | None:
    p.write(S.ROLE_ASK)
    p.write(f"  1. {S.ROLE_COORDINATOR}\n  2. {S.ROLE_NODE}\n{S.ROLE_NOTE}")
    answer = _read(p, S.CHOOSE)
    return {"1": "coordinator", "2": "node"}.get(answer)


def _read(p: Ports, prompt: str) -> str:
    try:
        return p.read(prompt).strip()
    except EOFError:
        return ""


def _choose_address(p: Ports) -> str | None:
    found = p.addresses()
    if not found:
        p.write(S.ADDRESS_NONE)
        return None
    p.write(S.ADDRESS_ASK)
    for i, (name, addr) in enumerate(found, 1):
        p.write(f"  {i}. {addr}  ({name})")
    pick = _read(p, S.ADDRESS_CHOOSE)
    if not pick.isdigit() or not 1 <= int(pick) <= len(found):
        return None
    return found[int(pick) - 1][1]


def _status(p: Ports, role: str, steps: list[dict]) -> list[tuple[dict, bool, str | None]]:
    done = {s["id"]: p.done(role, s["done"]) for s in steps}
    return [(s, done[s["id"]], s["needs"] if s["needs"] and not done.get(s["needs"], False) and not done[s["id"]] else None) for s in steps]


def run_step(p: Ports, step: dict, bind: str | None) -> int:
    command = command_text(step["command"], bind)
    p.write(S.STEP_SHOW.format(command="mas " + " ".join(_argv(command)), undo=step["undo"]))
    code = p.run_cli(_argv(command))
    p.write(S.STEP_RESULT_OK if code == 0 else S.STEP_RESULT_FAIL.format(code=code))
    if code == 0 and step.get("then"):
        p.write(getattr(S, step["then"].upper()))
    return code


def run_menu(p: Ports, menu: dict | None = None) -> int:
    """The loop: banner, the role, then the steps with their state; the owner picks one at a time. Returns 0 when the owner leaves."""
    menu = menu or load_menu()
    p.write(p.banner())
    p.write("\n" + S.MENU_TITLE + "\n" + S.MENU_INTRO)
    role = p.detect_role()
    if role:
        p.write(S.ROLE_FOUND.format(role=role))
    else:
        role = _ask_role(p)
        if role is None:
            p.write(S.LEFT)
            return 0
    steps = menu["roles"][role]["steps"]
    while True:
        rows = _status(p, role, steps)
        todo = [(s, blocked) for s, done, blocked in rows if not done]
        for i, (s, done, blocked) in enumerate(rows, 1):
            state = S.STATE_DONE if done else (S.STATE_BLOCKED.format(needs=blocked) if blocked else S.STATE_TODO)
            p.write(f"  {i}. [{'x' if done else ' '}] {S.STEP_LABELS[s['label']]}  ({state})")
        if not todo:
            p.write(S.ALL_DONE)
            return 0
        pick = _read(p, S.CHOOSE)
        if not pick:
            p.write(S.LEFT)
            return 0
        if not pick.isdigit() or not 1 <= int(pick) <= len(rows):
            p.write(S.CANCELLED)
            continue
        step, done, blocked = rows[int(pick) - 1]
        if done:
            continue
        if blocked:
            p.write(S.NEEDS_NOTE)
            continue
        bind = _choose_address(p) if step.get("asks_address") else None
        if step.get("asks_address") and bind is None:
            p.write(S.CANCELLED)
            continue
        run_step(p, step, bind)


def main_for_terminal(state: Path, state_args: list[str], *, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """Entry for `mas` with no arguments. Not a terminal on both ends: plain text, never a prompt."""
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    if not (stdin.isatty() and stdout.isatty()):
        return 2
    try:
        envrec_os.current().enable_ansi()
    except AttributeError:
        pass
    return run_menu(real_ports(state, state_args))
