"""The environment run: what the coordinator and a node agree on when a task is handed to an environment (a provider CLI, an own harness, the local model).

Docs: docs/ROUTER_API.md, docs/ROUTER_ENVIRONMENTS.md, docs/NETWORK_CONTRACT.md 3.9 (amended: `environment_run` is built like `setup_step`). The catalog of environments is DATA
(`skills/router-environments/references/environments.json`); this module reads it and holds the pure parts both sides share: the recipe hash, the scope the coordinator signs into the
token, the node's check of that scope, the loopback rule for a model endpoint and the expansion of an argument template. Nothing here starts a program."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import time
from pathlib import Path
from typing import Any

from envrec_net import task as T
from envrec_os import SKILLS_DIR

ENVIRONMENTS_FILE = SKILLS_DIR / "router-environments" / "references" / "environments.json"
POLICY_FILE = SKILLS_DIR / "router-environments" / "references" / "policy.json"
SENSITIVITIES = ("public", "internal", "confidential")
CONFINEMENTS = ("none_needed", "tool_declared", "unverified", "sandbox_required")
NEEDS_SANDBOX = ("unverified", "sandbox_required")  # confinement values that only run inside our sandbox backend
WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
KNOWN_VARS = ("workspace", "prompt", "endpoint", "model")
HUMAN_GESTURES = ("os_dialog", "host_dialog", "panel", "chat_choice")  # the ways a human decision reaches the coordinator: the OS approval dialog, the chat host's own dialog, the owner's panel, and ONE chat question bound to a plan hash
INSTRUCTIONS_FILE = SKILLS_DIR / "router-environments" / "references" / "instructions.json"
EVIDENCE_STATES = ("unknown", "hypothesis", "tested", "verified", "contradicted")  # how much is known about an entry as a whole (skill router-environments); only the last two but one can be proposed as a default
NOT_DEFAULT_STATES = ("unknown", "hypothesis", "contradicted")  # an entry in one of these can be shown as a lead and is never the recommended default, nor routed
RESEARCHED_DIR_NAME = "researched"  # under the coordinator's state dir: leads the research step recorded, merged by the same loader and held to the same guard
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def load_instructions(path: Path | None = None) -> dict[str, str]:
    """Stored instruction texts by reference (`instr:verify`...), written by the project and never by a model. They carry no task data, so they never raise a task's level."""
    p = path or INSTRUCTIONS_FILE
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def decomposition_canon(parent: str, subtasks: list[dict]) -> str:
    """The exact decomposition as one canonical string: the parent and the ordered list of subtasks, each with its id, type and level. An owner decision is for THIS string and no other."""
    items = [{"id": str(x["id"]), "type": str(x["type"]), "sensitivity": str(x["sensitivity"])} for x in subtasks]
    return json.dumps({"parent": str(parent), "subtasks": items}, sort_keys=True, separators=(",", ":"))


def decomposition_id(parent: str, subtasks: list[dict], owner_event: str) -> str:
    return "dec_" + hashlib.sha256((decomposition_canon(parent, subtasks) + "|" + owner_event).encode()).hexdigest()[:20]


def researched_file(extra_dir: Path | str) -> Path:
    return Path(extra_dir) / "environments.json"


def load_catalog(path: Path | None = None, *, extra_dir: Path | str | None = None) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """(entries, rejected). The shipped catalog first (a problem in it raises: fail closed), then the researched leads the owner's state dir holds (`extra_dir`), merged by the same loader and held to the same
    guard: a lead that breaks it is left out and listed in `rejected` with its problems, it never takes the catalog down and it never replaces a shipped entry. Every entry carries `origin`."""
    p = path or ENVIRONMENTS_FILE
    if not p.is_file():
        return {}, {}
    envs = {e["id"]: {**e, "origin": "shipped"} for e in json.loads(p.read_text(encoding="utf-8")).get("environments", [])}
    problems = check_catalog(envs)
    if problems:
        raise ValueError("the environments catalog is unsafe: " + "; ".join(problems))  # fail closed: a catalog that lets an API pass for a local model is never loaded
    rejected: dict[str, list[str]] = {}
    f = researched_file(extra_dir) if extra_dir is not None else None
    if f is not None and f.is_file():
        try:
            rows = json.loads(f.read_text(encoding="utf-8")).get("environments", [])
        except (OSError, ValueError):
            rows = []
            rejected["(file)"] = ["the researched file could not be read"]
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                rejected[str(row)[:40]] = ["an entry needs an id"]
                continue
            entry = {**row, "origin": "researched"}
            bad = [f"{entry['id']} already exists in the shipped catalog; research never replaces an entry"] if entry["id"] in envs else check_catalog({entry["id"]: entry})
            if bad:
                rejected[entry["id"]] = bad
            else:
                envs[entry["id"]] = entry
    return envs, rejected


def load_environments(path: Path | None = None, *, extra_dir: Path | str | None = None) -> dict[str, dict]:
    """The curated catalog: id -> entry. An empty dict when the file is missing (a node with no catalog refuses every environment run). With `extra_dir`, the researched leads of the state dir are merged in."""
    return load_catalog(path, extra_dir=extra_dir)[0]


def is_runnable(entry: dict) -> bool:
    """An entry the router can route to: it has accesses and an adapter. A researched lead has neither, so it can be proposed and never dispatched."""
    return bool(entry.get("adapter")) and bool(entry.get("accesses"))


def runnable(envs: dict[str, dict]) -> dict[str, dict]:
    return {k: v for k, v in envs.items() if is_runnable(v)}


def load_policy(path: Path | None = None) -> dict:
    return json.loads((path or POLICY_FILE).read_text(encoding="utf-8"))


def access_of(entry: dict, access_id: str | None) -> dict | None:
    return next((a for a in entry.get("accesses", []) if a.get("id") == access_id), None)


def is_cloud(entry: dict, access_id: str | None = None) -> bool:
    """Whether a run of this environment through this access sends data out of the owner's network. `cloud` is a property of the ACCESS when the access declares it, else of the environment (one
    environment can run on a loopback model through one access and on an API through another). Fail closed: an access that is not named, or not found, counts as cloud when the environment is cloud
    or when ANY of its accesses is."""
    acc = access_of(entry, access_id)
    if acc is not None and "cloud" in acc:
        return bool(acc["cloud"])
    if acc is not None:
        return bool(entry.get("cloud"))
    return bool(entry.get("cloud")) or any(bool(a.get("cloud")) for a in entry.get("accesses", []))


def recipe_sha256(entry: dict) -> str:
    """The hash of what decides how an environment runs (its adapter, its task types, where it counts): a node whose copy differs refuses (`stale_recipe`)."""
    body = {"id": entry["id"], "cloud": bool(entry.get("cloud")), "task_types": sorted(entry.get("task_types", [])), "adapter": entry.get("adapter", {})}
    per_access = {a["id"]: bool(a["cloud"]) for a in entry.get("accesses", []) if "cloud" in a}
    if per_access:  # only when an access declares it, so the hash of an entry without any stays what it was
        body["access_cloud"] = per_access
    own_adapter = {a["id"]: a["adapter"] for a in entry.get("accesses", []) if a.get("adapter")}
    if own_adapter:
        body["access_adapter"] = own_adapter
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _merge(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        out = dict(base)
        for k, v in over.items():
            out[k] = _merge(base.get(k), v) if k in base else v
        return out
    return over


def adapter_of(entry: dict, access_id: str | None = None) -> dict:
    """The adapter of this environment as it runs through this access: the environment's own, with the access's `adapter` laid over it (dicts merge key by key, anything else is replaced). One
    environment can thus run on a loopback model through one access and on an API through another, all of it data."""
    acc = access_of(entry, access_id)
    base = entry.get("adapter", {})
    return _merge(base, acc["adapter"]) if acc is not None and acc.get("adapter") else base


def declared_model(entry: dict, access_id: str | None = None) -> str | None:
    """The model id the data names for this access (an API's model), else None: the machine's own local model is then the one."""
    acc = access_of(entry, access_id)
    return (acc or {}).get("model") or entry.get("model")


def listed_model_ok(entry: dict, access_id: str | None, model_id: str | None) -> bool:
    """For an API whose data says its models come from a live list (`models_list` in the adapter): is this id one the data's pattern accepts as a chat model? False when the entry has no such data."""
    ml = adapter_of(entry, access_id).get("models_list")
    return bool(ml and model_id and re.fullmatch(ml["chat_pattern"], model_id))


def uses_local_endpoint(entry: dict, access_id: str | None = None) -> bool:
    """Whether a run through this access talks to the node's loopback model server."""
    ad = adapter_of(entry, access_id)
    return bool(ad.get("needs_model_endpoint")) or (ad.get("kind") == "http_chat" and not ad.get("base_url"))


def mode_of(entry: dict, task_type: str, access_id: str | None = None) -> dict | None:
    return (adapter_of(entry, access_id).get("modes") or {}).get(task_type)


def entry_problems(e: dict, permissive: set[str]) -> list[str]:
    """The evidence, license and install rules of one entry (skill router-environments): an evidence state from the closed list; a researched lead is never runnable, names the page it was read from, its
    retrieval date, and (when it can be installed) a permissive license, a pin and a SHA-256 for every file; nothing that is installed by us is anything but permissive."""
    out: list[str] = []
    state, origin = e.get("evidence_state"), e.get("origin")
    where = e["id"]
    if state is not None and state not in EVIDENCE_STATES:
        out.append(f"{where}: evidence_state must be one of {', '.join(EVIDENCE_STATES)}")
    if origin == "shipped" and state is None:
        out.append(f"{where}: a shipped entry must state its evidence_state")
    domains = e.get("domains", [])
    if not isinstance(domains, list) or not all(isinstance(d, str) and d.strip() and len(d) <= 40 for d in domains):
        out.append(f"{where}: domains is a list of short tags")
    for k in ("source_url",):
        if k in e and not str(e[k]).startswith("https://"):
            out.append(f"{where}: {k} must be https")
    spdx = (e.get("license") or {}).get("spdx")
    installed = bool(e.get("installed_by_us")) or "install" in e
    if installed or origin == "researched":
        if spdx not in permissive:
            out.append(f"{where}: the license must be one of {', '.join(sorted(permissive))} (read: {spdx!r})")
    if "install" in e and e.get("subtype") != "skill_pack":  # a skill pack lists FILES, checked below
        assets = (e.get("upstream") or {}).get("assets") or {}
        if not (e.get("pin") or (e.get("upstream") or {}).get("commit")):
            out.append(f"{where}: an install needs a pin (a commit)")
        if not assets or not all(SHA256_HEX.match(str(a.get("sha256", ""))) and str(a.get("url", "")).startswith("https://") and a.get("bytes") for a in assets.values()):
            out.append(f"{where}: an install needs, for every file, an https address, its size and its SHA-256")
    if e.get("subtype") == "skill_pack":
        up = e.get("upstream") or {}
        commit = str(up.get("commit") or "")
        if not (len(commit) == 40 and all(c in "0123456789abcdef" for c in commit)):
            out.append(f"{where}: a skill pack is pinned to a 40-hex commit")
        if spdx not in permissive:
            out.append(f"{where}: a skill pack's license must be one of {', '.join(sorted(permissive))} (read: {spdx!r})")
        if e.get("adapter") or e.get("accesses"):
            out.append(f"{where}: a skill pack is instructions an environment reads; it has no adapter and no access of its own")
        if not (isinstance(e.get("pack_for"), list) and e["pack_for"] and all(isinstance(x, str) for x in e["pack_for"])):
            out.append(f"{where}: a skill pack names the environments it is for (pack_for)")
        files = up.get("files") or {}
        if not files:
            out.append(f"{where}: a skill pack lists its files")
        for name, f in files.items():
            if not (str(f.get("url", "")).startswith("https://") and commit in str(f.get("url", "")) and SHA256_HEX.match(str(f.get("sha256", ""))) and isinstance(f.get("bytes"), int) and f["bytes"] > 0):
                out.append(f"{where}: file {name!r} needs an https address pinned to the commit, its SHA-256 and its size")
    if origin == "researched":
        if e.get("adapter"):
            out.append(f"{where}: a researched lead has no adapter; only a reviewed entry in the shipped catalog can be run")
        if state in ("tested", "verified"):
            out.append(f"{where}: research can only record a hypothesis; tested and verified come from a measurement or a review")
        for k in ("source_url", "retrieved", "fetched"):
            if not e.get(k):
                out.append(f"{where}: a researched lead names its {k}")
        f = e.get("fetched")
        if f is not None and not (isinstance(f, dict) and SHA256_HEX.match(str(f.get("capture_sha256", ""))) and str(f.get("url", "")).startswith("https://")):
            out.append(f"{where}: fetched is the https page and the SHA-256 of the capture it was read from")
        for a in e.get("accesses", []) or []:
            if a.get("cost_class") not in ("local", "api", "subscription"):
                out.append(f"{where}: an access names its cost class (local, api or subscription)")
    return out


def check_catalog(envs: dict[str, dict], permissive: set[str] | None = None) -> list[str]:
    """Problems that make a catalog unsafe to use, by environment and access. Empty means: an adapter that talks to an address of its own (`base_url`) is https, belongs to an access that declares
    `cloud: true`, and takes no model endpoint; an adapter that uses the node's loopback model server is never cloud."""
    out: list[str] = []
    permissive = permissive if permissive is not None else set(load_policy().get("permissive_licenses", []))
    for e in envs.values():
        out += entry_problems(e, permissive)
        for acc in e.get("accesses", []) or [{"id": None}]:
            ad = adapter_of(e, acc.get("id"))
            where = f"{e['id']} / {acc.get('id')}"
            base = ad.get("base_url")
            if base is not None:
                if not str(base).startswith("https://"):
                    out.append(f"{where}: base_url must be https")
                if not is_cloud(e, acc.get("id")):
                    out.append(f"{where}: an adapter with a base_url leaves the network, so the access must declare cloud: true")
                if ad.get("needs_model_endpoint"):
                    out.append(f"{where}: an adapter with a base_url cannot also need the loopback model endpoint")
            elif (ad.get("kind") == "http_chat" or ad.get("needs_model_endpoint")) and is_cloud(e, acc.get("id")):
                out.append(f"{where}: a cloud access must name its base_url (the loopback endpoint is only for local models)")
    return out


# ---- the proof of execution (finding K): what the NODE records about a run inside its own signed reply ---------------------------------------------------------------------------------------

def proof_digest(proof: dict) -> str:
    """The node's own hash over its proof (every field but the digest itself): the coordinator recomputes it before it shows anything, so a field changed after the node wrote it does not verify."""
    body = {k: v for k, v in proof.items() if k != "digest"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def verify_proof(proof: Any, output_text: str) -> tuple[bool, str]:
    """(ok, why) for a proof as the coordinator received it. A proof is the node's: the coordinator never writes one, so a result without it is shown as having none, and one that does not match its digest or
    the output it travelled with is shown as not verifying."""
    if not isinstance(proof, dict) or proof.get("recorded_by") != "node":
        return False, "the node recorded no proof of this run"
    if proof.get("digest") != proof_digest(proof):
        return False, "the proof does not match its own digest"
    if proof.get("output_sha256") != hashlib.sha256(str(output_text).encode()).hexdigest():
        return False, "the proof is for another output than the one shown"
    return True, ""


def proof_line(proof: dict, ok: bool, why: str) -> str:
    """One owner-facing line from a node's proof (English). Only what the node wrote; a field it could not know is said as not reported."""
    if not ok:
        return f"Proof of execution: {why}."
    api = proof.get("api") or {}
    usage = api.get("usage") or {}
    asked = proof.get("model_requested")
    returned = api.get("model")
    model = (f"the model field in the API's answer: {returned}" + (f" (the route asked for {asked})" if asked and asked != returned else "")) if returned else "the API's model field: not reported (a local server or a tool)"
    tokens = f"tokens {usage.get('prompt_tokens')} in, {usage.get('completion_tokens')} out" if usage else "token usage: not reported"
    return (f"Proof recorded by the node {proof.get('node_host')}: ran {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(proof['started_at']))} to {time.strftime('%H:%M:%S', time.gmtime(proof['ended_at']))} UTC on its own clock; "
            f"response id {api.get('response_id') or 'not reported'}; {model}; {tokens}; {proof.get('bytes_sent')} bytes sent, {proof.get('bytes_received')} received; output sha256 {str(proof.get('output_sha256'))[:12]}.")


def parse_endpoint(endpoint: str) -> tuple[str, int]:
    """`127.0.0.1:8080` or `[::1]:8080` -> (host, port). Only the loopback LITERALS are accepted (N5): `localhost`, a name, a LAN or wildcard address is `not_loopback`."""
    m = re.fullmatch(r"(\[(?P<v6>[0-9a-fA-F:]+)\]|(?P<v4>[0-9.]+)):(?P<port>[0-9]{1,5})", endpoint or "")
    if not m:
        raise ValueError("not_loopback")
    host = m.group("v6") or m.group("v4")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("not_loopback") from None
    if not addr.is_loopback or str(addr) != host or (addr.version == 4 and host != "127.0.0.1") or (addr.version == 6 and host != "::1"):
        raise ValueError("not_loopback")
    port = int(m.group("port"))
    if not 1 <= port <= 65535:
        raise ValueError("not_loopback")
    return host, port


def expand(template: str, values: dict[str, str]) -> str:
    """Replace `{workspace}`, `{prompt}`, `{endpoint}`, `{model}` and nothing else (a template holds JSON braces, so this is not str.format)."""
    out = template
    for k in KNOWN_VARS:
        if k in values:
            out = out.replace("{" + k + "}", values[k])
    return out


def env_scope(*, entry: dict, task_type: str, sensitivity: str, workspace_id: str, endpoint: str | None, origin_event: str, decision: dict, access_id: str | None = None) -> dict:
    """The scope the coordinator signs into an `environment_run` token. `origin` names the OWNER event that created or approved the task."""
    mode = mode_of(entry, task_type, access_id) or {}
    return {"environment": entry["id"], "recipe_sha256": recipe_sha256(entry), "task_type": task_type, "sensitivity": sensitivity, "mode": mode.get("name", ""), "confinement": mode.get("confinement", ""),
            "workspace_id": workspace_id, "endpoint": endpoint or "", "access": access_id or "", "origin": {"actor": "OWNER", "event": origin_event}, "decision": decision}


def check_env_scope(token: dict, params: Any) -> None:
    """The node's check of an `environment_run` token against the parameters it received. Raises TokenRejected with the typed reason; nothing is run before this passes."""
    sc = token.get("scope")
    if not isinstance(sc, dict):
        raise T.TokenRejected("bad_scope")
    og = sc.get("origin")
    if not (isinstance(og, dict) and og.get("actor") == "OWNER" and og.get("event")):
        raise T.TokenRejected("no_owner_origin")
    if (sc.get("environment") != params.environment_id or sc.get("recipe_sha256") != params.recipe_sha256 or sc.get("task_type") != params.task_type or sc.get("sensitivity") != params.sensitivity
            or sc.get("mode") != params.mode or sc.get("workspace_id") != params.workspace_id or sc.get("endpoint") != (params.endpoint or "") or sc.get("access", "") != (getattr(params, "access_id", None) or "")):
        raise T.TokenRejected("bad_scope")
