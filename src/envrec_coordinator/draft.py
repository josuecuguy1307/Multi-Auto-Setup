"""The draft of the owner's whole network (skill `protocol-draft`). PURE CODE: the hard limits (GUARD), the typed edits, the owner profile and the draft's persistence live here. Nothing a
model says changes a limit; inside the limits the model proposes (see `draft_agent.py`) and the owner approves.

What counts as what: a CHECK is a fact measured here by code (an access was detected, a model fits the memory that was measured); an ELEMENT of the draft is either the OWNER's (stated in
an edit or an answer, and binding) or an ASSUMPTION that names what it rests on. There is no third kind: an element that is neither is refused."""

from __future__ import annotations

import copy
import functools
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from envrec_coordinator import draft_strings as S
from envrec_coordinator import options as O
from envrec_os import SKILLS_DIR

REF = SKILLS_DIR / "protocol-draft" / "references"
PROVISIONAL = O.PROVISIONAL
MAX_EDITS = 12


def _load(name: str) -> dict:
    return json.loads((REF / name).read_text())


def vocab() -> dict:
    return _load("vocab.json")


def defaults() -> dict:
    return _load("defaults.json")


def questions() -> dict:
    return _load("questions.json")


def key_id(node_id: str) -> str:
    """A node id as it appears inside a fact key."""
    return node_id.replace("-", "_")


def split_list(raw) -> list[str]:
    items = raw.split(",") if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return [x.strip() for x in items if isinstance(x, str) and x.strip()]


# ---- what a machine is, from its stored analysis ----------------------------------------------------------------------------------------------------------

def facts_of(stored: dict) -> dict:
    return {k: row["value"] for k, row in stored.get("facts", {}).items()}


def accesses(facts: dict) -> list[dict]:
    """The accesses that really exist on a machine, by presence and sign-in state only (never a token or a key value): subscription tools whose configuration was found, API key variables
    that were found, and the hardware for local models. Each names the fact it was read from."""
    out: list[dict] = []
    for c in facts.get("machine.ai_clis") or []:
        if c.get("installed") and c.get("set_up"):
            out.append({"id": f"subscription:{c['id']}", "kind": "subscription", "label": c.get("name", c["id"]), "cloud": True, "fact": "machine.ai_clis", "option_id": f"cli-{c['id']}"})
    for k in facts.get("machine.api_key_variable_names") or []:
        if k.get("provider") not in (None, "other") and not any(a["id"] == f"api:{k['provider']}" for a in out):
            out.append({"id": f"api:{k['provider']}", "kind": "api", "label": k["provider"], "cloud": True, "fact": "machine.api_key_variable_names", "option_id": f"api-{k['provider']}"})
    if facts.get("machine.model_path"):
        out.append({"id": "local_hardware", "kind": "local_hardware", "label": "this machine's own hardware", "cloud": False, "fact": "machine.model_path", "option_id": None})
    for p in facts.get("machine.programs") or []:  # a program the analysis listed that the owner may use as an embedded AI is not guessed here: only a program the analysis names as having one
        if isinstance(p, dict) and p.get("embedded_ai"):
            out.append({"id": f"app_license:{p['name']}", "kind": "app_license", "label": p["name"], "cloud": True, "fact": "machine.programs", "option_id": None})
    return out


def machine_summary(node_id: str, name: str, stored: dict) -> dict:
    f = facts_of(stored)
    return {"id": node_id, "name": name, "os": f.get("machine.os.name"), "arch": f.get("machine.os.arch"), "ram_total_gib": f.get("machine.ram_total_gib"),
            "ram_available_gib": f.get("machine.ram_available_gib"), "model_path": f.get("machine.model_path"), "vram_gib": f.get("machine.vram_gib"),
            "gpus": [g.get("name") for g in f.get("machine.gpus") or []], "runtimes": [r.get("id") for r in f.get("machine.runtimes") or []],
            "largest_model_params_b": f.get("machine.largest_model_params_b"), "accesses": [a["id"] for a in accesses(f)], "analysis_run": stored.get("run")}


@functools.cache
def _catalog() -> dict:
    """The shipped catalog is a 2 s YAML parse; the draft asks for it once per machine and sensitivity, so it is parsed once. Callers get a copy."""
    return O.load_catalog()


def fitting_local_options(facts: dict, sensitivity: str, catalog: dict | None = None, harnesses: dict | None = None) -> tuple[dict, dict]:
    """(kept by id, removed by id -> reason): the local candidates after the SAME filters the setup options use (memory budget, disk, license class)."""
    catalog = catalog if catalog is not None else copy.deepcopy(_catalog())
    harnesses = harnesses or O.load_harnesses()
    rules = O.load_rules()
    needs = tuple(rules.data["tasks"]["default"])
    cands = [o for o in O.candidates(facts, catalog, harnesses, rules=rules, task_needs=needs) if o["kind"] == "local"]
    kept, removed = O.apply_filters(cands, facts, "local", sensitivity, rules=rules, task_needs=needs, catalog=catalog)
    return {o["id"]: o for o in kept}, {o["id"]: o["removed_by"] for o in removed}


# ---- the owner's words, as typed edits ----------------------------------------------------------------------------------------------------------------------

@dataclass
class Constraints:
    """What the OWNER has stated (edits of this round and answers). These bind the draft; the profile does not."""
    resources: dict = field(default_factory=dict)  # machine id -> list of resource tokens
    sensitivity: dict = field(default_factory=dict)  # machine id -> id
    excluded: set = field(default_factory=set)
    director: str | None = None
    topology: str | None = None
    freedom: str | None = None
    rhythm: str | None = None
    tasks: str | None = None
    work_sensitivity: str | None = None  # the network-wide answer
    source: dict = field(default_factory=dict)  # (kind, machine|None) -> "owner.edit.N" | "owner.answer.q"

    def sensitivity_for(self, mid: str) -> tuple[str | None, str | None]:
        if mid in self.sensitivity:
            return self.sensitivity[mid], self.source.get(("sensitivity", mid))
        return self.work_sensitivity, self.source.get(("work_sensitivity", None))


def resolve_machine(ref: str, machines: dict[str, dict]) -> str | None:
    """A machine id exactly as listed; nothing else (no name, no key form)."""
    ref = str(ref).strip()
    return ref if ref in machines else None


def edit_error(kind: str, machine: str, value: str, quote: str, correction: str | None, machines: dict[str, dict]) -> tuple[str, str] | tuple[None, dict]:
    """(code, reason) for an edit the guard refuses, or (None, the typed edit). The quote must be words the owner wrote this round (checked by code, as for any quoted claim)."""
    v = vocab()
    spec = v["edit_kinds"].get(kind)
    if spec is None:
        return "bad_edit_kind", f"the edit kinds are {sorted(v['edit_kinds'])}"
    if not correction:
        return "no_owner_correction", "the owner has not said anything to correct in this round"
    q = " ".join(str(quote).split()).casefold()
    if len(q) < 3 or q not in " ".join(correction.split()).casefold():
        return "quote_not_in_owner_words", "the quote must be words the owner wrote, copied exactly"
    mid = None
    if "machine" in spec["needs"]:
        mid = resolve_machine(machine, machines)
        if mid is None:
            return "unknown_machine", f"the machine ids are {sorted(machines)}"
    out: object = None
    if "value" in spec["needs"]:
        val = " ".join(str(value).split())
        if spec["value"] == "resources":
            toks = split_list(val)
            if not toks or any(t not in v["resources"] for t in toks):
                return "bad_value", f"resources are a comma separated list of {sorted(v['resources'])}"
            out = toks
        elif spec["value"] == "sensitivity":
            if val not in v["sensitivity"]:
                return "bad_value", f"sensitivity is one of {v['sensitivity']}"
            out = val
        elif spec["value"] in ("topologies", "freedom", "rhythm"):
            if val not in v[spec["value"]]:
                return "bad_value", f"{kind} is one of {sorted(v[spec['value']])}"
            out = val
        else:  # tasks: free words, short
            if not val:
                return "bad_value", "say the tasks"
            out = val[:160]
    return None, {"kind": kind, "machine": mid, "value": out, "quote": " ".join(str(quote).split())[:200]}


def apply_edit(c: Constraints, edit: dict, ref: str) -> None:
    k, m, val = edit["kind"], edit["machine"], edit["value"]
    if k == "resources":
        c.resources[m] = val
    elif k == "sensitivity":
        c.sensitivity[m] = val
    elif k == "exclude_machine":
        c.excluded.add(m)
    elif k == "director":
        c.director = m
    else:
        setattr(c, k, val)
    c.source[(k, m)] = ref


# ---- the hard limits (GUARD) -------------------------------------------------------------------------------------------------------------------------------

LOCAL_SHOWN = 6  # local models listed per machine (the written order of the setup options); the total that survive the limits is always shown beside them
IMPLIED_RESOURCE = {"subscription": "plan", "api": "api", "local_hardware": "local_models", "app_license": "app"}


def valid_refs(known_keys: set[str]) -> list[str]:
    """The closed set an assumption may rest on: the analyses, the owner's answers and edits, the profile, the previous draft, and the listed defaults. Written out for the model; it picks, it never types."""
    pre = ("network.machine.", "owner.answer.", "owner.edit.", "owner.profile.")
    return sorted(k for k in known_keys if k.startswith(pre) or k == "draft.previous") + [f"default:{k}" for k in sorted(defaults())]


def refs_error(raw, valid: list[str]) -> tuple[str, str] | tuple[None, list[str]]:
    """(code, reason) or (None, refs). At least one ref, and every one is in the closed set; a ref that is not lists the valid ones."""
    refs = split_list(raw)
    if not refs:
        return "assumption_unmarked", "the proposal is made of assumptions: say what it rests on (rests_on), from the listed values"
    bad = [r for r in refs if r not in valid]
    if bad:
        return "unresolved_basis", f"not valid: {bad}; the valid values are {valid}"
    return None, refs


def choice_id(mid: str, subtype: str, access: str, model: str = "none") -> str:
    return f"{mid}/{subtype}/{access}" + (f"/{model}" if model not in ("none", "") else "")


def parse_choice(cid: str) -> tuple[str, str, str, str] | None:
    parts = str(cid).split("/", 3)
    return (parts[0], parts[1], parts[2], parts[3] if len(parts) == 4 else "none") if len(parts) >= 3 else None


def machine_choices(mid: str, summary: dict, facts: dict, c: "Constraints", catalog: dict | None = None, harnesses: dict | None = None) -> dict:
    """The closed set of environments a machine may be drafted with: every one is a combination that passes the hard limits NOW (detected access, memory fit, license class, sensitivity, what the owner
    stated). Local models are the first `LOCAL_SHOWN` of the setup options' written order; `local_total` says how many survive the limits."""
    v = vocab()
    sens = c.sensitivity_for(mid)[0] or "internal"
    out: list[str] = []
    total = 0
    for a in accesses(facts):
        for subtype, sub in v["subtypes"].items():
            if a["kind"] not in sub["access_kinds"]:
                continue
            if sub["needs_model"]:
                kept, _ = fitting_local_options(facts, sens, catalog, harnesses)
                ordered = O.order_options(list(kept.values()))
                total = len(ordered)
                models = [o["id"] for o in ordered[:LOCAL_SHOWN]]
            else:
                models = ["none"]
            for m in models:
                res = c.resources.get(mid) or [IMPLIED_RESOURCE[a["kind"]]]
                errs, _ = machine_errors(summary, facts, c, subtype=subtype, access=a["id"], model=m, resources=res, sensitivity=sens, catalog=catalog, harnesses=harnesses)
                if not errs:
                    out.append(choice_id(mid, subtype, a["id"], m))
    return {"choices": out, "local_total": total}


def team_choices(machine_ids: list[str], c: "Constraints") -> list[str]:
    """The closed set of teams that pass the topology limits for the machines the owner kept (and agree with what the owner stated)."""
    import itertools
    live = [m for m in sorted(machine_ids) if m not in c.excluded]
    freedom, rhythm = c.freedom or "propose_only", c.rhythm or "owner_present"
    cands: list[tuple[str, list[str]]] = [("single", [m]) for m in live] + [("director_workers", [m]) for m in live] + [("peers", [])]
    if 2 <= len(live) <= 4:
        cands += [("chain", list(p)) for p in itertools.permutations(live)]
    out = []
    for topo, director in cands:
        if not network_errors(topo, director, machine_ids, c, freedom=freedom, rhythm=rhythm):
            out.append(topo + ("/" + ",".join(director) if director else ""))
    return out


def parse_team(tid: str) -> tuple[str, list[str]]:
    topo, _, rest = str(tid).partition("/")
    return topo, split_list(rest)


def machine_errors(summary: dict, facts: dict, c: Constraints, *, subtype: str, access: str, model: str, resources: list[str], sensitivity: str,
                   catalog: dict | None = None, harnesses: dict | None = None) -> tuple[list[dict], list[dict]]:
    """(refusals, checks). A refusal is a typed hard limit; a check is something measured here that the draft rests on."""
    v = vocab()
    errs: list[dict] = []
    checks: list[dict] = []
    sub = v["subtypes"].get(subtype)
    if sub is None:
        return [{"rule": "bad_subtype", "reason": f"the subtypes are {sorted(v['subtypes'])}"}], checks
    have = {a["id"]: a for a in accesses(facts)}
    acc = have.get(access)
    if acc is None:
        errs.append({"rule": "access_not_detected", "reason": f"{access!r} was not detected on this machine; the detected accesses are {sorted(have)} (a tool counts only with a sign-in found, an API key only when its variable was found)"})
    elif acc["kind"] not in sub["access_kinds"]:
        errs.append({"rule": "access_kind_mismatch", "reason": f"a {subtype} environment uses an access of kind {sub['access_kinds']}, not {acc['kind']}"})
    else:
        checks.append({"rule": "access_present", "ok": True, "fact": acc["fact"], "detail": f"{access} was detected"})
        if acc["cloud"] and sensitivity == "confidential":
            errs.append({"rule": "confidential_cloud", "reason": "this machine is confidential: an access that sends data to a provider is not allowed"})
        else:
            checks.append({"rule": "sensitivity_allows_access", "ok": True, "fact": "owner.sensitivity" if sensitivity != "internal" else "default", "detail": f"{sensitivity}: {'cloud allowed' if acc['cloud'] else 'stays on the machine'}"})
        allowed = c.resources.get(summary["id"])
        if allowed is not None and acc["kind"] not in {v["resources"][t]["access_kind"] for t in allowed}:
            errs.append({"rule": "outside_owner_resources", "reason": f"the owner said this machine puts {allowed}; {acc['kind']} is not among them"})
    if sub["needs_model"]:
        kept, removed = fitting_local_options(facts, sensitivity, catalog, harnesses)
        if model in kept:
            budget, basis = O._memory_budget(O._norm(facts))
            checks.append({"rule": "model_fits", "ok": True, "fact": "machine.ram_available_gib", "detail": f"{model} needs {kept[model].get('memory_needed_gib')} GiB; budget {budget} GiB from {basis}"})
            checks.append({"rule": "license_allowed", "ok": True, "fact": "catalog.lock.yaml", "detail": f"license {kept[model].get('license')} is on the allowed list"})
        elif model in removed:
            errs.append({"rule": "model_not_allowed", "reason": f"{model}: {removed[model]}"})
        else:
            errs.append({"rule": "unknown_model", "reason": f"{model!r} is not a local candidate for this machine; the ones that survive the filters are {sorted(kept)}"})
    elif model not in ("none", ""):
        errs.append({"rule": "model_not_applicable", "reason": f"a {subtype} environment brings its own model: use model none"})
    bad = [t for t in resources if t not in v["resources"]]
    if bad or not resources:
        errs.append({"rule": "bad_resources", "reason": f"resources are a comma separated list of {sorted(v['resources'])}"})
    forced = c.resources.get(summary["id"])
    if forced is not None and sorted(resources) != sorted(forced):
        errs.append({"rule": "contradicts_owner", "reason": f"the owner said this machine puts {forced}; the draft says {resources}"})
    return errs, checks


def network_errors(topology: str, director: list[str], machine_ids: list[str], c: Constraints, *, freedom: str, rhythm: str) -> list[dict]:
    v = vocab()
    errs: list[dict] = []
    top = v["topologies"].get(topology)
    if top is None:
        return [{"rule": "bad_value", "reason": f"topology is one of {sorted(v['topologies'])}"}]
    if freedom not in v["freedom"]:
        errs.append({"rule": "bad_value", "reason": f"freedom is one of {sorted(v['freedom'])}"})
    if rhythm not in v["rhythm"]:
        errs.append({"rule": "bad_value", "reason": f"rhythm is one of {sorted(v['rhythm'])}"})
    live = [m for m in machine_ids if m not in c.excluded]
    if len(live) < top["min_machines"]:
        errs.append({"rule": "topology_needs_machines", "reason": f"{topology} needs at least {top['min_machines']} machines and {len(live)} are in the draft"})
    kind = top["director"]
    if kind == "none" and director:
        errs.append({"rule": "director_not_applicable", "reason": f"{topology} has no director: use none"})
    elif kind == "one" and len(director) != 1:
        errs.append({"rule": "director_needed", "reason": f"{topology} needs exactly one director machine id"})
    elif kind == "ordered" and sorted(director) != sorted(live):
        errs.append({"rule": "chain_order_incomplete", "reason": f"a chain lists every machine in the draft once, in order: {live}"})
    unknown = [d for d in director if d not in machine_ids]
    if unknown:
        errs.append({"rule": "director_unknown", "reason": f"unknown machines {unknown}"})
    gone = [d for d in director if d in c.excluded]
    if gone:
        errs.append({"rule": "director_excluded", "reason": f"the owner left {gone} out"})
    for name, forced, given in (("topology", c.topology, topology), ("freedom", c.freedom, freedom), ("rhythm", c.rhythm, rhythm)):
        if forced is not None and forced != given:
            errs.append({"rule": "contradicts_owner", "reason": f"the owner said {name} is {forced}; the draft says {given}"})
    if c.director is not None and kind in ("one",) and director != [c.director]:
        errs.append({"rule": "contradicts_owner", "reason": f"the owner said {c.director} directs"})
    return errs


# ---- the owner profile and the persisted draft ------------------------------------------------------------------------------------------------------------

class DraftStore:
    """`<state>/drafts/current.json` (the latest draft, proposed or approved), `approved.json` (what an approval produced) and `profile.json` (plain data, no secrets: the owner's own words
    and the typed edits they became)."""

    def __init__(self, root: Path) -> None:
        self.dir = Path(root) / "drafts"

    def _read(self, name: str):
        try:
            return json.loads((self.dir / name).read_text())
        except (OSError, ValueError):
            return None

    def _write(self, name: str, doc) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f"{name}.tmp"
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1, default=str))
        tmp.replace(self.dir / name)

    def current(self) -> dict | None:
        return self._read("current.json")

    def save_current(self, doc: dict) -> None:
        self._write("current.json", doc)

    def approved(self) -> dict | None:
        return self._read("approved.json")

    def save_approved(self, doc: dict) -> None:
        self._write("approved.json", doc)

    def profile(self) -> dict:
        return self._read("profile.json") or {"version": 1, "entries": []}

    def profile_add(self, entries: list[dict]) -> None:
        p = self.profile()
        n = len(p["entries"])
        p["entries"] += [{**e, "id": f"p{n + i + 1}"} for i, e in enumerate(entries)]
        self._write("profile.json", p)

    def profile_reset(self) -> int:
        n = len(self.profile()["entries"])
        self._write("profile.json", {"version": 1, "entries": []})
        return n


def profile_entries(edits: list[dict], machines: dict[str, dict], round_: int) -> list[dict]:
    """The owner's corrections of an approved draft, as profile data. A machine is kept by its id and its name at the time; the owner's words are kept as typed (no value of any key is ever read)."""
    now = time.time()
    return [{"kind": e["kind"], "machine": e.get("machine"), "machine_name": (machines.get(e.get("machine")) or {}).get("name"), "value": e["value"], "quote": e["quote"], "round": round_, "at": now}
            for e in edits]


def profile_facts(profile: dict) -> dict[str, dict]:
    """`owner.profile.N` -> the entry (what the view shows as context)."""
    return {f"owner.profile.{e['id']}": e for e in profile.get("entries", [])}


# ---- the output of an approved draft -----------------------------------------------------------------------------------------------------------------------

def approved_output(doc: dict) -> dict:
    """What an approval produces, without executing anything: per machine the pick in the shape the setup-plan builder consumes (machine, option, sensitivity), and the team topology record for
    the later router and board."""
    picks = []
    for mid, m in sorted(doc["machines"].items()):
        env = m["environment"]
        picks.append({"machine_id": mid, "option_id": env.get("option_id"), "sensitivity": m["sensitivity"]["value"], "subtype": env["subtype"], "access": env["access"],
                      "note": "nothing to install: the AI is already inside an application" if env["subtype"] == "embedded" else "", "draft_round": doc["round"], "executed": False})
    n = doc["network"]
    live = sorted(doc["machines"])
    roles = {mid: "worker" for mid in live}
    if n["topology"] == "director_workers":
        roles[n["director"][0]] = "director"
    elif n["topology"] == "peers":
        roles = {mid: "peer" for mid in live}
    elif n["topology"] == "chain":
        roles = {mid: f"link {i}" for i, mid in enumerate(n["director"], 1)}
    elif n["topology"] == "single":
        roles = {mid: ("single" if mid in n["director"] else "idle") for mid in live}
    return {"picks": picks, "topology": {"topology": n["topology"], "director": n["director"], "roles": roles, "freedom": n["freedom"], "rhythm": n["rhythm"], "tasks": n["tasks"],
                                        "draft_round": doc["round"], "status": PROVISIONAL}}


# ---- intents for the Harness App (T2b's schema) -------------------------------------------------------------------------------------------------------------

SENS_RANK = {"public": 0, "internal": 1, "confidential": 2}


def _why(refs: list[str]) -> str:
    d = defaults()
    return "; ".join(d.get(r[len("default:"):], r) if r.startswith("default:") else S.BASED_ON.format(ref=r) for r in refs)


def _assumption_text(el: dict) -> str:
    return S.ASSUMPTION.format(element=el["element"], value=el["value"], why=_why(el.get("rests_on", [])))


def draft_text(doc: dict) -> str:
    """The owner-facing text of a draft: what is proposed per machine, what is assumed."""
    v = vocab()
    lines = [S.TITLE.format(round=doc["round"])]
    n = doc["network"]
    lines.append(S.TEAM.format(topology=v["topologies"][n["topology"]]["label"], freedom=v["freedom"][n["freedom"]]["label"], rhythm=v["rhythm"][n["rhythm"]]["label"]))
    for mid, m in sorted(doc["machines"].items()):
        env = m["environment"]
        lines.append(S.MACHINE.format(name=m["name"], subtype=v["subtypes"][env["subtype"]]["label"], access=env["access"]) + (S.MODEL.format(model=env["model"]) if env.get("model") else "")
                     + f". {m.get('because', '')}".rstrip())
        lines += [f"    - {_assumption_text(e)}" for e in m["elements"] if e["status"] == "assumption"]
    lines += [f"  - {_assumption_text(e)}" for e in n["elements"] if e["status"] == "assumption"]
    if doc.get("excluded"):
        lines.append(S.EXCLUDED.format(names=", ".join(doc["excluded"])))
    return "\n".join(lines)


def show_data_intent(doc: dict, run_id: str, actor: str = "MODEL") -> dict:
    top = max((m["sensitivity"]["value"] for m in doc["machines"].values()), key=SENS_RANK.get, default="internal")
    return {"kind": "show_data", "run_id": run_id, "actor": actor, "sensitivity": top,
            "payload": {"card": "draft", "round": doc["round"], "network": doc["network"], "machines": doc["machines"], "excluded": doc.get("excluded", []), "status": PROVISIONAL},
            "text_fallback": draft_text(doc)}


def ask_choice_intent(pending: dict, run_id: str, sensitivity: str = "internal") -> dict:
    opts = pending["options"]
    return {"kind": "ask_choice", "run_id": run_id, "actor": "GUARD", "sensitivity": sensitivity, "payload": {"question": pending["question"], "text": pending["text"], "options": opts},
            "text_fallback": pending["text"] + "\n" + "\n".join(f"{i}. {o['label']} — {o.get('detail', '')}".rstrip(" —") for i, o in enumerate(opts, 1))}
