"""Signed tasks from the coordinator to a node (docs/NETWORK_CONTRACT.md 3.7 read-only, 3.9 the one write task).

A task travels inside a session envelope, and carries a CAPABILITY TOKEN signed by the coordinator's identity key. The token names: the task, the one node it is for, the one session it is
valid in, the tools it allows, that it is single-use, and a time to live that the node measures on its own monotonic clock from the moment it received the task (so it depends on nobody's
wall clock). The node checks all of it before it runs anything, and runs only what is on its read-only allowlist (`envrec_node.tasks`). The result travels back in a session envelope too."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from envrec_net.identity import Identity, node_id_of, verify_signature

TOKEN_VERSION = 1
DEFAULT_TTL_S = 120.0

STATES = ("created", "sent", "accepted", "running", "done", "failed", "rejected", "timed_out", "cancelled")
TERMINAL = ("done", "failed", "rejected", "timed_out", "cancelled")

REJECTIONS = {  # typed reasons a node (or the coordinator) gives for not running a task, in plain words
    "tool_not_allowed": "That task is not on this computer's read-only list, so it was not run.",
    "bad_parameters": "The task's parameters are not the ones that task accepts, so it was not run.",
    "bad_token": "The task's permission token did not verify, so it was not run.",
    "token_wrong_node": "The task's permission token names another computer.",
    "token_wrong_session": "The task's permission token belongs to another connection.",
    "token_reused": "The task's permission token was already used (it is single-use).",
    "token_expired": "The task's permission token expired before it could start.",
    "token_tool_mismatch": "The task's permission token does not allow this task.",
    "access_missing": "The access this task needs is not on this computer (the credential it names is missing), so nothing was sent.",
    "busy": "This computer is already running as many tasks as it is allowed to.",
    "node_offline": "The computer is not connected, so the task was not sent (tasks are not queued).",
    "write_refused": "A task that changes this computer needs the owner's approval of the exact plan; without it, and for any type but the one setup task, nothing was run.",
    "bad_plan": "The setup plan was not a well-formed plan, so nothing was run.",
    "bad_scope": "The setup step's permission token is not scoped to a recipe and a plan, so nothing was run.",
    "plan_altered": "The plan that arrived is not the plan the owner approved (its hash is different), so nothing was run.",
    "no_owner_approval": "The owner's approval of this exact plan and step is missing, so nothing was run.",
    "unknown_recipe": "This computer has no recipe with that name, so nothing was run.",
    "stale_recipe": "This computer's copy of the recipe is not the one the plan was made from, so nothing was run.",
    "plan_not_from_recipe": "The plan's steps are not the recipe's operations in the recipe's order, so nothing was run.",
    "outside_declared_writes": "The step would write outside what its recipe declares, so nothing was run.",
    "no_executor": "This computer has no function for that setup operation, so nothing was run.",
    "no_owner_origin": "The owner did not create or approve this task, so nothing was run.",
    "unknown_environment": "This computer has no environment with that name, so nothing was run.",
    "unsupported_os": "That environment does not run on this computer's operating system, so nothing was run.",
    "not_installed": "That environment is not installed on this computer, so nothing was run.",
    "sandbox_missing": "That environment may only run inside a sandbox and this computer has none ready, so nothing was run.",
    "not_loopback": "The model endpoint is not a loopback address (127.0.0.1 or ::1), so nothing was run.",
    "outside_workspace": "The environment tried to write outside its task workspace; what it wrote was removed.",
    "environment_failed": "The environment ended with an error.",
    "confidential_cloud": "A confidential task cannot run in a cloud environment, so nothing was run.",
    "credential_store_missing": "This computer has no credential store to keep the key in (Windows Credential Manager, the macOS Keychain or a Linux Secret Service). Nothing was stored, and no other place is ever used.",
    "bad_credential_name": "A credential name is a plain variable name such as MODEL_API_KEY, so nothing was done.",
    "bad_credential_value": "The credential value was empty, so nothing was stored.",
    "credential_absent": "There is no such credential on this computer, so nothing was changed.",
    "credential_store_failed": "The credential store refused the change, so nothing was changed.",
}


class TaskParams(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a parameter nobody declared (a "command", a path to write) makes the task invalid


class NodeReportParams(TaskParams):
    pass


class MachineAnalysisParams(TaskParams):
    include_apps: bool = True
    max_apps: int = Field(default=60, ge=0, le=500)


class NoParams(TaskParams):
    pass


class ProgramsParams(TaskParams):
    limit: int = Field(default=200, ge=1, le=500)


class CredentialPresenceParams(TaskParams):
    """Read-only: which of these variable NAMES exist on this computer, in its credential store or its environment. The answer is names, never values."""
    names: list[str] = Field(max_length=32)


class CredentialStepParams(TaskParams):
    """The third task that writes. A credential `set` carries its value (the only place a value travels, inside the sealed session, never logged); `remove` moves it aside inside the store and `restore`
    brings it back. It is tied to the owner's approval of one plan hash, name and action."""
    name: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    action: Literal["set", "remove", "restore"]
    plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    value: str | None = Field(default=None, max_length=4096, repr=False)

    @model_validator(mode="after")
    def _value_only_for_set(self):
        if (self.action == "set") != (self.value is not None):
            raise ValueError("bad_parameters")
        return self


class SetupStepParams(TaskParams):
    """The one task that writes. The plan travels whole; the node recomputes its hash and checks it against the signed token, the owner's approval named in it and the node's own copy of the recipe."""
    plan: dict[str, Any]
    step: int = Field(ge=1, le=40)
    action: Literal["run", "undo"] = "run"


class EnvironmentRunParams(TaskParams):
    """The second task that writes (docs/NETWORK_CONTRACT.md 3.9, amended). The environment is named, never described: its executable, arguments and confinement come from the node's own copy of the
    catalog (data), and the hash of that recipe must match the signed token. The input travels as text; the workspace is a plain id under the node's own folder."""
    environment_id: str = Field(min_length=1, max_length=48, pattern=r"^[a-z0-9][a-z0-9-]*$")
    task_type: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")  # which types exist is data (the catalog and the suites), not a list here
    sensitivity: Literal["public", "internal", "confidential"]
    mode: str = Field(min_length=1, max_length=48)
    workspace_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,47}$")
    recipe_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_text: str = Field(max_length=100_000)
    endpoint: str | None = Field(default=None, max_length=64)
    model_id: str | None = Field(default=None, max_length=120, pattern=r"^[A-Za-z0-9._:/-]+$")
    access_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")  # the access the route went through: it decides whether the run leaves the network


WRITE_TYPES = ("setup_step", "environment_run", "credential_step")  # every other task type is read-only; this is the only one the coordinator sends with a scoped token and the node runs after the checks of `envrec_node.setup`

PARAMS: dict[str, type[TaskParams]] = {"setup_step": SetupStepParams, "environment_run": EnvironmentRunParams, "credential_step": CredentialStepParams, "credential_presence": CredentialPresenceParams, "node_report": NodeReportParams, "machine_analysis": MachineAnalysisParams, "os_details": NoParams, "installed_programs": ProgramsParams, "gpus": NoParams,
                                        "model_runtimes": NoParams, "ai_clis": NoParams, "memory_for_models": NoParams}
PROBES = ("os_details", "installed_programs", "gpus", "model_runtimes", "ai_clis", "memory_for_models")  # the read-only probes a model can pick, one by one
TIMEOUTS_S = {"setup_step": 1800.0, "environment_run": 1200.0, "credential_step": 60.0, "credential_presence": 20.0, "node_report": 30.0, "machine_analysis": 240.0, "os_details": 20.0, "installed_programs": 60.0, "gpus": 30.0, "model_runtimes": 30.0, "ai_clis": 20.0, "memory_for_models": 30.0}


class TaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str
    status: Literal["done", "failed", "cancelled"]
    result: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    duration_s: float = Field(default=0.0, ge=0)
    reason: str = ""


def validate_params(task_type: str, params: dict) -> BaseModel:
    """The parameters of a known task type, or a ValueError carrying the typed reason."""
    model = PARAMS.get(task_type)
    if model is None:
        raise ValueError("tool_not_allowed")
    try:
        return model.model_validate(params)
    except ValidationError:
        raise ValueError("bad_parameters") from None


def _canon(token: dict) -> bytes:
    return json.dumps({k: v for k, v in token.items() if k != "sig"}, sort_keys=True, separators=(",", ":")).encode()


def issue_token(identity: Identity, *, task_id: str, node_id: str, sid: str, tools: list[str], ttl_s: float = DEFAULT_TTL_S, scope: dict | None = None) -> dict:
    t = {"v": TOKEN_VERSION, "jti": secrets.token_hex(12), "task": task_id, "node": node_id, "sid": sid, "tools": sorted(tools), "single_use": True, "ttl_s": float(ttl_s)}
    if scope is not None:  # a write task only: one recipe, one plan hash, one step, the paths it may write, and the owner's approval of exactly that (signed with the rest)
        t["scope"] = scope
    t["sig"] = base64.b64encode(identity.sign(_canon(t))).decode()
    return t


class TokenRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TokenChecker:
    """A node's checks on a token, in one place. `seen` is the set of token ids already used in this session."""

    def __init__(self, coordinator_public: bytes, my_node_id: str, sid: str) -> None:
        self.pub, self.me, self.sid = coordinator_public, my_node_id, sid
        self.seen: set[str] = set()

    def check(self, token: Any, *, task_id: str, task_type: str) -> dict:
        if not isinstance(token, dict):
            raise TokenRejected("bad_token")
        try:
            sig = base64.b64decode(str(token.get("sig", "")), validate=True)
            ok = verify_signature(self.pub, sig, _canon(token))
        except ValueError:
            ok = False
        if not ok or token.get("v") != TOKEN_VERSION or not isinstance(token.get("tools"), list):
            raise TokenRejected("bad_token")
        if token.get("node") != self.me:
            raise TokenRejected("token_wrong_node")
        if token.get("sid") != self.sid:
            raise TokenRejected("token_wrong_session")
        if token.get("task") != task_id or task_type not in token["tools"]:
            raise TokenRejected("token_tool_mismatch")
        jti = str(token.get("jti", ""))
        if not jti or jti in self.seen:
            raise TokenRejected("token_reused")
        self.seen.add(jti)
        return token


def credential_plan_hash(node: str, access: str, name: str, action: str) -> str:
    """The hash an owner's approval of a credential change is bound to: this node, this access, this credential name and this action. No value is part of it."""
    return "sha256:" + hashlib.sha256(json.dumps({"node": node, "access": access, "name": name, "action": action}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def credential_scope(plan_hash: str, name: str, action: str, approval_event: str) -> dict:
    """The scope signed into a credential task's token: the plan, the name and the action the owner approved, and the OWNER event that approved exactly those."""
    return {"credential": name, "action": action, "plan_hash": plan_hash, "approval": {"actor": "OWNER", "event": approval_event, "plan_hash": plan_hash, "name": name, "action": action}}


def check_credential_scope(token: dict, params: "CredentialStepParams") -> None:
    """The node's check of a credential task's token against the parameters it received. Raises TokenRejected with the typed reason; nothing is touched before this passes."""
    sc = token.get("scope")
    if not isinstance(sc, dict) or sc.get("credential") != params.name or sc.get("action") != params.action:
        raise TokenRejected("bad_scope")
    ap = sc.get("approval")
    if not (isinstance(ap, dict) and ap.get("actor") == "OWNER" and ap.get("event")):
        raise TokenRejected("no_owner_approval")
    if sc.get("plan_hash") != params.plan_hash or ap.get("plan_hash") != params.plan_hash or ap.get("name") != params.name or ap.get("action") != params.action:
        raise TokenRejected("plan_altered")


def write_scope(plan_hash: str, recipe: str, step: int, action: str, writes: list[str], approval_event: str) -> dict:
    """The scope the coordinator signs into a write task's token. `approval` names the OWNER event (its hash in the coordinator's log) that approved exactly this plan, step and action."""
    return {"recipe": recipe, "plan_hash": plan_hash, "step": int(step), "action": action, "writes": sorted(writes),
            "approval": {"actor": "OWNER", "event": approval_event, "plan_hash": plan_hash, "step": int(step), "action": action}}


def check_write_scope(token: dict, *, plan_hash: str, plan_declared_hash: str, recipe: str, step: int, action: str, writes: list[str]) -> None:
    """The node's checks of a write task's token against what it recomputed from the plan it received. Raises TokenRejected with the typed reason; nothing is run before this passes."""
    sc = token.get("scope")
    if not isinstance(sc, dict):
        raise TokenRejected("bad_scope")
    if plan_declared_hash != plan_hash or sc.get("plan_hash") != plan_hash:
        raise TokenRejected("plan_altered")  # the plan carries a hash that is not its own, or the token was signed for another plan
    ap = sc.get("approval")
    if not (isinstance(ap, dict) and ap.get("actor") == "OWNER" and ap.get("event") and ap.get("plan_hash") == plan_hash and ap.get("step") == step and ap.get("action") == action):
        raise TokenRejected("no_owner_approval")
    if sc.get("recipe") != recipe or sc.get("step") != step or sc.get("action") != action or sc.get("writes") != sorted(writes):
        raise TokenRejected("bad_scope")


def coordinator_id(public: bytes) -> str:
    return node_id_of(public)
