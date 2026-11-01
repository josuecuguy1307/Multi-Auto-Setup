"""Setup options built from a machine's analysis (skill `setup-options`). PURE CODE: the hard filters and the written ordering live here, and nothing a model says can change them. The model's
only part is to explain, in plain words, why each option is there and what limits it.

Inputs are plain dicts of the facts the probes returned (`machine.*`); sources are the repository's `catalog.lock.yaml` (labeled verified-by-code), the skill's `harnesses.json` (declared by
the project) and `evidence.json` (public evidence; empty is fine and is said)."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from envrec.catalog import rules as R
from envrec_os import SKILLS_DIR

REF = SKILLS_DIR / "setup-options" / "references"
CATALOG_FILE = SKILLS_DIR.parent / "catalog.lock.yaml"
COST_RANK = {"free_local": 0, "subscription": 1, "metered_api": 2}
WRITTEN_ORDER = {"installed_runtime": 0, "catalog_runtime": 1, "installed_cli": 2, "api_key": 3}
ORDER_TEXT = ("cost class (free and local, then subscription, then metered API); then measured pass rate and measured time when they exist (none are measured yet, so they are skipped); then the written "
              "order (an installed runtime before a download, an installed command-line tool, an API key), one that is already set up or whose key variable was found before one that is not; then the larger model that fits; then the name. "
              "A model published in several quantizations is one option (the preferred quantization that fits), and up to twelve options are shown, with the count of those not shown")
PROVISIONAL = "provisional, not measured here"
ARCHES = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}


def load_catalog(path: Path | None = None) -> dict:
    p = path or CATALOG_FILE
    return yaml.safe_load(p.read_text()) if p.is_file() else {"runtime_assets": [], "model_files": []}


def load_harnesses() -> dict:
    return json.loads((REF / "harnesses.json").read_text())


def load_evidence() -> dict:
    return json.loads((REF / "evidence.json").read_text())


RECIPES_FILE = SKILLS_DIR / "setup-recipes" / "references" / "recipes.json"


def recipe_coverage() -> dict[str, set[str]]:
    """What the closed recipes can set up: the runtimes they download (`catalog`) and the runtimes already on a machine that they take over (`installed`, a recipe's `handles_installed`). Nothing else has a recipe."""
    recipes = json.loads(RECIPES_FILE.read_text())["recipes"] if RECIPES_FILE.is_file() else []
    return {"catalog": {r["runtime_source"] for r in recipes}, "installed": {i for r in recipes for i in r.get("handles_installed", [])}}


def load_rules() -> R.Rules:
    return R.Rules.load()


def catalog_summary(catalog: dict) -> dict:
    """What `catalog_lookup` shows the model: counts and the sources, not the rows."""
    rt = catalog.get("runtime_assets", [])
    rows = catalog.get("model_files", [])
    return {"label": "verified-by-code (licenses and checksums read by the catalog generator; sizes, kinds and parameter counts checked against each file's header)", "runtime_builds": len(rt),
            "runtimes": sorted({r["source"] for r in rt}), "model_files": len(rows), "licenses": sorted({m["license"] for m in rows}), "checked": (catalog.get("verified") or {}).get("checked"),
            "kinds": {k: sum(1 for m in rows if m.get("kind", "chat") == k) for k in sorted({m.get("kind", "chat") for m in rows})}}


def evidence_for(evidence: dict, topic: str) -> dict:
    rows = [e for e in evidence.get("entries", []) if topic.lower() in json.dumps(e).lower()]
    return {"entries": rows, "note": "" if rows else "no public data"}


def _norm(facts: dict) -> dict:
    os_ = str(facts.get("machine.os.family") or "") or load_rules().os_family(facts.get("machine.os.name"))  # the machine says its family itself; a name is read only for an analysis stored before that
    return {"os": os_, "arch": ARCHES.get(str(facts.get("machine.os.arch", "")).lower(), ""), "path": facts.get("machine.model_path"), "vram": facts.get("machine.vram_gib"),
            "ram": float(facts.get("machine.ram_total_gib") or 0), "ram_free": facts.get("machine.ram_available_gib"), "disk": float(facts.get("machine.disk_free_gib") or 0), "gpus": facts.get("machine.gpus") or [],
            "runtimes": facts.get("machine.runtimes") or [], "clis": facts.get("machine.ai_clis") or [], "keys": facts.get("machine.api_key_variable_names") or []}


def _vendor(n: dict) -> str | None:
    """The graphics maker that matters for the build: the one models run on (a separate card with its own memory, or the Apple GPU that shares the memory)."""
    if n["path"] == "unified":
        return next((g.get("vendor") for g in n["gpus"] if g.get("memory") == "unified"), None)
    ded = [g for g in n["gpus"] if g.get("memory") == "dedicated"]
    return max(ded, key=lambda g: g.get("vram_gib") or 0).get("vendor") if ded else None


def _machine(n: dict) -> dict:
    return {"os": n["os"], "arch": n["arch"], "path": n["path"], "vendor": _vendor(n), "model_path": n["path"], "vram_gib": n["vram"], "ram_total_gib": n["ram"], "ram_available_gib": n["ram_free"]}


def local_build(facts: dict, catalog: dict, rules: R.Rules | None = None) -> dict:
    """Which runtime build this machine gets, from the matrix of the rules (R3.6): {ok, cell, backend, memory, asset} or a typed refusal."""
    return R.select_build(_machine(_norm(facts)), catalog.get("runtime_assets", []), rules or load_rules())


def _memory_budget(n: dict, rules: R.Rules | None = None) -> tuple[float, str]:
    return R.memory_budget(_machine(n), rules or load_rules())


def _fit(o: dict, budget: float, mem_name: str, n: dict, path) -> dict:
    """The numbers an owner compares: which model, quantization, size, what it needs, what may be used and whether it fits. Notes are codes: the words are in the Harness App's strings."""
    base = {"budget_gib": budget, "budget_basis": mem_name, "available_gib": n["ram_free"], "total_gib": n["ram"]}
    if o["kind"] != "local":
        return {**base, "model": None, "quantization": None, "size_gib": None, "needed_gib": None, "fits": None, "note": "cloud_not_applicable"}
    if o["origin"] == "installed_runtime":
        rows = o.get("models") or []
        return {**base, "models": rows, "model": ", ".join(r["name"] for r in rows) or None, "fits": any(r["fits"] for r in rows) if rows else False, "note": "size_is_lower_bound"}
    return {**base, "model": o["model"].split("/")[-1], "quantization": o.get("quantization"), "size_gib": round(o["model_bytes"] / 2 ** 30, 2), "needed_gib": o["memory_needed_gib"],
            "fits": o["memory_needed_gib"] <= budget}


def candidates(facts: dict, catalog: dict, harnesses: dict, *, rules: R.Rules | None = None, runtime_arches: dict | None = None, task_needs: tuple[str, ...] | None = None) -> list[dict]:
    rules = rules or load_rules()
    arches = runtime_arches if runtime_arches is not None else R.load_runtime_arches()
    needs = tuple(task_needs or rules.data["tasks"]["default"])
    n = _norm(facts)
    out: list[dict] = []
    for rt in n["runtimes"]:  # a runtime already on the machine: no download
        budget_here, _basis = _memory_budget(n, rules)
        models = [{"name": m["name"], "size_gib": m.get("size_gib"), "quantization": m.get("quantization"), "parameter_size": m.get("parameter_size"),
                   "fits": (m["size_gib"] <= budget_here) if m.get("size_gib") is not None else None} for m in rt.get("models", [])]  # the file size is a lower bound of the memory a model needs
        out.append({"id": f"use-{rt['id']}", "runtime_id": rt["id"], "models": models, "kind": "local", "origin": "installed_runtime", "title": f"Use the {rt['name']} already on this machine", "cost_class": "free_local", "cloud": False,
                    "runtime": rt["name"], "models_present": [m["name"] for m in models], "license": "see the runtime's own license", "label": "verified-by-code (found on this machine by a probe)",
                    "memory_needed_gib": None, "disk_needed_gib": 0.0, "needs_download": False, "limits": ([] if models else ["it has no models yet"]) + (["it is reachable from the network: bind it to loopback"] if rt.get("exposed_beyond_loopback") else []),
                    "params_b": max([0.0] + [float(m["name"].split(":")[1].rstrip("b")) if ":" in m["name"] and m["name"].split(":")[1].rstrip("b").replace(".", "").isdigit() else 0.0 for m in models])})
    sel = R.select_build(_machine(n), catalog.get("runtime_assets", []), rules)
    asset = sel["asset"] if sel["ok"] else None
    if asset is not None:
        for m in catalog.get("model_files", []):
            est = R.estimate_memory(m, rules, measured=m.get("measured"))
            supports = R.runtime_supports(m.get("architecture"), asset["source"], asset.get("tag", ""), arches, rules)
            labels = list(m.get("labels", []))
            if supports is not False and not (m.get("measured") or {}).get("loaded") and rules.on("runtime_architecture"):
                labels.append(next(e["label"] for e in next(r for r in rules.rules if r["id"] == "R5.1-runtime-architecture")["effects"] if e["action"] == "label"))
            companions = m.get("companions", [])  # a vision model comes with its projector: counted in memory and disk, downloaded with it
            if m.get("kind") == "vision-language" and not companions:
                labels.append("vision model whose projector is not in the catalog: it is used as a text model")
            out.append({"id": f"catalog-{m['params_b']}b-{m['repo'].split('/')[-1][:30]}-{str(m.get('quantization', '')).lower()}", "kind": "local", "origin": "catalog_runtime", "title": f"{asset['source']} ({asset['backend']}) with {m['repo'].split('/')[-1]} {m['quantization']}",
                        "cost_class": "free_local", "cloud": False, "runtime_source": asset["source"], "runtime": f"{asset['source']} {asset['tag']} ({asset['backend']})", "model": m["repo"], "params_b": float(m["params_b"]), "license": m["license"],
                        "runtime_license": asset["license"], "license_name": m.get("license_name"), "license_class": m.get("license_class"), "license_label": m.get("license_label"), "license_basis": m.get("license_basis"),
                        "label": "verified-by-code (license and checksum read from the source)", "source": m["license_source"], "memory_needed_gib": est["total_gib"],
                        "memory_estimate": est, "disk_needed_gib": R.disk_need_gib({**m, "companions": companions}, asset["bytes"], rules, with_companions=True), "needs_download": True, "limits": [],
                        "build": {"cell": sel["cell"], "backend": sel["backend"], "memory": sel["memory"], "asset_id": asset.get("id"), "file": asset.get("file"), "sha256": asset.get("sha256"), "bytes": asset["bytes"]},
                        "model_id": m.get("id"), "model_kind": m.get("kind", R.KIND_CHAT), "model_file": m.get("file"), "model_revision": m.get("revision"), "model_sha256": m.get("sha256"), "model_bytes": m.get("bytes"),
                        "quantization": m.get("quantization"), "params_active_b": m.get("params_active_b"), "architecture": m.get("architecture"), "labels": labels, "requires_task": dict(m.get("requires_task", {})),
                        "unverified": not m.get("verified"), "not_default": list(m.get("not_default_reasons", [])), "runtime_supports_architecture": supports, "companions": companions, "conflicts": list(m.get("conflicts", []))})
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


def apply_filters(options: list[dict], facts: dict, choice: str, sensitivity: str, *, rules: R.Rules | None = None, task_needs: tuple[str, ...] | None = None,
                  catalog: dict | None = None) -> tuple[list[dict], list[dict]]:
    """(kept, removed with the reason). Every removal names its rule or its numbers."""
    rules = rules or load_rules()
    needs = tuple(task_needs or rules.data["tasks"]["default"])
    n = _norm(facts)
    budget, mem_name = _memory_budget(n, rules)
    rows = [r for r in (catalog or {}).get("model_files", []) if r.get("verified")]
    unverified_reason = next((e["reason"] for r in rules.rules if r["id"] == "R7.2-header-required" for e in r["effects"] if e["action"] == "not_default"), "unverified")
    kept, removed = [], []
    coverage = recipe_coverage()
    for o in options:
        why, variants = None, []
        if choice != "mix" and o["kind"] != choice:
            why = f"not the kind you chose ({choice})"
        elif o["cloud"] and sensitivity == "confidential":
            why = "sends data to a provider: not allowed for confidential work"
        elif o["origin"] == "installed_runtime" and o.get("runtime_id") not in coverage["installed"]:
            why = f"no_recipe_yet: using the {o['runtime']} already on this machine cannot be set up by this version (there is no recipe for it yet)"  # offered only what can be set up: a typed reason instead
        elif o["origin"] == "catalog_runtime" and o.get("runtime_source") not in coverage["catalog"]:
            why = f"no_recipe_yet: there is no setup recipe for {o.get('runtime_source')} yet"
        elif o.get("unverified") and rules.has("R7.2-header-required"):
            why = unverified_reason
        elif any(t not in needs for t in o.get("requires_task", {})):
            t = next(t for t in o["requires_task"] if t not in needs)
            why = f"{o['requires_task'][t]} (this task needs: {', '.join(needs)})"
        elif o.get("not_default"):
            why = "; ".join(o["not_default"])
        elif o.get("runtime_supports_architecture") is False:
            why = f"the pinned runtime ({o['runtime']}) cannot load the architecture {o.get('architecture')!r}"
        elif o["kind"] == "local" and o.get("memory_needed_gib") is not None and o["memory_needed_gib"] > budget:
            why = f"needs {o['memory_needed_gib']} GiB and only {budget} GiB of {mem_name} may be used for a model"  # mem_name carries the measured value
            row = next((r for r in rows if r["id"] == o.get("model_id")), None)
            if row:
                variants = [v for v in R.fitting_variants(row, [r for r in rows if all(t in needs for t in r.get("requires_task", {}))], budget, rules)]
        elif o["kind"] == "local" and o.get("disk_needed_gib", 0) > n["disk"]:
            why = f"needs {o['disk_needed_gib']} GiB of disk and only {n['disk']} GiB is free"
        elif o["kind"] == "local" and o.get("license") and o["origin"] == "catalog_runtime" and not rules.license_offered(o["license"]):
            why = f"its license ({o['license']}) is not a known license whose class is offered"
        (removed if why else kept).append({**o, "removed_by": why, **({"fit_variants": variants} if variants else {})} if why else o)
    return kept, removed


def order_options(options: list[dict]) -> list[dict]:
    """The written rule (ORDER_TEXT). Measured pass rate and time would come first within a cost class; none exist yet, so every option is provisional."""
    def key(o: dict):
        measured = o.get("measured") or {}
        return (COST_RANK[o["cost_class"]], -(measured.get("pass_rate") or 0.0), measured.get("seconds") or 0.0, WRITTEN_ORDER[o["origin"]], 0 if (o.get("set_up") or o.get("key_variable_found") or o["origin"] in ("installed_runtime", "catalog_runtime")) else 1, -float(o.get("params_b") or 0.0), o["title"])
    return sorted(options, key=key)


def load_display() -> dict:
    return json.loads((REF / "display.json").read_text())


def one_per_model(options: list[dict], rules: R.Rules) -> list[dict]:
    """A model published in several quantizations is ONE option (three files of the same model would fill the list and hide every other model). The quantization shown is the first of the rules'
    preference that fits; the others that also fit are listed on it (`other_quantizations`, with their ids) so the owner can still ask for one. Order is kept."""
    pref = [q.upper() for q in load_display()["option_quant_preference"]]
    rank = lambda o: pref.index(str(o.get("quantization", "")).upper()) if str(o.get("quantization", "")).upper() in pref else len(pref)  # noqa: E731
    groups: dict[tuple, list[dict]] = {}
    for o in options:
        groups.setdefault((o["kind"], o.get("model") or o["id"], o.get("runtime")), []).append(o)
    out, done = [], set()
    for o in options:
        key = (o["kind"], o.get("model") or o["id"], o.get("runtime"))
        if key in done:
            continue
        done.add(key)
        same = groups[key]
        best = min(same, key=lambda x: (rank(x), same.index(x)))
        if len(same) > 1:
            best = {**best, "other_quantizations": [{"id": x["id"], "quantization": x.get("quantization"), "memory_needed_gib": x.get("memory_needed_gib")} for x in same if x is not best]}
        out.append(best)
    return out


def relabel_measured(options: list[dict], lookup) -> list[dict]:
    """Measured wins (spec 6): where `lookup(option)` returns an admission view (envrec_coordinator.admission.facts), a fresh measurement fills the `measured` slot that `order_options` reads
    and the option says so; a stale one is shown as stale and does not rank. `lookup` returns None for an option that has no combination to look up."""
    from envrec_coordinator.admission.facts import measured_of
    for o in options:
        view = lookup(o)
        if view is None or view.state == "none":
            continue
        o["admission_label"], o["admission_state"] = view.label, view.state
        m = measured_of(view)
        if m:
            o["measured"] = m
    return options


def build_options(facts: dict, *, choice: str, sensitivity: str = "internal", chosen_cli: str | None = None, chosen_provider: str | None = None, catalog: dict | None = None,
                  harnesses: dict | None = None, evidence: dict | None = None, rules: R.Rules | None = None, runtime_arches: dict | None = None, task_needs: tuple[str, ...] | None = None,
                  admission_lookup=None) -> dict:
    catalog = catalog if catalog is not None else load_catalog()
    harnesses = harnesses or load_harnesses()
    evidence = evidence if evidence is not None else load_evidence()
    rules = rules or load_rules()
    needs = tuple(task_needs or rules.data["tasks"]["default"])
    all_ = candidates(facts, catalog, harnesses, rules=rules, runtime_arches=runtime_arches, task_needs=needs)
    if chosen_cli:
        all_ = [o for o in all_ if o["kind"] != "cli" or o["id"] == f"cli-{chosen_cli}"]
    if chosen_provider:
        all_ = [o for o in all_ if o["kind"] != "api" or o["id"] == f"api-{chosen_provider}"]
    kept, removed = apply_filters(all_, facts, choice, sensitivity, rules=rules, task_needs=needs, catalog=catalog)
    sel = local_build(facts, catalog, rules)
    refused = [] if sel["ok"] or choice not in ("local", "mix") else [{"id": "local-runtime", "reason": f"{sel['reason']}: {sel['text']}"}]  # no build for this machine: said, never an empty list nobody explains
    if admission_lookup is not None:
        kept = relabel_measured(kept, admission_lookup)  # before the written order, so a measured pass rate ranks inside its cost class
        failed = [o for o in kept if o.get("admission_state") == "not_admitted"]  # measured below the mark here: not approved (spec 8.1), said in `removed`
        kept = [o for o in kept if o not in failed]
        removed = removed + [{**o, "removed_by": f"not admitted: {o['admission_label']}"} for o in failed]
    ordered = order_options(kept)
    if choice == "mix":  # one of each kind that applies, in the written order
        seen, picked = set(), []
        for o in ordered:
            if o["kind"] not in seen:
                seen.add(o["kind"])
                picked.append(o)
        ordered = picked
    ordered = one_per_model(ordered, rules)
    cap = int(load_display()["options_shown_max"])
    shown = ordered[:cap]
    nn = _norm(facts)
    budget, mem_name = _memory_budget(nn, rules)
    path = facts.get("machine.model_path")
    for o in shown:
        o["fit"] = _fit(o, budget, mem_name, nn, path)
        if o["kind"] == "local" and o.get("memory_needed_gib") is not None:
            e = o.get("memory_estimate", {})
            o["memory_basis"] = (f"needs {o['memory_needed_gib']} GiB ({e.get('label', 'estimate')}: weights {e.get('weights_gib')} + KV cache {e.get('kv_gib')} for a context of {e.get('context')} + runtime allowance {e.get('overhead_gib')}); "
                                 f"budget {budget} GiB from {mem_name}; the analysis says models run on: {path or 'not measured'}") if e else f"needs {o['memory_needed_gib']} GiB; budget {budget} GiB from {mem_name}; the analysis says models run on: {path or 'not measured'}"
        o["status"] = o.get("admission_label", "measured") if o.get("measured") else (f"{PROVISIONAL}; {o['admission_label']}" if o.get("admission_state") == "stale" else PROVISIONAL)
        o["evidence"] = evidence_for(evidence, o.get("runtime", ""))["note"] or "public evidence exists (see entries)"
    return {"choice": choice, "sensitivity": sensitivity, "task_needs": list(needs), "options": shown, "not_shown": max(0, len(ordered) - len(shown)), "considered": len(all_), "kept": len(kept),
            "removed": refused + [{"id": o["id"], "reason": o["removed_by"], **({"fit_variants": o["fit_variants"]} if o.get("fit_variants") else {})} for o in removed][:60], "rule": ORDER_TEXT}
