"""Setup options built from a machine's analysis (skill `setup-options`). PURE CODE: the hard filters and the written ordering live here, and nothing a model says can change them. The model's
only part is to explain, in plain words, why each option is there and what limits it.

Inputs are plain dicts of the facts the probes returned (`machine.*`); sources are the repository's `catalog.lock.yaml` (labeled verified-by-code), the skill's `harnesses.json` (declared by
the project) and `evidence.json` (public evidence; empty is fine and is said)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from envrec_os import SKILLS_DIR

REF = SKILLS_DIR / "setup-options" / "references"
CATALOG_FILE = SKILLS_DIR.parent / "catalog.lock.yaml"
COST_RANK = {"free_local": 0, "subscription": 1, "metered_api": 2}
WRITTEN_ORDER = {"installed_runtime": 0, "catalog_runtime": 1, "installed_cli": 2, "api_key": 3}
ORDER_TEXT = ("cost class (free and local, then subscription, then metered API); then measured pass rate and measured time when they exist (none are measured yet, so they are skipped); then the written "
              "order (an installed runtime before a download, an installed command-line tool, an API key), one that is already set up or whose key variable was found before one that is not; then the larger model that fits; then the name")
MEMORY_SHARE = 0.7  # of the memory a model may use (a model must leave room for the system)
PROVISIONAL = "provisional, not measured here"
MAX_SHOWN = 3
OS_NAMES = {"macOS": "macos", "Darwin": "macos", "Windows": "windows", "Linux": "linux"}
ARCHES = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}


def load_catalog(path: Path | None = None) -> dict:
    p = path or CATALOG_FILE
    return yaml.safe_load(p.read_text()) if p.is_file() else {"runtime_assets": [], "model_files": []}


def load_harnesses() -> dict:
    return json.loads((REF / "harnesses.json").read_text())


def load_evidence() -> dict:
    return json.loads((REF / "evidence.json").read_text())


def catalog_summary(catalog: dict) -> dict:
    """What `catalog_lookup` shows the model: counts and the sources, not the rows."""
    rt = catalog.get("runtime_assets", [])
    return {"label": "verified-by-code (licenses and checksums read by the catalog generator)", "runtime_builds": len(rt), "runtimes": sorted({r["source"] for r in rt}),
            "model_files": len(catalog.get("model_files", [])), "licenses": sorted({m["license"] for m in catalog.get("model_files", [])})}


def evidence_for(evidence: dict, topic: str) -> dict:
    rows = [e for e in evidence.get("entries", []) if topic.lower() in json.dumps(e).lower()]
    return {"entries": rows, "note": "" if rows else "no public data"}


def _norm(facts: dict) -> dict:
    name = str(facts.get("machine.os.name", ""))
    os_ = next((v for k, v in OS_NAMES.items() if name.startswith(k)), "")
    return {"os": os_, "arch": ARCHES.get(str(facts.get("machine.os.arch", "")).lower(), ""), "path": facts.get("machine.model_path"), "vram": facts.get("machine.vram_gib"),
            "ram": float(facts.get("machine.ram_total_gib") or 0), "disk": float(facts.get("machine.disk_free_gib") or 0), "gpus": facts.get("machine.gpus") or [],
            "runtimes": facts.get("machine.runtimes") or [], "clis": facts.get("machine.ai_clis") or [], "keys": facts.get("machine.api_key_variable_names") or []}


def _backend(n: dict) -> str:
    vendors = {g.get("vendor") for g in n["gpus"]}
    if n["os"] == "linux" and "NVIDIA" in vendors:
        return "cuda"
    if n["os"] in ("windows", "linux") and vendors & {"NVIDIA", "AMD", "Intel"}:
        return "vulkan"
    return "cpu"


def _memory_budget(n: dict) -> tuple[float, str]:
    if n["path"] == "gpu" and n["vram"]:
        return round(n["vram"] * MEMORY_SHARE, 2), "VRAM"
    return round(n["ram"] * MEMORY_SHARE, 2), "RAM"


def candidates(facts: dict, catalog: dict, harnesses: dict) -> list[dict]:
    n = _norm(facts)
    out: list[dict] = []
    budget, mem_name = _memory_budget(n)
    for rt in n["runtimes"]:  # a runtime already on the machine: no download
        models = rt.get("models", [])
        out.append({"id": f"use-{rt['id']}", "kind": "local", "origin": "installed_runtime", "title": f"Use the {rt['name']} already on this machine", "cost_class": "free_local", "cloud": False,
                    "runtime": rt["name"], "models_present": [m["name"] for m in models], "license": "see the runtime's own license", "label": "verified-by-code (found on this machine by a probe)",
                    "memory_needed_gib": None, "disk_needed_gib": 0.0, "needs_download": False, "limits": ([] if models else ["it has no models yet"]) + (["it is reachable from the network: bind it to loopback"] if rt.get("exposed_beyond_loopback") else []),
                    "params_b": max([0.0] + [float(m["name"].split(":")[1].rstrip("b")) if ":" in m["name"] and m["name"].split(":")[1].rstrip("b").replace(".", "").isdigit() else 0.0 for m in models])})
    asset = next((a for a in catalog.get("runtime_assets", []) if a["os"] == n["os"] and a["arch"] == n["arch"] and a["backend"] == _backend(n) and a.get("runnable")), None) \
        or next((a for a in catalog.get("runtime_assets", []) if a["os"] == n["os"] and a["arch"] == n["arch"] and a["backend"] == "cpu" and a.get("runnable")), None)
    if asset is not None:
        for m in catalog.get("model_files", []):
            out.append({"id": f"catalog-{m['params_b']}b-{m['repo'].split('/')[-1][:30]}", "kind": "local", "origin": "catalog_runtime", "title": f"{asset['source']} ({asset['backend']}) with {m['repo'].split('/')[-1]} {m['quantization']}",
                        "cost_class": "free_local", "cloud": False, "runtime": f"{asset['source']} {asset['tag']} ({asset['backend']})", "model": m["repo"], "params_b": float(m["params_b"]), "license": m["license"],
                        "runtime_license": asset["license"], "label": "verified-by-code (license and checksum read from the source)", "source": m["license_source"], "memory_needed_gib": float(m["min_ram_gib"]),
                        "disk_needed_gib": round((m["bytes"] + asset["bytes"]) / 2 ** 30, 2), "needs_download": True, "limits": []})
    for cli in n["clis"]:
        h = next((c for c in harnesses["cli"] if c["id"] == cli["id"]), {})
        out.append({"id": f"cli-{cli['id']}", "kind": "cli", "origin": "installed_cli", "title": f"{cli['name']} (already installed)", "cost_class": "subscription", "cloud": True, "runtime": cli["name"],
                    "license": h.get("terms", "its own terms apply (not evaluated here)"), "label": "declared-by-project (found on this machine by a probe)", "source": h.get("source", ""), "memory_needed_gib": None,
                    "disk_needed_gib": 0.0, "needs_download": False, "set_up": bool(cli.get("set_up")), "limits": ["CLOUD: your prompts and files go to the provider"] + ([] if cli.get("set_up") else ["no sign-in was found (a configuration file's existence is checked; nothing is read)"])})
    present = {k["provider"] for k in n["keys"] if k["provider"] != "other"}
    for p in harnesses["api_providers"]:
        have = p["id"] in present
        out.append({"id": f"api-{p['id']}", "kind": "api", "origin": "api_key", "title": f"{p['name']} with your API key" + (" (a key variable was found)" if have else ""), "cost_class": "metered_api", "cloud": True,
                    "runtime": p["name"], "license": "the provider's terms apply (not evaluated here)", "label": "declared-by-project", "memory_needed_gib": None, "disk_needed_gib": 0.0, "needs_download": False,
                    "key_variable_found": have, "limits": ["CLOUD: your prompts go to the provider and are billed per use"] + ([] if have else ["no key variable was found on this machine; you would supply one"])})
    return out


def apply_filters(options: list[dict], facts: dict, choice: str, sensitivity: str, *, licenses_allowed: list[str]) -> tuple[list[dict], list[dict]]:
    """(kept, removed with the reason). Every removal names its rule."""
    n = _norm(facts)
    budget, mem_name = _memory_budget(n)
    kept, removed = [], []
    for o in options:
        why = None
        if choice != "mix" and o["kind"] != choice:
            why = f"not the kind you chose ({choice})"
        elif o["cloud"] and sensitivity == "confidential":
            why = "sends data to a provider: not allowed for confidential work"
        elif o["kind"] == "local" and o.get("memory_needed_gib") is not None and o["memory_needed_gib"] > budget:
            why = f"needs {o['memory_needed_gib']} GiB and only {budget} GiB of {mem_name} may be used for a model"
        elif o["kind"] == "local" and o.get("disk_needed_gib", 0) > n["disk"]:
            why = f"needs {o['disk_needed_gib']} GiB of disk and only {n['disk']} GiB is free"
        elif o["kind"] == "local" and o.get("license") and o["origin"] == "catalog_runtime" and str(o["license"]).lower() not in licenses_allowed:
            why = f"its license ({o['license']}) is not on the allowed list"
        (removed if why else kept).append({**o, "removed_by": why} if why else o)
    return kept, removed


def order_options(options: list[dict]) -> list[dict]:
    """The written rule (ORDER_TEXT). Measured pass rate and time would come first within a cost class; none exist yet, so every option is provisional."""
    def key(o: dict):
        measured = o.get("measured") or {}
        return (COST_RANK[o["cost_class"]], -(measured.get("pass_rate") or 0.0), measured.get("seconds") or 0.0, WRITTEN_ORDER[o["origin"]], 0 if (o.get("set_up") or o.get("key_variable_found") or o["origin"] in ("installed_runtime", "catalog_runtime")) else 1, -float(o.get("params_b") or 0.0), o["title"])
    return sorted(options, key=key)


def build_options(facts: dict, *, choice: str, sensitivity: str = "internal", chosen_cli: str | None = None, chosen_provider: str | None = None, catalog: dict | None = None,
                  harnesses: dict | None = None, evidence: dict | None = None) -> dict:
    catalog = catalog if catalog is not None else load_catalog()
    harnesses = harnesses or load_harnesses()
    evidence = evidence if evidence is not None else load_evidence()
    all_ = candidates(facts, catalog, harnesses)
    if chosen_cli:
        all_ = [o for o in all_ if o["kind"] != "cli" or o["id"] == f"cli-{chosen_cli}"]
    if chosen_provider:
        all_ = [o for o in all_ if o["kind"] != "api" or o["id"] == f"api-{chosen_provider}"]
    kept, removed = apply_filters(all_, facts, choice, sensitivity, licenses_allowed=harnesses["licenses_allowed"])
    ordered = order_options(kept)
    if choice == "mix":  # one of each kind that applies, in the written order
        seen, picked = set(), []
        for o in ordered:
            if o["kind"] not in seen:
                seen.add(o["kind"])
                picked.append(o)
        ordered = picked
    shown = ordered[:MAX_SHOWN]
    for o in shown:
        o["status"] = PROVISIONAL if not o.get("measured") else "measured"
        o["evidence"] = evidence_for(evidence, o.get("runtime", ""))["note"] or "public evidence exists (see entries)"
    return {"choice": choice, "sensitivity": sensitivity, "options": shown, "considered": len(all_), "kept": len(kept), "removed": [{"id": o["id"], "reason": o["removed_by"]} for o in removed][:40], "rule": ORDER_TEXT}
