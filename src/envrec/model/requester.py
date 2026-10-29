"""Ask a model for one Action: validate, retry once with the error text, then fall back."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from envrec.contracts import Action
from envrec.model.base import ModelAdapter, ModelError
from envrec.model.parse import ActionParseError, parse_action_repairing

Emit = Callable[[str, dict[str, Any]], None]
SNIPPET = 200


@dataclass
class RequestResult:
    action: Action
    attempts: int
    used_fallback: bool
    errors: list[str] = field(default_factory=list)
    repaired: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0


async def request_action(
    adapter: ModelAdapter,
    system: str,
    user: str,
    *,
    fallback: Action,
    emit: Emit | None = None,
    repair_tools: set[str] | None = None,
    retry_hint: str = "",
    pick: Callable[[], ModelAdapter] | None = None,
    observe: Callable[[bool], None] | None = None,
) -> RequestResult:
    """Two attempts at most. The second prompt carries the first attempt's error."""
    emit = emit or (lambda _type, _payload: None)
    result = RequestResult(action=fallback, attempts=0, used_fallback=True)
    prompt = user
    for attempt in (1, 2):
        result.attempts = attempt
        model = pick() if pick else adapter
        try:
            done = await model.complete(system, prompt)
        except ModelError as exc:
            error = f"backend error: {exc}"
            if observe:
                observe(False)
            result.errors.append(error)
            emit("model_invalid", {"attempt": attempt, "error": error})
        else:
            result.prompt_tokens += done.prompt_tokens
            result.completion_tokens += done.completion_tokens
            result.latency_s += done.latency_s
            emit("model_call", {"attempt": attempt, "latency_s": round(done.latency_s, 3),
                                "prompt_tokens": done.prompt_tokens,
                                "completion_tokens": done.completion_tokens})
            try:
                result.action, note = parse_action_repairing(done.text, repair_tools)
                result.used_fallback = False
                if observe:
                    observe(True)
                if note:
                    result.repaired += 1
                    emit("model_repaired", {"attempt": attempt, "note": note})
                return result
            except ActionParseError as exc:
                error = str(exc)
                if observe:
                    observe(False)
                result.errors.append(error)
                emit("model_invalid", {"attempt": attempt, "error": error,
                                       "reply": done.text[:SNIPPET]})
        prompt = (
            f"{user}\n\nYour previous reply was rejected: {error}\n"
            + (f"{retry_hint}\n" if retry_hint else "")
            + "Reply again with exactly one valid JSON Action and nothing else."
        )
    emit("model_fallback", {"errors": result.errors, "action": fallback.action.value})
    return result
