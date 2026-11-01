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
    return {e["id"]: e for e in json.loads(p.read_text(encoding="utf-8")).get("environments", [])}


def load_policy(path: Path | None = None) -> dict:
    return json.loads((path or POLICY_FILE).read_text(encoding="utf-8"))


def recipe_sha256(entry: dict) -> str:
    """The hash of what decides how an environment runs (its adapter, its task types, where it counts): a node whose copy differs refuses (`stale_recipe`)."""
    body = {"id": entry["id"], "cloud": bool(entry.get("cloud")), "task_types": sorted(entry.get("task_types", [])), "adapter": entry.get("adapter", {})}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def mode_of(entry: dict, task_type: str) -> dict | None:
    return (entry.get("adapter", {}).get("modes") or {}).get(task_type)


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


def env_scope(*, entry: dict, task_type: str, sensitivity: str, workspace_id: str, endpoint: str | None, origin_event: str, decision: dict) -> dict:
    """The scope the coordinator signs into an `environment_run` token. `origin` names the OWNER event that created or approved the task."""
    mode = mode_of(entry, task_type) or {}
    return {"environment": entry["id"], "recipe_sha256": recipe_sha256(entry), "task_type": task_type, "sensitivity": sensitivity, "mode": mode.get("name", ""), "confinement": mode.get("confinement", ""),
            "workspace_id": workspace_id, "endpoint": endpoint or "", "origin": {"actor": "OWNER", "event": origin_event}, "decision": decision}


def check_env_scope(token: dict, params: Any) -> None:
    """The node's check of an `environment_run` token against the parameters it received. Raises TokenRejected with the typed reason; nothing is run before this passes."""
    sc = token.get("scope")
    if not isinstance(sc, dict):
        raise T.TokenRejected("bad_scope")
    og = sc.get("origin")
    if not (isinstance(og, dict) and og.get("actor") == "OWNER" and og.get("event")):
        raise T.TokenRejected("no_owner_origin")
    if (sc.get("environment") != params.environment_id or sc.get("recipe_sha256") != params.recipe_sha256 or sc.get("task_type") != params.task_type or sc.get("sensitivity") != params.sensitivity
            or sc.get("mode") != params.mode or sc.get("workspace_id") != params.workspace_id or sc.get("endpoint") != (params.endpoint or "")):
        raise T.TokenRejected("bad_scope")
