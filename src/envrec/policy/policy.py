"""Policy: a pure function from (tool, target, state) to a decision with a reason.

Authorization comes only from `Target.authorized`, which only the human config file sets.
Nothing here reads model output.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from envrec.contracts import Decision, Origin, PolicyDecision, Target, Tier, ToolSpec

# Anything that deletes, formats, flashes or elevates is refused, whatever the tier says.
DENY_TAGS = frozenset({"delete", "format", "flash", "elevate", "sudo", "wipe", "erase"})
DENY_NAME_PARTS = ("delete", "remove", "format_disk", "flash", "firmware_write", "sudo", "wipe", "erase")


@dataclass(frozen=True)
class PolicyState:
    # (tool name, target id) pairs a human has approved.
    approved: frozenset[tuple[str, str]] = field(default_factory=frozenset)


def is_replay_target(target: Target) -> bool:
    """Replay devices are recorded transcripts, never real hardware."""
    return target.handle.startswith("replay:")


def decide(tool: ToolSpec, target: Target, state: PolicyState | None = None) -> PolicyDecision:
    state = state or PolicyState()

    def out(decision: Decision, reason: str) -> PolicyDecision:
        return PolicyDecision(decision=decision, reason=reason, tool=tool.name, target_id=target.id)

    if DENY_TAGS & {t.lower() for t in tool.tags} or any(p in tool.name for p in DENY_NAME_PARTS):
        return out(Decision.deny, "tool is on the destructive denylist (delete, format, flash, elevate)")

    if target.kind not in tool.target_kinds:
        return out(Decision.deny, f"tool does not apply to target kind {target.kind.value!r}")

    replay = is_replay_target(target)

    if tool.origin == Origin.generated and not tool.verified:
        if not replay:
            return out(Decision.deny, "generated tool is unverified and this target is real")
        return out(Decision.allow_logged, "unverified generated tool, allowed on a replay device only")

    if (tool.ring >= 2 or target.ring >= 2) and not target.authorized:
        return out(Decision.deny, "ring 2 and 3 work needs a target authorized in the human config")

    if tool.tier in (Tier.observe, Tier.read):
        return out(Decision.allow, f"{tool.tier.value} tier, read-only")

    if not target.authorized and not replay:
        return out(Decision.deny, "write tools need an authorized target")

    if tool.tier == Tier.write_logged:
        return out(Decision.allow_logged, "write allowed on an authorized target, every call logged")

    if (tool.name, target.id) in state.approved:
        return out(Decision.allow_logged, "write approved by a human, call logged")
    return out(Decision.needs_approval, "write needs human approval before it runs")
