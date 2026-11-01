"""The intent: what the harness says to the owner, independent of any screen (docs/HARNESS_APP_STANDARD.md).

`ask_choice` (a closed question), `confirm` (an approval), `progress` (what is happening), `show_data` (network, machine, options, plan, run) and `text`. Every intent carries who acted
(actor), how sensitive it is, and a text fallback that is enough on its own: a chat that renders nothing else still lets the owner understand and answer it."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

KINDS = ("ask_choice", "confirm", "progress", "show_data", "text")
DATA_KINDS = ("network", "machine", "options", "plan", "run")
ACTORS = ("MODEL", "GUARD", "SCRIPT", "OWNER", "CLAUDE-CODE")
SENSITIVITY = ("public", "internal", "confidential")
RANK = {s: i for i, s in enumerate(SENSITIVITY)}
MAX_FALLBACK = 16000  # a plan is told whole: its commands are never cut to fit
PLACEHOLDERS = {"<mas>": "MAS_HOME"}  # the plan's own placeholder for the machine's Multi Auto Setup folder, as every owner-facing surface shows it (the plan and its hash keep the original)
HOST_EATEN = re.compile(r"<[^>]*>")  # what a chat may treat as an HTML tag and strip from a dialog or a message


def for_display(text: str) -> str:
    """The text of a plan (a command, a path, an undo) as every host can show it: the placeholder `<mas>` is an angle-bracket word that Open WebUI strips as a tag, which turned `rm -rf <mas>/downloads/x`
    into `rm -rf /downloads/x`, a delete from the disk root. `MAS_HOME` survives every host, and cannot be read as a root path. The mapping is one way to one word, so it is reversible (`undisplay`)."""
    for src, dst in PLACEHOLDERS.items():
        text = text.replace(src, dst)
    home = str(Path.home())
    if len(home) > 1:
        text = text.replace(home, "~")  # a path in the owner's own profile as `~`: no user name goes to a chat, and `~` is a character no host strips
    return text


def undisplay(text: str) -> str:
    for src, dst in PLACEHOLDERS.items():
        text = text.replace(dst, src)
    return text.replace("~", str(Path.home()))


class IntentInvalid(ValueError):
    pass


@dataclass
class Intent:
    id: str
    kind: str
    run_id: str | None
    actor: str
    sensitivity: str
    payload: dict
    text_fallback: str
    seq: int = 0
    ts: float = 0.0
    channel: str | None = None  # set when an owner decision came through a chat channel
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"id": self.id, "seq": self.seq, "ts": self.ts, "kind": self.kind, "run_id": self.run_id, "actor": self.actor, "sensitivity": self.sensitivity, "payload": self.payload,
                "text_fallback": self.text_fallback, **({"channel": self.channel} if self.channel else {})}


def numbered(options: list[dict]) -> str:
    """One line per option: its number, its label, and the id that answers it (a text-only chat can answer with either)."""
    return "\n".join(f"{i}. {o['label']}" + (f" ({o['detail']})" if o.get("detail") else "") + f" [id: {o['id']}]" for i, o in enumerate(options, 1))


def validate(i: Intent) -> Intent:
    """The rules every intent must satisfy before it leaves the core. Raises IntentInvalid with the reason."""
    if i.kind not in KINDS:
        raise IntentInvalid(f"unknown kind {i.kind!r}")
    if i.actor not in ACTORS:
        raise IntentInvalid(f"unknown actor {i.actor!r}")
    if i.sensitivity not in SENSITIVITY:
        raise IntentInvalid(f"unknown sensitivity {i.sensitivity!r}")
    if not isinstance(i.payload, dict):
        raise IntentInvalid("payload must be an object")
    if not isinstance(i.text_fallback, str) or not i.text_fallback.strip():
        raise IntentInvalid("every intent needs a text fallback")
    if len(i.text_fallback) > MAX_FALLBACK:
        raise IntentInvalid("text fallback too long")
    p = i.payload
    if i.kind == "show_data" and p.get("data_kind") not in DATA_KINDS:
        raise IntentInvalid(f"show_data needs data_kind in {DATA_KINDS}")
    if i.kind == "show_data" and p.get("data_kind") == "plan" and HOST_EATEN.search(i.text_fallback):  # the whole plan the owner reads is held to the rule of an approval: no word a chat could strip
        raise IntentInvalid("the plan text holds angle brackets a chat may strip, so the owner would read something else than what runs")
    if i.kind in ("ask_choice", "confirm"):
        if not isinstance(p.get("writes"), bool):
            raise IntentInvalid(f"{i.kind} must say whether it writes (payload.writes: true/false)")
    if i.kind == "ask_choice":
        opts = p.get("options")
        if not isinstance(opts, list) or not opts or not all(isinstance(o, dict) and o.get("id") and o.get("label") for o in opts):
            raise IntentInvalid("ask_choice needs a non-empty list of options with id and label")
        for n, o in enumerate(opts, 1):
            if f"{n}. {o['label']}" not in i.text_fallback:
                raise IntentInvalid(f"the text fallback must list option {n} as a numbered line")
    if i.kind == "confirm" and p["writes"]:
        for need in ("summary", "command", "undo"):  # every write shows the exact action and how to undo it (NETWORK_CONTRACT, hard limits)
            if not isinstance(p.get(need), str) or not p[need].strip():
                raise IntentInvalid(f"a confirmation that writes must show its exact {need}")
            if HOST_EATEN.search(p[need]):  # fail closed: a command a host would strip is not the command that runs, so it is not shown at all
                raise IntentInvalid(f"the {need} of a confirmation that writes holds angle brackets a chat may strip, so the owner would approve something else than what runs")
        if not isinstance(p.get("plan_hash"), str) or not p["plan_hash"].startswith("sha256:") or len(p["plan_hash"]) < 15:
            raise IntentInvalid("a confirmation that writes is bound to a plan hash (sha256:...)")
    return i
