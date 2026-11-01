"""The rules of the skill `model-catalog-rules`, as data (`skills/model-catalog-rules/references/rules.json`), and the small guard code that applies them.

Code here is GUARD: it evaluates a rule's predicate over verified facts, executes the arithmetic the rules name, and records which rule fired. It chooses nothing a model could choose: what is excluded, what is gated
by a task, what is labeled and with which words all live in the data. Remove a rule from the data and its behavior is gone (every rule has a fixture that fails without it: tests/test_catalog_rules.py).

Facts are flat dicts keyed `file.*` (the catalog row), `hub.*` (the hub's own listing and repo info at a pinned revision), `repo.*`, `gguf.*` (the file's header), `policy.*`, `machine.*`. Their label is
"verified-by-code" when they were read from a header, a listing or a runtime's source; names and card text are "declared-by-project" and never override a verified value."""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from envrec_os import SKILLS_DIR

RULES_FILE = SKILLS_DIR / "model-catalog-rules" / "references" / "rules.json"
RUNTIME_ARCHES_FILE = SKILLS_DIR / "model-catalog-rules" / "references" / "runtime_arches.json"
GIB = 2 ** 30
CONFLICT_TOLERANCE = 0.10  # a declared number (a name, a card) this far from the verified one is recorded as a conflict
KIND_CHAT = "chat"
VERIFIED = "verified-by-code"
DECLARED = "declared-by-project"


class RulesError(ValueError):
    """The rules file is malformed (an unknown operator or action). A rule that cannot be read is never silently skipped."""


KNOWN_OPS = {"eq", "ne", "in", "not_in", "contains", "matches", "not_matches", "exists", "missing", "gt", "ge", "lt", "le", "differs", "not_in_fact"}
KNOWN_ACTIONS = {"set_kind", "companion", "exclude", "require_task", "label", "not_default", "use_listing_size", "sum_shards", "keep_active_params", "kv_cache", "runtime_overhead", "budget_from_available",
                 "budget_follows_path", "budget_unified", "count_projector", "build_matrix", "disk_with_temp", "offer_fitting_variants", "measured_replaces_estimate", "split_context_claims", "runtime_architecture", "pin_ollama_digest", "reject_evidence",
                 "verified_wins"}


class Rules:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.rules: list[dict[str, Any]] = data["rules"]
        self.params: dict[str, Any] = data.get("parameters", {})
        for r in self.rules:
            for e in r.get("effects", []):
                if e["action"] not in KNOWN_ACTIONS:
                    raise RulesError(f"rule {r['id']}: unknown action {e['action']!r}")
            if "when" in r:
                _check_pred(r["when"], r["id"])

    @classmethod
    def load(cls, path: Path | None = None) -> Rules:
        return cls(json.loads((path or RULES_FILE).read_text(encoding="utf-8")))

    def without(self, *rule_ids: str) -> Rules:
        """The same rules with these removed: what a fixture runs to prove the rule is what makes the difference."""
        d = copy.deepcopy(self.data)
        d["rules"] = [r for r in d["rules"] if r["id"] not in rule_ids]
        return Rules(d)

    def with_params(self, **p: Any) -> Rules:
        d = copy.deepcopy(self.data)
        d["parameters"].update(p)
        return Rules(d)

    def ids(self) -> list[str]:
        return [r["id"] for r in self.rules]

    def has(self, rule_id: str) -> bool:
        return rule_id in self.ids()

    def on(self, action: str) -> bool:
        return any(e["action"] == action for r in self.rules for e in r.get("effects", []))

    def effect(self, action: str) -> dict[str, Any] | None:
        return next((e for r in self.rules for e in r.get("effects", []) if e["action"] == action), None)

    def param(self, name: str) -> Any:
        return self.params[name]

    def license(self, license_id: str | None) -> dict[str, Any] | None:
        """The table entry for a license id as the hub writes it, with its class; None when the id is not on the table."""
        if not license_id:
            return None
        e = self.data.get("licenses", {}).get(str(license_id).casefold())
        if not e:
            return None
        cls = self.data.get("license_classes", {}).get(e["class"], {})
        return {"id": str(license_id).casefold(), "name": e["name"], "class": e["class"], "label": cls.get("label", e["class"]), "offered": e["class"] in self.params.get("license_classes_offered", []) and cls.get("offered", False), **({"publisher": e["publisher"]} if "publisher" in e else {})}

    def os_family(self, name: str | None) -> str:
        """macos, windows or linux from an operating-system name (a distribution name counts), or an empty string: the table says, nothing is guessed."""
        t = self.data.get("os_families", {})
        n = str(name or "")
        return next((fam for fam in ("macos", "windows", "linux") if any(n.casefold().startswith(x.casefold()) for x in t.get(fam, []))), "")

    def license_offered(self, license_id: str | None) -> bool:
        lic = self.license(license_id)
        return bool(lic and lic["offered"])

    def policy_facts(self) -> dict[str, Any]:
        return {f"policy.{k}": v for k, v in self.params.items() if not isinstance(v, (dict, list))}


# ---- predicates -----------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def _check_pred(p: dict[str, Any], rid: str) -> None:
    for k in ("all", "any"):
        if k in p:
            for q in p[k]:
                _check_pred(q, rid)
            return
    if "not" in p:
        _check_pred(p["not"], rid)
        return
    if p.get("op") not in KNOWN_OPS:
        raise RulesError(f"rule {rid}: unknown operator {p.get('op')!r}")


def _present(facts: dict[str, Any], key: str) -> bool:
    return key in facts and facts[key] is not None


def _norm(v: Any) -> Any:
    return v.casefold() if isinstance(v, str) else v


def holds(p: dict[str, Any], facts: dict[str, Any]) -> bool:
    """A predicate over facts. A missing fact never satisfies a test that needs it (unknown is not a match); `missing`, `not_matches`, `not_in` and `differs` are the ways a rule asks about absence."""
    if "all" in p:
        return all(holds(q, facts) for q in p["all"])
    if "any" in p:
        return any(holds(q, facts) for q in p["any"])
    if "not" in p:
        return not holds(p["not"], facts)
    op, key = p["op"], p["fact"]
    have = _present(facts, key)
    v = facts.get(key)
    want = facts.get(p["other_fact"]) if "other_fact" in p else p.get("value")
    if op == "exists":
        return have
    if op == "missing":
        return not have
    if op == "not_matches":
        return not (have and re.search(want, str(v)))
    if op == "not_in":
        return not (have and _norm(v) in [_norm(x) for x in want])
    if op == "differs":
        return have and want is not None and _norm(v) != _norm(want)
    if not have:
        return False
    if op == "eq":
        return _norm(v) == _norm(want)
    if op == "ne":
        return _norm(v) != _norm(want)
    if op == "in":
        return _norm(v) in [_norm(x) for x in want]
    if op == "not_in_fact":
        return isinstance(want, list) and _norm(v) not in [_norm(x) for x in want]
    if op == "contains":
        if isinstance(v, (list, tuple)):
            return _norm(want) in [_norm(x) for x in v]
        return str(want).casefold() in str(v).casefold()
    if op == "matches":
        return re.search(want, str(v)) is not None
    try:
        a, b = float(v), float(want)
    except (TypeError, ValueError):
        return False
    return {"gt": a > b, "ge": a >= b, "lt": a < b, "le": a <= b}[op]


# ---- the file rules -------------------------------------------------------------------------------------------------------------------------------------------------------------------------

@dataclass
class Decision:
    kind: str = KIND_CHAT
    companion: bool = False
    exclude: list[dict[str, str]] = field(default_factory=list)  # {"rule", "reason"}
    requires_task: dict[str, dict[str, str]] = field(default_factory=dict)  # task -> {"rule", "reason"}
    not_default: list[dict[str, str]] = field(default_factory=list)
    labels: list[dict[str, str]] = field(default_factory=list)
    fired: list[str] = field(default_factory=list)


def classify(facts: dict[str, Any], rules: Rules) -> Decision:
    """Walk the rules in file order over the facts. The first `set_kind` wins; a rule with `skip_if_kind` does not apply to a file already given one of those kinds."""
    facts = {**rules.policy_facts(), **facts}
    d = Decision()
    kind_set = False
    for r in rules.rules:
        if r.get("scope") != "file" or "when" not in r:
            continue
        if d.kind in r.get("skip_if_kind", []):
            continue
        if not holds(r["when"], facts):
            continue
        d.fired.append(r["id"])
        for e in r["effects"]:
            a = e["action"]
            if a == "set_kind" and not kind_set:
                d.kind, kind_set = e["kind"], True
            elif a == "companion":
                d.companion = True
            elif a == "exclude":
                d.exclude.append({"rule": r["id"], "reason": e["reason"]})
            elif a == "require_task":
                d.requires_task.setdefault(e["task"], {"rule": r["id"], "reason": e["reason"]})
            elif a == "not_default":
                d.not_default.append({"rule": r["id"], "reason": e["reason"]})
            elif a == "label":
                d.labels.append({"rule": r["id"], "label": e["label"]})
    return d


def _dims(facts: dict[str, Any], rules: Rules) -> dict[str, Any]:
    """What the KV estimate needs, read from the header. Nothing is invented: a missing number stays missing and the estimate says so."""
    layers = facts.get("gguf.arch.block_count")
    heads = facts.get("gguf.arch.attention.head_count")
    kv_heads = facts.get("gguf.arch.attention.head_count_kv", heads)
    emb = facts.get("gguf.arch.embedding_length")
    key_len = facts.get("gguf.arch.attention.key_length") or (emb // heads if emb and heads else None)
    val_len = facts.get("gguf.arch.attention.value_length") or key_len
    notes = [m["note"] for m in rules.data.get("kv_estimate_markers", []) if holds(m, facts)]
    out = {"layers": layers, "kv_layers": facts.get("gguf.arch.attention.head_count_kv_layers") or layers, "kv_heads": kv_heads, "key_length": key_len, "value_length": val_len}
    if notes:
        out["estimate_notes"] = notes
    return {k: v for k, v in out.items() if v is not None}


def derive_row(facts: dict[str, Any], rules: Rules) -> dict[str, Any]:
    """The verified part of a catalog row, from the facts. Declared values (`file.declared_*`) are kept and compared; with R7.1 the verified one wins and the disagreement is recorded."""
    facts = {**rules.policy_facts(), **facts}
    header_lic, card_lic = facts.get("gguf.general.license"), facts.get("hub.card_license")
    effective = header_lic or card_lic  # the header is read from the file; the card is the project's own words
    lic = rules.license(effective)
    facts["license.effective"] = effective
    if lic:
        facts.update({"license.class": lic["class"], "license.offered": lic["offered"]})
    dec = classify(facts, rules)
    conflicts: list[dict[str, Any]] = []
    row: dict[str, Any] = {"kind": dec.kind, "companion": dec.companion, "excluded": dec.exclude,
                           "requires_task": {t: v["reason"] for t, v in dec.requires_task.items()}, "rules_fired": dec.fired}

    def reconcile(name: str, declared: Any, verified: Any, relative: bool) -> Any:
        if verified is None:
            return declared
        if declared is None:
            return verified
        apart = abs(declared - verified) / max(abs(verified), 1e-9) > CONFLICT_TOLERANCE if relative else declared != verified
        if apart:
            conflicts.append({"field": name, "declared": declared, "verified": verified, "winner": "verified" if rules.on("verified_wins") else "declared", "label": f"{VERIFIED} vs {DECLARED}"})
        return verified if rules.on("verified_wins") else declared

    pc = facts.get("gguf.param_count")
    row["params_b"] = reconcile("params_b", facts.get("file.declared_params_b"), round(pc / 1e9, 2) if pc else None, True)
    if rules.on("keep_active_params") and facts.get("gguf.active_param_count"):
        row["params_active_b"] = round(facts["gguf.active_param_count"] / 1e9, 2)
    listing = facts.get("hub.listing_size")
    row["bytes"] = reconcile("bytes", facts.get("file.bytes"), listing, False) if rules.on("use_listing_size") else facts.get("file.bytes")
    row["architecture"] = facts.get("gguf.general.architecture")
    for src, dst in (("gguf.general.size_label", "size_label"), ("gguf.general.name", "name")):
        if facts.get(src):
            row[dst] = facts[src]
    row["dims"] = _dims(facts, rules)
    ctx, orig = facts.get("gguf.arch.context_length"), facts.get("gguf.arch.rope.scaling.original_context_length")
    scal = facts.get("gguf.arch.rope.scaling.type")
    if rules.on("split_context_claims"):
        row["context_trained"] = orig or ctx
        if scal and scal != "none" and orig and ctx and ctx != orig:
            row["context_extended"] = {"length": ctx, "scaling": scal, "factor": facts.get("gguf.arch.rope.scaling.factor"), "label": f"{VERIFIED}: the trained length is {orig}; the extended length is {ctx}"}
    elif ctx:
        row["context_trained"] = ctx
    row["publisher"] = facts.get("hub.author")
    bases = [b for b in facts.get("hub.base_models") or [] if b.startswith("base_model:")]
    row["base_model"] = next((b.split(":")[-1] for b in bases if b.startswith("base_model:quantized:")), None) or next((b.split(":")[-1] for b in bases), None)
    row["gated"] = facts.get("hub.gated", False)
    row["license_header"], row["license_card"] = header_lic, card_lic
    if effective:
        row["license"] = str(effective).casefold()
        row["license_basis"] = (f"{VERIFIED}: the file header and the card agree" if header_lic and card_lic and str(header_lic).casefold() == str(card_lic).casefold()
                                else f"{VERIFIED}: the file header (the card states none)" if header_lic and not card_lic
                                else f"{DECLARED}: the card only; the file header states none (not verified)" if card_lic and not header_lic
                                else f"conflict: the header says {header_lic!r}, the card says {card_lic!r}")
    if lic:
        row.update({"license_name": lic["name"], "license_class": lic["class"], "license_label": lic["label"]})
    labels = [x["label"] for x in dec.labels]
    row["labels"] = list(dict.fromkeys(labels))
    row["not_default_reasons"] = [x["reason"] for x in dec.not_default]
    row["offer_by_default"] = not (dec.exclude or dec.not_default)
    row["conflicts"] = conflicts
    row["license_verified"] = bool(row["license_header"] and row["license_card"] and str(row["license_header"]).casefold() == str(row["license_card"]).casefold())
    return row


# ---- sizes: shards, memory, disk ------------------------------------------------------------------------------------------------------------------------------------------------------------------

def group_shards(files: list[dict[str, Any]], rules: Rules) -> list[dict[str, Any]]:
    """Files named `-00001-of-00003.gguf` are one model. With R2.2 a group becomes one entry whose size is the SUM and whose `shards` lists every shard with its SHA-256 (the plan downloads and checks each);
    an incomplete group (a shard missing) is dropped. Without the rule every shard stays a separate (and wrong) entry."""
    eff = rules.effect("sum_shards")
    if not eff:
        return files
    pat = re.compile(eff["pattern"])
    singles, groups = [], {}
    for f in files:
        m = pat.search(f["file"])
        if not m:
            singles.append(f)
            continue
        groups.setdefault((f["repo"], f["file"][:m.start()], m.group(2)), []).append((int(m.group(1)), f))
    for (_repo, stem, total), parts in groups.items():
        parts.sort(key=lambda x: x[0])
        if [i for i, _ in parts] != list(range(1, int(total) + 1)):
            continue
        first = parts[0][1]
        singles.append({**first, "file": first["file"], "bytes": sum(p["bytes"] for _, p in parts), "sha256": first["sha256"],
                        "shards": [{"file": p["file"], "bytes": p["bytes"], "sha256": p["sha256"], "url": p.get("url")} for _, p in parts]})
    return singles


def kv_bytes(row: dict[str, Any], context: int, rules: Rules) -> tuple[int | None, list[str]]:
    d = row.get("dims") or {}
    need = ("kv_layers", "kv_heads", "key_length", "value_length")
    if not all(d.get(k) for k in need):
        return None, ["KV cache not estimated: the header lacks the layer, head or key/value length numbers"]
    b = int(d["kv_layers"]) * int(d["kv_heads"]) * (int(d["key_length"]) + int(d["value_length"])) * int(context) * rules.param("kv_bytes_per_element")
    notes = [f"KV cache for a context of {context} tokens ({rules.param('kv_bytes_per_element')} bytes per element)"]
    if d.get("estimate_notes"):
        notes.append("estimate, architecture-specific: " + ", ".join(d["estimate_notes"]))
    return b, notes


def estimate_memory(row: dict[str, Any], rules: Rules, *, context: int | None = None, measured: dict[str, Any] | None = None) -> dict[str, Any]:
    """Memory a model needs: the weights from the REAL file size, the KV cache for an explicit context, a runtime allowance. 'estimate' until a measured resident size exists (R3.5)."""
    ctx = int(context or rules.param("default_context"))
    notes: list[str] = []
    if rules.on("use_listing_size"):
        weights = float(row["bytes"])
        notes.append("weights: the real file size")
        if rules.on("count_projector") and row.get("companions"):
            weights += sum(float(c["bytes"]) for c in row["companions"])
            notes.append("plus the vision projector file(s), which the model needs to see images")
    else:
        bits = rules.param("quant_nominal_bits").get(str(row.get("quantization", "")).upper(), 4.0)
        weights = float(row["params_b"]) * 1e9 * bits / 8
        notes.append("weights: parameters x nominal bits (not the real file size)")
    kv = 0.0
    if rules.on("kv_cache"):
        b, n = kv_bytes(row, ctx, rules)
        kv = float(b or 0)
        notes += n
    overhead = 0.0
    if rules.on("runtime_overhead"):
        overhead = rules.param("overhead_fixed_gib") * GIB + rules.param("overhead_weight_share") * weights
        notes.append(f"runtime allowance: {rules.param('overhead_fixed_gib')} GiB plus {int(rules.param('overhead_weight_share') * 100)}% of the weights (estimate)")
    out = {"weights_gib": round(weights / GIB, 3), "kv_gib": round(kv / GIB, 3), "overhead_gib": round(overhead / GIB, 3), "context": ctx,
           "total_gib": round((weights + kv + overhead) / GIB, 2), "label": "estimate", "notes": notes}
    if measured and measured.get("resident_gib") and rules.on("measured_replaces_estimate"):
        out.update({"total_gib": round(float(measured["resident_gib"]), 2), "label": "measured here", "estimate_was_gib": out["total_gib"]})
        if measured.get("tokens_per_s"):
            out["tokens_per_s"] = measured["tokens_per_s"]
        out["notes"] = notes + ["measured here: resident memory after the load, from the minimal call"]
    return out


def memory_budget(machine: dict[str, Any], rules: Rules) -> tuple[float, str]:
    """What a model may use, in GiB, and where it was measured. `machine` keys: ram_total_gib, ram_available_gib, vram_gib, model_path (cpu, gpu or unified)."""
    path, vram, free, total = machine.get("model_path"), machine.get("vram_gib"), machine.get("ram_available_gib"), float(machine.get("ram_total_gib") or 0)
    if path == "unified" and rules.on("budget_unified") and free is not None:
        m, share = rules.param("available_margin"), rules.param("unified_gpu_max_share")
        by_free, by_wired = round(float(free) * (1 - m), 2), round(total * share, 2)
        basis = f"unified memory ({free} GiB free when measured, {int(m * 100)}% margin" + (f"; the GPU may use at most {int(share * 100)}% of the {total:g} GiB)" if by_wired < by_free else ")")
        return min(by_free, by_wired), basis
    gpu_counts = (path == "gpu") if rules.on("budget_follows_path") else bool(vram)
    if gpu_counts and vram:
        return round(vram * rules.param("vram_share"), 2), f"VRAM ({vram} GiB total measured; free VRAM was not measured)"
    if free is not None and rules.on("budget_from_available"):
        m = rules.param("available_margin")
        return round(float(free) * (1 - m), 2), f"available RAM ({free} GiB free when measured, {int(m * 100)}% margin)"
    return round(total * rules.param("total_ram_share"), 2), f"RAM ({total} GiB total measured; the available figure was missing)"


# ---- the build matrix -------------------------------------------------------------------------------------------------------------------------------------------------------------------------

REFUSALS = {"unsupported_cell": "no build for this kind of machine", "no_build_in_catalog": "the catalog has no such build", "no_cell_for_facts": "the matrix has no row for these facts",
            "backend_not_supported": "that backend is not supported", "backend_mismatch": "the machine does not use that backend", "no_build_matrix": "the build matrix rule is not loaded"}


def _cell_matches(when: dict[str, Any], m: dict[str, Any]) -> bool:
    for k, want in when.items():
        have = m.get(k)
        if have is None or (have not in want if isinstance(want, list) else have != want):
            return False
    return True


def select_build(machine: dict[str, Any], assets: list[dict[str, Any]], rules: Rules, *, requested_backend: str | None = None) -> dict[str, Any]:
    """Which runtime build a machine gets. `machine` keys: os (macos, windows, linux), arch (amd64, arm64), path (cpu, gpu, unified), vendor (the graphics maker when models run on the GPU). The first cell of
    the matrix that matches the facts decides; the answer is {ok: True, cell, backend, memory, asset} or {ok: False, reason (typed), text, cell}. Nothing falls back to another build: a cell with no
    build says so."""
    def no(reason: str, text: str, cell: str | None = None) -> dict[str, Any]:
        return {"ok": False, "reason": reason, "text": text, "cell": cell}
    if not rules.on("build_matrix"):
        return no("no_build_matrix", REFUSALS["no_build_matrix"])
    mx = rules.data["build_matrix"]
    cell = next((c for c in mx["cells"] if _cell_matches(c["when"], machine)), None)
    if cell is None:
        return no("no_cell_for_facts", f"{REFUSALS['no_cell_for_facts']}: {machine.get('os') or '?'} / {machine.get('arch') or '?'} / models on {machine.get('path') or '?'}")
    if "refuse" in cell:
        return no(cell["refuse"], cell.get("text") or REFUSALS.get(cell["refuse"], ""), cell["id"])
    backend = cell["backend"]
    b = mx["backends"].get(backend, {})
    if not b.get("supported"):
        return no(b.get("refuse", "backend_not_supported"), b.get("text") or REFUSALS["backend_not_supported"], cell["id"])
    if requested_backend and requested_backend != backend:
        rb = mx["backends"].get(requested_backend, {})
        return no("backend_not_supported" if not rb.get("supported") else "backend_mismatch", rb.get("text") or f"this machine uses the {backend} build, not {requested_backend}", cell["id"])
    src = rules.param("runtime_source")
    build = cell.get("build", backend)  # the catalog build the cell uses (it can differ from how it runs: Apple silicon's one build, run with no layers on the GPU)
    asset = next((a for a in assets if a["source"] == src and a["os"] == machine["os"] and a["arch"] == machine["arch"] and a["backend"] == build and a.get("runnable")), None)
    if asset is None:
        return no("no_build_in_catalog", f"{REFUSALS['no_build_in_catalog']}: {src} {build} for {machine['os']} {machine['arch']}", cell["id"])
    return {"ok": True, "cell": cell["id"], "backend": backend, "memory": cell.get("memory") or b.get("memory"), "asset": asset}


def disk_need_gib(row: dict[str, Any], runtime_bytes: int, rules: Rules, *, with_companions: bool = False) -> float:
    files = [int(runtime_bytes), int(row["bytes"])] + ([int(c["bytes"]) for c in row.get("companions", [])] if (with_companions or rules.on("count_projector")) else [])
    need = sum(files) + (rules.param("disk_temp_factor") * max(files) if rules.on("disk_with_temp") else 0)
    return round(need / GIB, 2)


def fitting_variants(row: dict[str, Any], rows: list[dict[str, Any]], budget_gib: float, rules: Rules, *, context: int | None = None, limit: int = 3) -> list[dict[str, Any]]:
    """When `row` does not fit: the rows that do. The same repository first (a smaller quantization), then other models, the larger first. Code filters; the owner picks."""
    if not rules.on("offer_fitting_variants"):
        return []
    out = []
    for r in rows:
        if r["id"] == row["id"] or not r.get("offer_by_default", True):
            continue
        need = estimate_memory(r, rules, context=context)["total_gib"]
        if need <= budget_gib:
            out.append({"id": r["id"], "repo": r["repo"], "quantization": r.get("quantization"), "params_b": r["params_b"], "memory_needed_gib": need, "same_repo": r["repo"] == row["repo"]})
    out.sort(key=lambda v: (not v["same_repo"], -float(v["params_b"]), v["memory_needed_gib"]))
    return out[:limit]


# ---- runtime, ollama, evidence --------------------------------------------------------------------------------------------------------------------------------------------------------------------

def load_runtime_arches(path: Path | None = None) -> dict[str, Any]:
    p = path or RUNTIME_ARCHES_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def runtime_supports(arch: str | None, runtime: str, tag: str, table: dict[str, Any], rules: Rules) -> bool | None:
    """True / False when the pinned runtime's architecture table says; None when it was not read for this runtime and tag (then nothing is claimed)."""
    if not rules.on("runtime_architecture") or not arch:
        return None
    entry = (table.get(runtime) or {}).get(tag)
    return None if not entry else arch in entry["architectures"]


def check_ollama_ref(ref: dict[str, Any], rules: Rules) -> list[str]:
    """Problems with an Ollama model reference ({'name', 'digest', 'hf_name'}); empty means it is pinned and mapped. Without R5.2 nothing is checked."""
    if not rules.on("pin_ollama_digest"):
        return []
    bad = []
    name = str(ref.get("name", ""))
    if name.endswith(":latest") or ":" not in name:
        bad.append("a moving tag (`latest`, or none) is not allowed: pin the digest")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(ref.get("digest", ""))):
        bad.append("no sha256 digest pinned")
    if not ref.get("hf_name"):
        bad.append("the Hugging Face name this maps to is not recorded")
    return bad


def check_evidence(entry: dict[str, Any], rules: Rules) -> list[str]:
    facts = {**rules.policy_facts(), **{f"evidence.{k}": v for k, v in entry.items()}}
    return [e["reason"] for r in rules.rules if r.get("scope") == "evidence" and holds(r["when"], facts) for e in r["effects"] if e["action"] == "reject_evidence"]
