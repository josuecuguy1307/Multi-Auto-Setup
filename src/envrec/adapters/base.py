"""One interface for every source of tools: builtin probes, REA, replay devices.

The loop never sees a source's raw tool list. It sees ToolSpecs from `tool_specs()` and
gets a ProbeResult back from `run()`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from envrec.contracts import ProbeResult, Target, ToolSpec
from envrec.store import Store


@dataclass(frozen=True)
class ToolContext:
    store: Store
    target: Target
    event_id: str | None = None  # the event that recorded this call; goes into Evidence


class Adapter(ABC):
    name: str

    def close(self) -> None:
        """Called once when a run ends: release what the adapter left open (a server it started, say) and leave a record of it. Nothing by default."""

    @abstractmethod
    def tool_specs(self) -> list[ToolSpec]: ...

    def available(self, tool: str, target: Target, store: Store) -> bool:
        """Extra gate beyond kind/ring/prerequisites: False hides the tool from the menu right now."""
        return True

    @abstractmethod
    async def run(self, tool: str, args: dict[str, Any], ctx: ToolContext) -> ProbeResult:
        """Run one tool. Never raises for expected failures: return ok=False with an error."""
