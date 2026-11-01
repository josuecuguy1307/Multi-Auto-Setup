"""Support for the fixture-per-rule tests of `model-catalog-rules` (tests/fixtures/catalog_rules/<rule id>.json).

A fixture says: with these inputs, the rule's effect is this. The harness runs it twice: with every rule (the expectation must hold) and with THIS rule removed from the data (it must NOT hold). A rule that
changes nothing a fixture can see would pass both runs and fail the test: that is how "each rule has a fixture that must fail without the rule" is enforced."""

from __future__ import annotations

import copy
from typing import Any

from envrec.catalog import rules as R
from envrec_coordinator import options as O

GIB = 2 ** 30

# a clean, verified, offered-by-default chat model: every case changes only what it is about
BASE_FILE: dict[str, Any] = {
    "file.name": "Model-4B-Instruct-Q4_K_M.gguf", "file.repo": "Org/Model-4B-Instruct-GGUF", "file.bytes": 2 * GIB, "file.revision": "a" * 40, "file.sha256": "b" * 64, "file.quantization": "Q4_K_M",
    "file.size_ratio_to_repo_largest": 0.5,
    "hub.author": "Org", "hub.gated": False, "hub.pipeline_tag": "text-generation", "hub.tags": ["gguf", "text-generation"], "hub.card_license": "apache-2.0", "hub.base_models": [],
    "hub.listing_size": 2 * GIB, "hub.listing_sha256": "b" * 64, "repo.has_companion_projector": False, "repo.largest_gguf_bytes": 4 * GIB, "policy.approved_publishers": ["Org"],
    "gguf.general.architecture": "qwen3", "gguf.general.type": "model", "gguf.general.license": "apache-2.0", "gguf.tensor_count": 400, "gguf.param_count": 4_000_000_000,
    "gguf.has_chat_template": True, "gguf.template.thinking": False,
    "gguf.arch.context_length": 32768, "gguf.arch.block_count": 36, "gguf.arch.attention.head_count": 32, "gguf.arch.attention.head_count_kv": 8, "gguf.arch.embedding_length": 2560,
    "gguf.arch.attention.key_length": 128, "gguf.arch.attention.value_length": 128,
}

BASE_ROW: dict[str, Any] = {
    "id": "Org/Model-4B-Instruct-GGUF/Model-4B-Instruct-Q4_K_M.gguf", "source": "org", "repo": "Org/Model-4B-Instruct-GGUF", "file": "Model-4B-Instruct-Q4_K_M.gguf", "url": "https://example.test/x",
    "revision": "a" * 40, "sha256": "b" * 64, "bytes": 2 * GIB, "license": "apache-2.0", "license_source": "https://example.test/lic", "format": "gguf", "quantization": "Q4_K_M", "params_b": 4.0,
    "min_ram_gib": 1.0, "kind": "chat", "architecture": "qwen3", "dims": {"layers": 36, "kv_layers": 36, "kv_heads": 8, "key_length": 128, "value_length": 128}, "labels": [], "requires_task": {},
    "offer_by_default": True, "not_default_reasons": [], "conflicts": [], "companions": [], "verified": {"label": "verified-by-code"},
}

BASE_MACHINE: dict[str, Any] = {"ram_total_gib": 16.0, "ram_available_gib": 10.0, "vram_gib": None, "model_path": "cpu", "disk_free_gib": 200.0}


def facts_for(case: dict[str, Any]) -> dict[str, Any]:
    f = copy.deepcopy(BASE_FILE)
    for k, v in case.get("facts", {}).items():
        if v is None:
            f.pop(k, None)
        else:
            f[k] = v
    return f


def row_for(over: dict[str, Any] | None = None) -> dict[str, Any]:
    r = copy.deepcopy(BASE_ROW)
    for k, v in (over or {}).items():
        if k == "bytes_gib":
            r["bytes"] = int(v * GIB)
        elif v is None:
            r.pop(k, None)
        else:
            r[k] = v
    return r


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "excluded_rules": [x["rule"] for x in row["excluded"]], "requires_task_keys": sorted(row["requires_task"]), "conflict_fields": [c["field"] for c in row["conflicts"]]}


def run(rules: R.Rules, spec_kind: str, case: dict[str, Any]) -> Any:
    """The observable result of one case, for the expectation to be compared with."""
    if case.get("params"):
        rules = rules.with_params(**case["params"])
    if spec_kind == "file":
        return _view(R.derive_row(facts_for(case), rules))
    if spec_kind == "estimate":
        return R.estimate_memory(row_for(case.get("row")), rules, context=case.get("context"), measured=case.get("measured"))
    if spec_kind == "shards":
        files = [row_for({**x, "file": x["file"]}) for x in case["files"]]
        out = R.group_shards(files, rules)
        return {"count": len(out), "bytes": sorted(o["bytes"] for o in out), "shard_files": sorted(s["file"] for o in out for s in o.get("shards", []))}
    if spec_kind == "machine":
        m = {**BASE_MACHINE, **case.get("machine", {})}
        budget, basis = R.memory_budget(m, rules)
        out = {"budget_gib": budget, "basis": basis}
        if "disk_row" in case:
            out["disk_need_gib"] = R.disk_need_gib(row_for(case["disk_row"]), int(case["runtime_gib"] * GIB), rules)
        return out
    if spec_kind == "option":
        return _option_view(rules, case)
    if spec_kind == "build":
        assets = case["assets"] if isinstance(case.get("assets"), list) else _lock_assets()
        sel = R.select_build(case["machine"], assets, rules, requested_backend=case.get("requested_backend"))
        return {"ok": sel["ok"], "cell": sel["cell"], **({"backend": sel["backend"], "memory": sel["memory"], "file": sel["asset"]["file"]} if sel["ok"] else {"reason": sel["reason"], "text": sel["text"]})}
    if spec_kind == "ollama":
        return {"problems": R.check_ollama_ref(case["ref"], rules)}
    if spec_kind == "evidence":
        return {"rejected": R.check_evidence(case["entry"], rules)}
    raise AssertionError(f"unknown fixture kind {spec_kind!r}")


def _lock_assets() -> list[dict[str, Any]]:
    import yaml
    from pathlib import Path
    return yaml.safe_load((Path(__file__).resolve().parent.parent / "catalog.lock.yaml").read_text())["runtime_assets"]


def _option_view(rules: R.Rules, case: dict[str, Any]) -> dict[str, Any]:
    rows = [row_for(r) for r in case["rows"]]
    for i, r in enumerate(rows):
        r.setdefault("id", f"row{i}")
    catalog = {"runtime_assets": [{"id": "rt", "source": "llama.cpp", "tag": "b1", "os": "windows", "arch": "amd64", "backend": b, "license": "MIT", "bytes": 10 * 2 ** 20, "runnable": True}
                                  for b in ("cpu", "vulkan")], "model_files": rows}
    machine = case.get("machine", {})
    facts = {"machine.os.name": "Windows 11", "machine.os.arch": "amd64", "machine.model_path": machine.get("model_path", "cpu"), "machine.vram_gib": machine.get("vram_gib"),
             "machine.ram_total_gib": machine.get("ram_total_gib", 16.0), "machine.ram_available_gib": machine.get("ram_available_gib", 10.0), "machine.disk_free_gib": 200.0,
             "machine.gpus": machine.get("gpus", []), "machine.runtimes": [], "machine.ai_clis": [], "machine.api_key_variable_names": []}
    res = O.build_options(facts, choice="local", catalog=catalog, evidence={"entries": []}, rules=rules, runtime_arches=case.get("runtime_arches", {}), task_needs=tuple(case["task_needs"]) if case.get("task_needs") else None)
    shown = {o["id"]: o for o in res["options"]}
    return {"option_ids": sorted(shown), "runtimes": sorted({o["runtime"] for o in res["options"]}), "labels": sorted({lb for o in res["options"] for lb in o["labels"]}),
            "removed": {x["id"]: x["reason"] for x in res["removed"]}, "fit_variants": {x["id"]: [v["id"] for v in x.get("fit_variants", [])] for x in res["removed"] if x.get("fit_variants")}}


def matches(expect: Any, actual: Any, path: str = "") -> list[str]:
    """Differences between an expectation and a result ([] when it holds). A dict is compared by its keys, a list by membership (each expected item is in the result), a float within 0.01, a string
    starting with `~` as a substring."""
    out: list[str] = []
    if isinstance(expect, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected an object, got {actual!r}"]
        for k, v in expect.items():
            if k.startswith("!"):  # "!key": the list under key must hold NONE of these
                got = actual.get(k[1:], [])
                out += [f"{path}.{k[1:]}: {item!r} must not be there" for item in v if item in got]
            elif k not in actual:
                out.append(f"{path}.{k}: missing")
            else:
                out += matches(v, actual[k], f"{path}.{k}")
    elif isinstance(expect, list):
        if not isinstance(actual, (list, tuple)):
            return [f"{path}: expected a list, got {actual!r}"]
        for item in expect:
            if not any(not matches(item, a, path) for a in actual):
                out.append(f"{path}: {item!r} not found in {list(actual)!r}")
    elif isinstance(expect, str) and expect.startswith("~"):
        if expect[1:].lower() not in str(actual).lower():
            out.append(f"{path}: {expect[1:]!r} not in {actual!r}")
    elif isinstance(expect, float):
        if not isinstance(actual, (int, float)) or abs(actual - expect) > 0.011:
            out.append(f"{path}: expected about {expect}, got {actual!r}")
    elif expect != actual:
        out.append(f"{path}: expected {expect!r}, got {actual!r}")
    return out
