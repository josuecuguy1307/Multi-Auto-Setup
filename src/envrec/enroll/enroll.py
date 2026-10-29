"""Enrollment: the ONLY code that writes targets.yaml. Human-only, interactive, one device per confirmation.

It refuses to run: without an interactive terminal, inside the loop, or in a worker build. It gives the
harness no access by itself: access exists only because the human already set up keys and services on the
device. After enrolling it runs exactly one read-only reachability check over the proposed transport.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from envrec.contracts import Actor, Event
from envrec.guard import LOOP_ACTIVE, in_worker_build
from envrec.inventory.report import Candidate
from envrec.mission.loader import load_targets
from envrec.mission.models import (
    AdbBlock,
    DockerExecBlock,
    EntryLimits,
    SerialBlock,
    SshBlock,
    TcpBlock,
    TargetConfig,
    TargetEntry,
    Transport,
)
from envrec.remote.planner import HINTS, plan_transport
from envrec.remote.sizing import RAM_TABLE, choose_model
from envrec.store import Store

CONFIRM_TEXT = "I own this device or have the owner's permission"
DEFAULT_DISK_GB, DEFAULT_PARAMS_B = 2.0, 0.5

Reachability = Callable[[TargetEntry, Transport], bool]  # one read-only check over the chosen transport only


class EnrollmentRefused(Exception):
    """Enrollment was called where it must never run."""


@dataclass
class EnrollResult:
    enrolled: bool
    entry: TargetEntry | None = None
    reason: str = ""


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (("d-" + s) if not s or not s[0].isalpha() else s)[:32].rstrip("-") or "device"


def _write_atomic(path: Path, config: TargetConfig) -> None:
    text = yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".targets-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def _ask_connection(t: Transport, slug: str, ask: Callable[[str], str]) -> dict:
    if t == Transport.ssh:
        user = ask("SSH user on the device: ").strip()
        port = ask("SSH port [22]: ").strip() or "22"
        return {"ssh": SshBlock(user=user, port=int(port), key_env=f"ENVREC_KEY_{slug.upper().replace('-', '_')}")}
    if t == Transport.docker_exec:
        return {"docker_exec": DockerExecBlock(container=ask("Container name: ").strip())}
    if t == Transport.adb_usb:
        return {"adb_usb": AdbBlock(serial=ask("adb serial (from `adb devices`): ").strip())}
    if t == Transport.tcp:
        return {"tcp": TcpBlock(host=ask("Instrument address on your own network: ").strip(), port=int(ask("Port: ").strip()),
                                allow_send=ask("May discovery send the fixed `*IDN?` identify query? [no]: ").strip().lower() in ("y", "yes"))}
    return {"serial": SerialBlock(path=ask("Serial port path (e.g. /dev/ttyUSB0): ").strip(),
                                  baud=int(ask("Baud [115200]: ").strip() or "115200"),
                                  allow_send=ask("May discovery send the fixed identify queries on this port (otherwise it only listens)? [no]: ").strip().lower() in ("y", "yes"))}


def _limits_for(c: Candidate) -> tuple[float, float, str]:
    """Proposed max_disk_gb and max_model_params_b from the size table and what is known of the device."""
    vals = c.known_values()
    ram, disk = vals.get("hw.ram_total_gib"), vals.get("hw.disk_free_gib")
    if isinstance(ram, (int, float)) and isinstance(disk, (int, float)):
        budget = round(max(disk * 0.5, 1.0), 1)
        choice = choose_model(float(ram), float(disk), budget, 1000)
        return budget, max(choice.params_b, 0.5), f"from the size table: {ram} GiB RAM, {disk} GB free disk"
    return DEFAULT_DISK_GB, DEFAULT_PARAMS_B, "RAM or disk unknown: conservative defaults"


def screen(c: Candidate, entry: TargetEntry, transport_reasons: list[str], limit_note: str) -> str:
    facts = [f"  {k} = {v} ({s}, confidence {conf:.2f})" for k, (v, s, conf) in sorted(c.facts.items())] or ["  nothing recognized"]
    return "\n".join([
        f"=== Enroll candidate: {c.name} ({c.kind}) ===",
        f"Seen via: {c.seen_via}",
        "Recognized:", *facts,
        "Stays unknown: " + (", ".join(sorted(c.unknown)) or "nothing"),
        f"Proposed transport: {entry.allowed_transports[0].value} (because: {'; '.join(transport_reasons)})",
        f"Proposed limits: max_disk_gb={entry.max_disk_gb}, max_model_params={entry.limits.max_model_params} ({limit_note})",
        "Steps only the device owner can do (this harness performs none of them and never touches credentials):",
        f"  - {HINTS[entry.allowed_transports[0]]}",
        "Exact entry that will be written to targets.yaml:",
        *["  " + ln for ln in yaml.safe_dump(entry.model_dump(mode="json"), sort_keys=False).splitlines()],
    ])


def enroll(
    candidate: Candidate,
    *,
    targets_path: Path | str,
    store: Store,
    check: Reachability,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    is_tty: Callable[[], bool] = lambda: sys.stdin.isatty() and sys.stdout.isatty(),
) -> EnrollResult:
    """Enroll ONE candidate. Returns enrolled=False (and writes nothing) if the human declines."""
    if in_worker_build():
        raise EnrollmentRefused("enrollment is not part of the worker build")
    if LOOP_ACTIVE.get():
        raise EnrollmentRefused("enrollment cannot be called from the loop or from any model action")
    if not is_tty():
        raise EnrollmentRefused("enrollment needs an interactive terminal")

    path = Path(targets_path)
    config = load_targets(path)
    host = candidate.host
    if not host:
        host = input_fn(f"Address (hostname or IP) of {candidate.name}: ").strip()
    plan_facts = candidate.known_values()
    all_transports = list(Transport)
    pref_entry = TargetEntry.model_construct(id=_slug(candidate.name), allowed_transports=all_transports)
    proposed = plan_transport(pref_entry, plan_facts, {}).considered[0]
    disk, params, limit_note = _limits_for(candidate)
    slug = _slug(candidate.name)
    blocks = _ask_connection(proposed, slug, input_fn)  # facts about HOW to reach it, typed by the human; no secrets
    entry = TargetEntry(
        id=slug, name=candidate.name, host=host, os=str(plan_facts["os.name"]).lower() if plan_facts.get("os.name") else None,
        allowed_transports=[proposed], **blocks,
        limits=EntryLimits(max_disk_gb=disk, max_model_params=f"{params:g}B"), owner_confirmed=True)
    if config.get(entry.id):
        return EnrollResult(False, None, "already enrolled")

    from envrec.remote.planner import preference
    output_fn(screen(candidate, entry, preference(plan_facts)[1], limit_note))
    typed = input_fn(f"Type the device name ({candidate.name}) to enroll it, or press Enter to skip: ").strip()
    if typed != candidate.name:
        return EnrollResult(False, None, "skipped: name not typed")
    output_fn(f'"{CONFIRM_TEXT}"')
    if input_fn("Confirm [yes/no]: ").strip().lower() != "yes":
        return EnrollResult(False, None, "declined")

    config.targets.append(entry)
    _write_atomic(path, config)
    store.append_event(Event(actor=Actor.human, type="enrolled", payload={
        "target": entry.id, "host": entry.host, "transport": entry.allowed_transports[0].value,
        "max_disk_gb": entry.max_disk_gb, "max_model_params": entry.limits.max_model_params}))

    ok = bool(check(entry, entry.allowed_transports[0]))  # the one read-only check; no other transport or port
    entry = entry.model_copy(update={"reachability": "reachable" if ok else "unreachable"})
    config.targets[-1] = entry
    _write_atomic(path, config)
    store.append_event(Event(actor=Actor.human, type="enroll_check", payload={"target": entry.id, "reachable": ok}))
    if not ok:
        output_fn(f"Not reachable over {entry.allowed_transports[0].value}. {HINTS[entry.allowed_transports[0]]}")
    return EnrollResult(True, entry, "enrolled" if ok else "enrolled, unreachable")
