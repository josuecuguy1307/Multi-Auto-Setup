"""Sensitivity in code (docs/NETWORK_CONTRACT.md 6; docs/ROUTER_API.md section 5). Nothing here depends on a prompt: it is the creation guard and the outbound filter."""

from __future__ import annotations

import copy
from typing import Any

from envrec_coordinator.router import strings
from envrec_coordinator.router.model import SENSITIVITIES, Refusal, Task


def highest(*levels: str) -> str:
    """The highest of several levels (an unknown one counts as confidential: fail closed)."""
    ranks = [SENSITIVITIES.index(x) if x in SENSITIVITIES else len(SENSITIVITIES) - 1 for x in levels]
    return SENSITIVITIES[max(ranks)]


def creation_refusal(task: Task, effective: str | None = None) -> Refusal | None:
    """A task with no level, or a confidential one (its own level or inherited from an input) asked from a cloud chat, is refused before anything else (R1). No default level is ever guessed."""
    if task.sensitivity not in SENSITIVITIES:
        return Refusal(task_id=task.task_id, code="sensitivity_missing", reason=strings.refusal_text("sensitivity_missing"))
    if (effective or task.sensitivity) == "confidential" and task.entry_chat.kind == "cloud":
        return Refusal(task_id=task.task_id, code="confidential_from_cloud_chat", reason=strings.refusal_text("confidential_from_cloud_chat"), details={"entry_chat": task.entry_chat.model_dump()})
    return None


def filter_outbound(intent: dict[str, Any], entry_chat: dict[str, Any] | None, *, outbound_policy: dict | None = None) -> dict[str, Any]:
    """What may leave toward the chat the request came from. Total and pure. Unknown level or unknown entry kind is treated as confidential / cloud (fail closed)."""
    sens = intent.get("sensitivity")
    kind = (entry_chat or {}).get("kind")
    if sens not in SENSITIVITIES:
        sens = "confidential"
    if kind not in ("local", "cloud"):
        kind = "cloud"
    if kind == "local" or sens == "public":
        return copy.deepcopy(intent)
    payload = intent.get("payload") if isinstance(intent.get("payload"), dict) else {}
    if sens == "confidential":
        keep = {k: str(payload[k])[:64] for k in ("task_id", "state", "machine") if k in payload}
        line = strings.REDACTED_LINE.format(task_id=keep.get("task_id", "?"), state=keep.get("state", "in progress"), machine=keep.get("machine", "your computers"))
        out = {k: intent[k] for k in ("kind", "run_id", "actor") if k in intent}
        return {**out, "sensitivity": "confidential", "payload": keep, "text_fallback": line, "redacted": True}
    op = outbound_policy or {}  # internal, cloud chat: the owner policy's redaction (identifiers dropped, size capped)
    drop, cap = set(op.get("drop_keys_internal", [])), int(op.get("max_chars", 500))
    safe = {k: (v[:cap] if isinstance(v, str) else v) for k, v in payload.items() if k not in drop}
    return {**{k: v for k, v in intent.items() if k not in ("payload", "text_fallback")}, "payload": safe, "text_fallback": str(intent.get("text_fallback", ""))[:cap], "redacted": True}
