"""The per-user service layer: plan, install, read and remove the thing that starts the node (or the coordinator) when its owner signs in. It lives here because it runs programs and
touches the operating system (conformance P1/H1); WHAT to write and run is data in `skills/os-service/references/service.json`, which this module reads.

Nothing here needs administrator rights, and nothing is done before `plan()` has been shown to the owner: `plan()` only reads, `install()` runs what the plan listed."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

import envrec_os
from envrec_os import SKILLS_DIR

SERVICE_DIR = SKILLS_DIR / "os-service" / "references"
SERVICE_FILE = SERVICE_DIR / "service.json"
ROLES = ("node", "coordinator")
RUN_TIMEOUT_S = 30

Runner = Callable[[Sequence[str]], "tuple[int, str]"]


class ServiceRefused(Exception):
    """A typed refusal: `reason` is a stable code, the text is for the owner."""

    def __init__(self, reason: str, text: str) -> None:
        super().__init__(text)
        self.reason, self.text = reason, text


def default_runner(argv: Sequence[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(list(argv), capture_output=True, text=True, timeout=RUN_TIMEOUT_S, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, f"{type(exc).__name__}: {str(exc)[:120]}"
    return done.returncode, ((done.stdout or "") + (done.stderr or "")).strip()[:2000]


@dataclass
class ServicePlan:
    role: str
    family: str
    kind: str
    label: str
    file: Path | None
    content: str | None
    command: str  # the exact program line that will run at sign-in
    pre: list[list[str]] = field(default_factory=list)
    start: list[list[str]] = field(default_factory=list)
    stop: list[list[str]] = field(default_factory=list)
    post_remove: list[list[str]] = field(default_factory=list)
    status_cmd: list[str] | None = None
    retries: int = 0  # how many times a failing start command is tried again (launchd answers 'Input/output error' for a moment after a bootout)
    retry_delay_s: float = 1.0
    what: str = ""
    undo: str = ""
    when: str = ""

    def describe(self) -> str:
        """What the owner is shown before anything is written."""
        lines = [self.what, f"  When it starts: {self.when}", f"  Line that runs: {self.command}"]
        if self.file is not None:
            lines.append(f"  File that is written: {self.file}")
        for c in self.pre + self.start:
            lines.append("  Command: " + _show(c))
        lines.append(f"  How to undo it: {self.undo}")
        return "\n".join(lines)


def _show(argv: Sequence[str]) -> str:
    return subprocess.list2cmdline(list(argv)) if envrec_os.family() == "windows" else shlex.join(list(argv))


def load_table() -> dict:
    return json.loads(SERVICE_FILE.read_text(encoding="utf-8"))


def _fill(text: str, values: dict[str, str]) -> str:
    return re.sub(r"\{([a-z_]+)\}", lambda m: values.get(m.group(1), m.group(0)), text)


def _fill_all(rows: list[list[str]], values: dict[str, str]) -> list[list[str]]:
    return [[_fill(a, values) for a in row] for row in rows]


def program_line(family: str, argv: Sequence[str]) -> str:
    """The program and its arguments as ONE line in the quoting rules of the family (a Windows registry value and a systemd ExecStart are strings, not argument lists)."""
    return subprocess.list2cmdline(list(argv)) if family == "windows" else shlex.join(list(argv))


def _uid() -> str:
    return str(os.getuid()) if hasattr(os, "getuid") else "0"


def _available(entry: dict, runner: Runner, env: dict[str, str]) -> bool:
    need = entry.get("needs_env_any")
    if need and not any(env.get(n) for n in need):
        return False
    probe = entry.get("probe")
    if probe:
        rc, _ = runner(_fill_all([probe], {"uid": _uid()})[0])
        return rc == 0
    return True


def plan(role: str, argv: Sequence[str], *, family: str | None = None, runner: Runner = default_runner, env: dict[str, str] | None = None) -> ServicePlan:
    """Choose the first user-level manager of this family that is available and describe what installing it would do. Reads and probes only; writes nothing."""
    if role not in ROLES:
        raise ServiceRefused("unknown_role", f"The role must be one of: {', '.join(ROLES)}.")
    env = dict(os.environ) if env is None else env
    family = family or envrec_os.family()
    table = load_table()
    rows = table.get(family)
    if not rows:
        raise ServiceRefused("no_user_service_manager", f"There is no known user service manager for the '{family}' family.")
    for entry in rows:
        if not _available(entry, runner, env):
            continue
        label = _fill(table["label"], {"role": role})
        values = {"role": role, "label": label, "uid": _uid(), "value_name": _fill(entry.get("value_name") or "", {"role": role})}
        argv = list(argv)
        if entry.get("windowless_python") and argv:
            w = Path(argv[0]).with_name(entry["windowless_python"])
            if w.is_file():
                argv[0] = str(w)
        command = program_line(family, argv)
        values["command"] = command
        file = None
        content = None
        if entry.get("file"):
            file = envrec_os._expand(_fill(entry["file"], values), env)
            values["file"] = str(file)
            content = _render(entry, values, argv)
        return ServicePlan(role, family, entry["kind"], label, file, content, command,
                           pre=_fill_all(entry.get("pre", []), values), start=_fill_all(entry.get("start", []), values), stop=_fill_all(entry.get("stop", []), values),
                           post_remove=_fill_all(entry.get("post_remove", []), values), retries=int(entry.get("retries", 0)), retry_delay_s=float(entry.get("retry_delay_s", 1.0)),
                           status_cmd=_fill_all([entry["status"]], values)[0] if entry.get("status") else None,
                           what=_fill(entry["what"], values), undo=_fill(entry["undo"], values), when=_fill(entry["when"], values))
    raise ServiceRefused("no_user_service_manager", "This computer has no user service manager available. Start the program by hand with `mas node run`; nothing was changed.")


def _render(entry: dict, values: dict[str, str], argv: Sequence[str]) -> str:
    text = (SERVICE_DIR / entry["template"]).read_text(encoding="utf-8")
    values = dict(values)
    values["program_arguments_xml"] = "\n".join(f"    <string>{escape(a)}</string>" for a in argv)
    return _fill(text, values)


def install(p: ServicePlan, *, runner: Runner = default_runner) -> list[str]:
    """Run a plan the owner approved. Returns the log lines. A command that fails undoes what this call wrote and raises, so a half-install is never left behind."""
    log: list[str] = []
    wrote = False
    if p.file is not None and p.content is not None:
        p.file.parent.mkdir(parents=True, exist_ok=True)
        p.file.write_text(p.content, encoding="utf-8")
        wrote = True
        log.append(f"wrote {p.file}")
    for c in p.pre + p.start:
        rc, out = runner(c)
        for _ in range(p.retries if c in p.start else 0):
            if rc == 0:
                break
            time.sleep(p.retry_delay_s)
            rc, out = runner(c)
        log.append(f"{_show(c)} -> {rc}")
        if rc != 0:
            if wrote and p.file is not None:
                try:
                    p.file.unlink()
                except OSError:
                    pass
            raise ServiceRefused("command_failed", f"`{_show(c)}` failed ({rc}): {out[:200]}. What was written was undone.")
    return log


def uninstall(p: ServicePlan, *, runner: Runner = default_runner) -> list[str]:
    """Stop and remove, whatever state it is in. Idempotent: a missing file or an already-stopped unit is not an error."""
    log: list[str] = []
    for c in p.stop:
        rc, _ = runner(c)
        log.append(f"{_show(c)} -> {rc}")
    if p.file is not None:
        try:
            p.file.unlink()
            log.append(f"deleted {p.file}")
        except FileNotFoundError:
            pass
    for c in p.post_remove:
        rc, _ = runner(c)
        log.append(f"{_show(c)} -> {rc}")
    return log


def installed(p: ServicePlan, *, runner: Runner = default_runner) -> bool:
    if p.file is not None:
        return p.file.is_file()
    if p.status_cmd:  # a registry value has no file: ask
        rc, _ = runner(p.status_cmd)
        return rc == 0
    return False


def running(p: ServicePlan, *, runner: Runner = default_runner) -> bool | None:
    """True/False when the manager can say, None when it cannot (a login entry has no state)."""
    if not p.status_cmd or p.kind in ("run-key",):
        return None
    rc, _ = runner(p.status_cmd)
    return rc == 0


def current_python() -> str:
    return sys.executable
