"""A scripted fake model for tests: replies come from a list, in order."""

from __future__ import annotations

from collections.abc import Callable

from envrec.contracts import ModelKind, ModelProfile, RingPolicy
from envrec.model.base import Completion, ModelAdapter, ModelError

Reply = str | Callable[[str, str], str] | Exception


def scripted_profile(view_budget_facts: int = 8, menu_max: int = 6, name: str = "scripted",
                     ring_policy: RingPolicy = RingPolicy.model_decides, walk_for_model: bool = False) -> ModelProfile:
    return ModelProfile(
        name=name, kind=ModelKind.scripted, model_id="scripted", endpoint="scripted://",
        context_tokens=8192, view_budget_facts=view_budget_facts, menu_max=menu_max, ring_policy=ring_policy, walk_for_model=walk_for_model,
    )


class ScriptedModel(ModelAdapter):
    """Each call pops the next reply. A callable gets (system, user). An Exception is raised as ModelError."""

    def __init__(self, replies: list[Reply], profile: ModelProfile | None = None) -> None:
        super().__init__(profile or scripted_profile())
        self._replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str) -> Completion:
        self.calls.append((system, user))
        if not self._replies:
            raise ModelError("scripted model has no replies left")
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise ModelError(str(reply))
        text = reply(system, user) if callable(reply) else reply
        return Completion(text=text, prompt_tokens=len(user.split()), completion_tokens=len(text.split()))
