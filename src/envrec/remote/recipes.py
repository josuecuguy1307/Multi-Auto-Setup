"""The closed recipe menu for remote targets. Each recipe is a ToolSpec; policy decides what each needs.

check, install_runtime, select_model, transfer_model, start_server, health_check, stop_server, uninstall.
Built-in recipes are builtin-origin. A recipe that comes from a generated probe (ladder rung 5, for an
unfamiliar OS) carries origin=generated and verified=False, and `decide()` refuses it on real targets until
a verifier passes it: it can only be registered here, never smuggled past policy.
"""

from __future__ import annotations

from envrec.contracts import Origin, TargetKind, Tier, ToolSpec

_KINDS = [TargetKind.net_host]


def _r(name: str, tier: Tier, line: str, rung: int = 1) -> ToolSpec:
    return ToolSpec(name=name, ring=2, tier=tier, target_kinds=_KINDS, one_line=line, origin=Origin.builtin,
                    rung=rung, source="recipes", verified=True)


RECIPES: list[ToolSpec] = [
    _r("check", Tier.observe, "Read-only preflight: architecture, OS, RAM limit, disk, GPU."),
    _r("install_runtime", Tier.write_approval, "Confirm the device can run the worker; install nothing else."),
    _r("select_model", Tier.observe, "Pick the largest model fitting RAM, disk and configured limits."),
    _r("transfer_model", Tier.write_approval, "Send the pinned worker and chosen model, checksum verified."),
    _r("start_server", Tier.write_approval, "Start the model server bound to localhost on the device."),
    _r("health_check", Tier.observe, "Ask the localhost server on the device whether it is up."),
    _r("stop_server", Tier.write_approval, "Stop the model server on the device."),
    _r("uninstall", Tier.write_approval, "Stop the server, remove everything installed, confirm nothing remains."),
]


def recipe_specs() -> list[ToolSpec]:
    return list(RECIPES)


def register_generated_recipe(name: str, tier: Tier, line: str) -> ToolSpec:
    """A recipe written by a generated probe. Unverified by construction; policy blocks it on real targets."""
    return ToolSpec(name=name, ring=2, tier=tier, target_kinds=_KINDS, one_line=line, origin=Origin.generated,
                    rung=5, source="recipes", verified=False)
