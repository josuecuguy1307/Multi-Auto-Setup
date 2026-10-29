"""The append-only event log both roles write to: the project's `Store` (events cannot be updated or deleted). Pairing and connection events are written by CODE, so their tag is
GUARD (the code recorded or enforced); the owner's yes or no is written as the owner's (OWNER). A pairing code, a private key or a token never appears in a payload."""

from __future__ import annotations

from pathlib import Path

from envrec.contracts import Actor, Event
from envrec.store import Store

FORBIDDEN_KEYS = ("code", "password", "secret", "token", "private", "key_pem")


class EventLog:
    def __init__(self, directory: Path) -> None:
        self.store = Store(directory)

    def add(self, type: str, payload: dict | None = None, *, owner: bool = False) -> Event:
        payload = dict(payload or {})
        bad = [k for k in payload if any(f in k.lower() for f in FORBIDDEN_KEYS)]
        if bad:
            raise ValueError(f"event payload keys {bad} could carry a secret and are refused")
        return self.store.append_event(Event(actor=Actor.human if owner else Actor.loop, type=type, payload=payload))

    def events(self, type: str | None = None) -> list[Event]:
        return self.store.events(type=type)

    def close(self) -> None:
        self.store.close()
