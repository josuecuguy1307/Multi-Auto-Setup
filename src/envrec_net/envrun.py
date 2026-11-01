"""The environment run: what the coordinator and a node agree on when a task is handed to an environment (a provider CLI, an own harness, the local model).

Docs: docs/ROUTER_API.md, docs/ROUTER_ENVIRONMENTS.md, docs/NETWORK_CONTRACT.md 3.9 (amended: `environment_run` is built like `setup_step`). The catalog of environments is DATA
(`skills/router-environments/references/environments.json`); this module reads it and holds the pure parts both sides share: the recipe hash, the scope the coordinator signs into the
token, the node's check of that scope, the loopback rule for a model endpoint and the expansion of an argument template. Nothing here starts a program."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
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
HUMAN_GESTURES = ("os_dialog", "host_dialog", "panel")  # the ways a human decision reaches the coordinator: the OS approval dialog, the chat host's own dialog, the owner's panel
INSTRUCTIONS_FILE = SKILLS_DIR / "router-environments" / "references" / "instructions.json"


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


def load_environments(path: Path | None = None) -> dict[str, dict]:
    """The curated catalog: id -> entry. An empty dict when the file is missing (a node with no catalog refuses every environment run)."""
    p = path or ENVIRONMENTS_FILE
    if not p.is_file():
        return {}
    envs = {e["id"]: e for e in json.loads(p.read_text(encoding="utf-8")).get("environments", [])}
    problems = check_catalog(envs)
    if problems:
        raise ValueError("the environments catalog is unsafe: " + "; ".join(problems))  # fail closed: a catalog that lets an API pass for a local model is never loaded
    return envs


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


def uses_local_endpoint(entry: dict, access_id: str | None = None) -> bool:
    """Whether a run through this access talks to the node's loopback model server."""
    ad = adapter_of(entry, access_id)
    return bool(ad.get("needs_model_endpoint")) or (ad.get("kind") == "http_chat" and not ad.get("base_url"))


def mode_of(entry: dict, task_type: str, access_id: str | None = None) -> dict | None:
    return (adapter_of(entry, access_id).get("modes") or {}).get(task_type)


def check_catalog(envs: dict[str, dict]) -> list[str]:
    """Problems that make a catalog unsafe to use, by environment and access. Empty means: an adapter that talks to an address of its own (`base_url`) is https, belongs to an access that declares
    `cloud: true`, and takes no model endpoint; an adapter that uses the node's loopback model server is never cloud."""
    out: list[str] = []
    for e in envs.values():
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
