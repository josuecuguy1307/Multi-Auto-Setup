"""Approval gate and per-device undo log.

Every write-tier action gets a fresh prompt showing the exact command, the target id, the checksum of
anything transferred, and the ToolSpec origin. Nothing is cached: each action asks again, and there is no
"yes to all". An undo entry is recorded BEFORE a write runs, so a half-finished action is still reversible;
a denied action records nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from envrec.contracts import Actor, Event, Origin
from envrec.store import Store


@dataclass(frozen=True)
class PendingAction:
    recipe: str
    command: str  # the exact command that will run on the target (rendered from a template)
    target_id: str
    origin: Origin
    checksum: str | None = None  # sha256 of any file being transferred
    artifact: str | None = None  # name and version of that file

    def render(self) -> str:
        lines = ["=== APPROVAL REQUIRED (applies to this one action only) ===",
                 f"recipe:  {self.recipe}", f"target:  {self.target_id}", f"origin:  {self.origin.value}",
                 f"command: {self.command}"]
        if self.checksum:
            lines.append(f"file:    {self.artifact or '(unnamed)'}  sha256 {self.checksum}")
        lines.append("Type 'approve' to run exactly this, anything else denies.")
        return "\n".join(lines)


class ApprovalGate:
    def __init__(self, store: Store, ask: Callable[[str], str] = input, show: Callable[[str], None] = print) -> None:
        self.store, self.ask, self.show = store, ask, show
        self.asked = 0

    def request(self, pending: PendingAction) -> bool:
        self.show(pending.render())
        self.asked += 1
        granted = self.ask("> ").strip().lower() == "approve"  # a fresh prompt every time; nothing carries over
        self.store.append_event(Event(actor=Actor.human, type="approval", payload={
            "recipe": pending.recipe, "target": pending.target_id, "origin": pending.origin.value,
            "command": pending.command, "checksum": pending.checksum, "artifact": pending.artifact, "granted": granted}))
        return granted


class UndoLog:
    """What has been installed on one device, replayed from the append-only event log."""

    def __init__(self, store: Store, target_id: str) -> None:
        self.store, self.target_id = store, target_id

    def record(self, step: str, undo_template: str, values: dict[str, object]) -> None:
        self.store.append_event(Event(actor=Actor.loop, type="undo_entry", payload={
            "target": self.target_id, "step": step, "undo": undo_template, "values": values}))

    def mark_undone(self) -> None:
        self.store.append_event(Event(actor=Actor.loop, type="undo_done", payload={"target": self.target_id}))

    def pending(self) -> list[dict]:
        """Entries recorded since the last full undo, oldest first."""
        out: list[dict] = []
        for e in self.store.events():
            if e.payload.get("target") != self.target_id:
                continue
            if e.type == "undo_entry":
                out.append(e.payload)
            elif e.type == "undo_done":
                out = []
        return out
