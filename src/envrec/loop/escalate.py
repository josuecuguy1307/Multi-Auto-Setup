"""Escalation: pure code, never a model decision.

A weak device's model is replaced by the controller's model (reached through a tunnel the controller
opens) in exactly two cases:
  1. after N consecutive invalid replies, the very next call goes to the controller's model;
  2. when the weak model reaches rung 6 (ask_human), that one turn is first routed to the controller's model.
Every escalated turn is recorded with its reason. A valid reply from the controller's model resets the count,
so the next turn goes back to the weak model.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from envrec.model.base import ModelAdapter


@dataclass
class Escalator:
    strong: ModelAdapter
    threshold: int
    open_tunnel: Callable[[], None] | None = None  # opens the tunnel to the controller's model, once, on first use
    close_tunnel: Callable[[], None] | None = None
    consecutive_invalid: int = 0
    escalated: list[dict] = field(default_factory=list)
    _opened: bool = False
    _current_turn: int = 0

    def start_turn(self, turn: int) -> None:
        self._current_turn = turn

    def pick(self, weak: ModelAdapter) -> ModelAdapter:
        if self.consecutive_invalid >= self.threshold:
            self._use_strong("invalid_threshold")
            return self.strong
        return weak

    def observe(self, valid: bool, *, from_strong: bool = False) -> None:
        self.consecutive_invalid = 0 if valid else self.consecutive_invalid + 1

    def route_rung6(self) -> ModelAdapter:
        self._use_strong("rung6_ask_human")
        return self.strong

    def _use_strong(self, reason: str) -> None:
        if not self._opened and self.open_tunnel:
            self.open_tunnel()
        self._opened = True
        self.escalated.append({"turn": self._current_turn, "reason": reason, "consecutive_invalid": self.consecutive_invalid})

    def close(self) -> None:
        if self._opened and self.close_tunnel:
            self.close_tunnel()
        self._opened = False
