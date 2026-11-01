"""The board's vocabulary: the six message types of docs/NETWORK_CONTRACT.md section 5, the sensitivity levels, the actors, and the typed refusals.

Every board entry carries `task_id`, `task_type`, `sensitivity` and `actor` as envelope metadata (the node card and the published skill have no task: those two stay empty). The payload holds the
type's own fields. Nothing here picks a route: the board only records what the router decided (see `board.Board.record_route_decision`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from envrec_board.strings import REFUSALS

MESSAGE_TYPES = ("node_card", "task", "lease", "progress", "result", "published_skill")
SENSITIVITY = ("public", "internal", "confidential")
RANK = {s: i for i, s in enumerate(SENSITIVITY)}
ACTORS = ("MODEL", "GUARD", "SCRIPT", "OWNER", "CLAUDE-CODE", "ROUTER")
FORBIDDEN_KEYS = ("code", "password", "secret", "token", "private", "key_pem")  # same rule as the coordinator's EventLog: a payload key that could carry a secret is refused

# required payload fields per type (NETWORK_CONTRACT 5.1-5.6)
REQUIRED: dict[str, tuple[str, ...]] = {
    "node_card": ("node_id", "os_family", "hardware", "inference", "harnesses", "capabilities"),
    "task": ("input", "expected", "test", "created_by"),
    "lease": ("lease_id", "node_id", "deadline", "attempt"),
    "progress": ("lease_id", "step", "note"),
    "result": ("lease_id", "output", "evidence", "test_result"),
    "published_skill": ("skill", "hash"),
}



class BoardError(Exception):
    """A refusal with a typed code. The board records it (GUARD) and raises it; nothing changed."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)

    @property
    def text(self) -> str:
        return REFUSALS.get(self.code, self.code) + (f" ({self.detail})" if self.detail else "")


@dataclass
class Entry:
    """One board event as read back from the log."""
    n: int
    type: str  # `board.<message type>` or `board.<operation>`
    tag: str
    task_id: str | None
    task_type: str | None
    sensitivity: str
    actor: str
    payload: dict[str, Any] = field(default_factory=dict)
    at: float = 0.0

    def to_dict(self) -> dict:
        return {"n": self.n, "type": self.type, "tag": self.tag, "task_id": self.task_id, "task_type": self.task_type, "sensitivity": self.sensitivity, "actor": self.actor,
                "payload": self.payload, "at": self.at}

    @classmethod
    def from_dict(cls, d: dict) -> "Entry":
        return cls(n=d["n"], type=d["type"], tag=d["tag"], task_id=d.get("task_id"), task_type=d.get("task_type"), sensitivity=d["sensitivity"], actor=d["actor"],
                   payload=d.get("payload", {}), at=d.get("at", 0.0))


def highest(*levels: str) -> str:
    """A derived item takes the highest level of its inputs."""
    return max(levels, key=lambda s: RANK[s]) if levels else "public"


def check_payload_keys(payload: dict) -> None:
    bad = [k for k in payload if any(f in k.lower() for f in FORBIDDEN_KEYS)]
    if bad:
        raise BoardError("secret_key", ", ".join(bad))
