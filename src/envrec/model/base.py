"""One interface for every model. The endpoint and model id always come from config."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from envrec.contracts import ModelProfile


class ModelError(Exception):
    """Transport or backend failure (timeout, connection refused, CLI error)."""


@dataclass(frozen=True)
class Completion:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    cost_usd: float | None = None


class ModelAdapter(ABC):
    def __init__(self, profile: ModelProfile) -> None:
        self.profile = profile

    @abstractmethod
    async def complete(self, system: str, user: str) -> Completion:
        """One request, one reply. Raises ModelError on backend failure."""
