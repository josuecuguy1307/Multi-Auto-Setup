"""The targets.yaml gate. Only a TargetConfig (written by the human) can make a device reachable.

This module deliberately does not import the mission card: mission text has no path into a decision.
"""

from __future__ import annotations

from envrec.contracts import Decision, PolicyDecision, Target, ToolSpec
from envrec.mission.models import TargetConfig
from envrec.policy.policy import PolicyState, decide


def decide_for_fleet(tool: ToolSpec, target: Target, config: TargetConfig,
                     state: PolicyState | None = None) -> PolicyDecision:
    """Remote/fleet decisions: a target must be an entry in targets.yaml (by id), then normal policy applies.
    A device merely seen by discovery is never accepted, even if its fingerprint matches an entry."""
    entry = config.get(target.handle)
    if entry is None:
        return PolicyDecision(decision=Decision.deny, tool=tool.name, target_id=target.id,
                              reason=f"target {target.handle!r} is not an entry in targets.yaml")
    return decide(tool, target.model_copy(update={"authorized": True}), state)
