"""The builtin Ring 0 adapter: this machine's own probes, exposed through the same interface as any source."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from envrec.adapters.base import Adapter, ToolContext
from envrec.contracts import (
    Coverage,
    CreatedBy,
    Evidence,
    Fact,
    FactStatus,
    Origin,
    ProbeResult,
    TargetKind,
    Tier,
    ToolSpec,
    Unknown,
)
from envrec.probes import ring0
from envrec.probes.ring0 import ProbeOut

Runner = Callable[[dict[str, Any]], ProbeOut]  # gets the already-known values, returns the probe output


def _plain(fn: Callable[[], ProbeOut]) -> Runner:
    return lambda _known: fn()


# name -> (description, produced keys, required keys, cost, runner)
_TOOLS: dict[str, tuple[str, list[str], list[str], str, Runner]] = {
    "read_os": ("Read OS name, version, architecture and kernel with Python.",
                ["os.name", "os.version", "os.arch", "os.kernel"], [], "cheap", _plain(ring0.read_os)),
    "read_os_native": ("Cross-check the OS facts with the system's own commands.",
                       ["os.name", "os.version", "os.arch", "os.kernel"], [], "cheap",
                       _plain(ring0.read_os_native)),
    "read_cpu_ram": ("Read core counts and total memory with psutil.",
                     ["hw.cpu_cores_physical", "hw.cpu_cores_logical", "hw.ram_total_gib"], [], "cheap",
                     _plain(ring0.read_cpu_ram)),
    "read_hw_native": ("Read CPU model and cross-check memory with system commands.",
                       ["hw.cpu_model", "hw.cpu_cores_physical", "hw.cpu_cores_logical", "hw.ram_total_gib"],
                       [], "cheap", _plain(ring0.read_hw_native)),
    "read_disks": ("Read total and free space of the home disk.",
                   ["hw.disk_total_gib", "hw.disk_free_gib"], [], "cheap", _plain(ring0.read_disks)),
    "read_battery": ("Check whether a battery is present.",
                     ["hw.battery_present"], [], "cheap", _plain(ring0.read_battery)),
    "read_display": ("Check whether a display is attached.",
                     ["hw.display_present"], [], "medium", _plain(ring0.read_display)),
    "read_virtualization": ("Detect whether this runs in a container or virtual machine.",
                            ["host.virtualization"], [], "cheap", _plain(ring0.read_virtualization)),
    "read_tool_versions": ("List versions of common developer tools on this machine.",
                           ["host.tools"], [], "medium", _plain(ring0.read_tool_versions)),
    "read_net_interfaces": ("List this machine's own network interfaces that hold an address.",
                            ["net.interfaces"], [], "cheap", _plain(ring0.read_net_interfaces)),
    "classify_host": ("Infer host kind and capabilities from the facts already known.",
                      ["host.kind", "host.capabilities"],
                      ["os.name", "hw.battery_present", "hw.display_present", "host.virtualization",
                       "host.tools", "net.interfaces"], "cheap", ring0.classify_host),
}

RING0_KEYS: list[str] = sorted({k for _, produced, _, _, _ in _TOOLS.values() for k in produced})
_CONFIDENCE = {"rules": 0.7, "native": 0.95, "platform": 0.95, "psutil": 0.9, "stdlib": 0.95, "which": 0.95}


class BuiltinRing0Adapter(Adapter):
    name = "builtin"

    def tool_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=name, ring=0, tier=Tier.observe, target_kinds=[TargetKind.host],
                one_line=desc, cost=cost, origin=Origin.builtin, rung=1, source=self.name,
                produces=produced, requires=required,
            )
            for name, (desc, produced, required, cost, _run) in _TOOLS.items()
        ]

    async def run(self, tool: str, args: dict[str, Any], ctx: ToolContext) -> ProbeResult:
        entry = _TOOLS.get(tool)
        if entry is None:
            return ProbeResult(ok=False, error=f"builtin adapter has no tool {tool!r}")
        known = {f.key: f.value for f in ctx.store.current_facts(ctx.target.id) if f.status != FactStatus.unknown}
        try:
            out = await asyncio.to_thread(entry[4], known)
        except Exception as exc:  # a probe must never take the run down
            return ProbeResult(ok=False, error=f"{type(exc).__name__}: {str(exc)[:200]}")

        raw_ref = ctx.store.put_raw(out.raw)
        confidence = _CONFIDENCE.get(out.provider, 0.8)
        facts: list[Fact] = []
        unknowns: list[Unknown] = []
        for key, value in out.values.items():
            if value is None:
                facts.append(Fact(target_id=ctx.target.id, key=key, created_by=CreatedBy.probe))
                unknowns.append(Unknown(key=key, question=out.reasons.get(key, "could not be determined")[:200]))
                continue
            inferred = key == "host.kind"
            facts.append(Fact(
                target_id=ctx.target.id, key=key, value=value,
                status=FactStatus.hypothesis if inferred else FactStatus.tested,
                evidence=[Evidence(provider=out.provider, probe=tool, event_id=ctx.event_id, raw_ref=raw_ref,
                                   confidence=confidence, limitations=out.limitations)],
                created_by=CreatedBy.probe,
            ))
        known_keys = [f.key for f in facts if f.status != FactStatus.unknown]
        return ProbeResult(
            ok=True, facts=facts, raw_ref=raw_ref, unknowns=unknowns,
            coverage=Coverage(scope=tool, status="complete" if not unknowns else "partial",
                              checked=known_keys, unknown=[u.key for u in unknowns]),
        )
