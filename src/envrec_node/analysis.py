"""machine_analysis: a node describes itself to its coordinator, read-only (skill `machine-analysis`).

It REUSES the repository's discovery: the Ring 0 probes (`envrec.probes.ring0`), the observe-tier discovery skills run through `envrec.skills.discover` where the operating system is
supported, and the workspace sizing table (`envrec.sizing`). The facts come from the repository's own Store and privacy gate; this module only groups them for the owner. It
never reads a secret: environment variables are listed by NAME, never by value."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import envrec_os
from envrec_net.task import MachineAnalysisParams

DISCOVERY_SKILLS = ("os-info", "hw-overview", "hw-storage", "app-tools", "app-agents", "app-model-servers", "app-installed")
SUPPORTED_SKILL_OSES = ("macos", "linux")  # the discovery skills' platforms today


def ai_env_names(env: dict[str, str] | None = None) -> list[dict]:
    """The names (never the values) of environment variables that look like an AI provider's key."""
    from envrec_os.probes import key_names
    return key_names(env)


def _entity_rows(facts, ekind: str, limit: int) -> list[dict]:
    """The attributes of the discovery entities of one kind (`app`, `agent`, `model_server`), as the repository's own Store holds them."""
    rows = []
    for f in facts:
        if ".entity." in f.key and isinstance(f.value, dict) and f.value.get("kind") == ekind:
            a = f.value.get("attrs", {}) or {}
            rows.append({k: a[k] for k in ("name", "category", "state", "kind", "seen_as", "version", "port") if k in a})
    rows.sort(key=lambda r: (str(r.get("name", r.get("kind", ""))), str(r.get("port", ""))))
    return rows[:limit]


def run_analysis(params: MachineAnalysisParams, *, adapters=None) -> tuple[dict, list[dict]]:
    """(grouped result, evidence). Blocking: the node runs it in a worker thread."""
    from envrec.probes import ring0
    started = time.monotonic()
    limits: list[str] = []
    evidence: list[dict] = []
    values: dict = {}
    for fn in (ring0.read_os, ring0.read_cpu_ram, ring0.read_disks):
        try:
            out = fn()
            values.update(out.values)
            evidence.append({"provider": out.provider, "tool": fn.__name__, "keys": sorted(out.values)})
        except Exception as exc:  # a probe never takes the analysis down
            limits.append(f"{fn.__name__}: {type(exc).__name__}")
    groups: dict = {
        "system": {k: values[k] for k in ("os.name", "os.version", "os.arch", "os.kernel") if k in values},
        "hardware": {k: values[k] for k in ("hw.cpu_cores_physical", "hw.cpu_cores_logical", "hw.ram_total_gib", "hw.disk_total_gib", "hw.disk_free_gib") if k in values},
        "programs": {}, "ai": {}}
    plat = envrec_os.family()
    plat_name = {"darwin": "macos", "linux": "linux", "windows": "windows"}[plat]
    if plat_name in SUPPORTED_SKILL_OSES:
        from envrec.contracts import Target, TargetKind
        from envrec.discovery import LocalCollector, Salts
        from envrec.skills.discover import get_adapter, run_adapter
        from envrec.store import Store
        with tempfile.TemporaryDirectory(prefix="mas-analysis-") as tmp, Store(Path(tmp)) as st:
            target = st.put_target(Target(kind=TargetKind.host, ring=0, handle="this-machine"))
            col = LocalCollector()
            ran = []
            for sid in (adapters or DISCOVERY_SKILLS):
                sk = get_adapter(sid)
                if sk is None:
                    continue
                if sid == "app-installed" and not params.include_apps:
                    continue
                r = run_adapter(sk, col, st, target, salts=Salts())
                ran.append({"skill": sid, "status": r.status, "found": r.entities})
                if r.status != "ran":
                    limits.append(f"{sid}: {r.status}" + (f" ({r.reason[:80]})" if r.reason else ""))  # what could not be read here is said, not hidden
                evidence.append({"provider": "discovery-skill", "tool": sid, "status": r.status, "found": r.entities})
            facts = list(st.current_facts(target.id))
            by_key = {f.key: f.value for f in facts if ".entity." not in f.key and not f.key.startswith("ring.")}
            groups["system"].update({k: by_key[k] for k in ("os.build", "os.locale", "os.uname") if k in by_key})
            groups["hardware"].update({k: by_key[k] for k in ("hw.machine_model", "hw.machine_name", "hw.chip", "hw.core_layout") if k in by_key})
            groups["programs"] = {"tools_found": by_key.get("app.tools_found"), "by_category": by_key.get("app.tools_by_category", {}),
                                  "installed_apps": _entity_rows(facts, "app", params.max_apps) if params.include_apps else []}
            groups["ai"]["agents"] = [r for r in _entity_rows(facts, "app", 500) if str(r.get("category", "")).lower() in ("agent", "assistant", "ide", "cli", "mcp", "llm")]
            groups["ai"]["model_servers"] = _entity_rows(facts, "model_server", 20)
            groups["skills_run"] = ran
    else:
        limits.append(f"the repository's discovery skills cover macOS and Linux today; on {plat_name} only the Ring 0 probes ran (system, CPU, memory, disk)")
    from envrec_os import probes
    for name in ("os_details", "installed_programs", "gpus", "model_runtimes", "ai_clis", "memory_for_models"):
        try:
            out = getattr(probes, name)(**({"limit": params.max_apps} if name == "installed_programs" else {}))
        except Exception as exc:  # a probe never takes the analysis down
            limits.append(f"{name}: {type(exc).__name__}")
            continue
        evidence.append({"provider": "envrec_os.probes", "tool": name})
        limits += out["limits"]
        f = out["facts"]
        if name == "os_details":
            groups["system"].update({k.removeprefix("machine."): v for k, v in f.items()})
        elif name == "installed_programs" and params.include_apps:
            groups["programs"]["installed_programs_count"] = f["machine.programs_count"]
            groups["programs"]["installed_programs"] = f["machine.programs"][:params.max_apps]
        elif name == "gpus":
            groups["hardware"]["gpus"] = f["machine.gpus"]
        elif name == "model_runtimes":
            groups["ai"]["runtimes"] = f["machine.runtimes"]
        elif name == "ai_clis":
            groups["ai"]["clis"] = f["machine.ai_clis"]
        elif name == "memory_for_models":
            groups["hardware"].update({k.removeprefix("machine."): v for k, v in f.items()})
            groups["ai"]["largest_model_that_fits"] = {"params_b": f["machine.largest_model_params_b"], "limited_by": f["machine.largest_model_limited_by"], "path": f["machine.model_path"]}
    groups["ai"]["api_key_variable_names"] = ai_env_names()  # names only: values are never read into this result
    groups["limits"] = limits
    groups["seconds"] = round(time.monotonic() - started, 1)
    return groups, evidence
