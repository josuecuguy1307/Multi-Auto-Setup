"""Signed read-only tasks from the coordinator to a node (docs/NETWORK_CONTRACT.md 3.7).

A task travels inside a session envelope, and carries a CAPABILITY TOKEN signed by the coordinator's identity key. The token names: the task, the one node it is for, the one session it is
valid in, the tools it allows, that it is single-use, and a time to live that the node measures on its own monotonic clock from the moment it received the task (so it depends on nobody's
wall clock). The node checks all of it before it runs anything, and runs only what is on its read-only allowlist (`envrec_node.tasks`). The result travels back in a session envelope too."""

from __future__ import annotations

import base64
import json
import secrets
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
    "busy": "This computer is already running as many tasks as it is allowed to.",
    "node_offline": "The computer is not connected, so the task was not sent (tasks are not queued).",
    "write_refused": "A task that changes this computer needs the owner's approval on that computer, which this version does not offer; nothing was run.",
}


class TaskParams(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a parameter nobody declared (a "command", a path to write) makes the task invalid


class NodeReportParams(TaskParams):
    pass


class MachineAnalysisParams(TaskParams):
    include_apps: bool = True
    max_apps: int = Field(default=60, ge=0, le=500)


PARAMS: dict[str, type[TaskParams]] = {"node_report": NodeReportParams, "machine_analysis": MachineAnalysisParams}
TIMEOUTS_S = {"node_report": 30.0, "machine_analysis": 240.0}


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


def issue_token(identity: Identity, *, task_id: str, node_id: str, sid: str, tools: list[str], ttl_s: float = DEFAULT_TTL_S) -> dict:
    t = {"v": TOKEN_VERSION, "jti": secrets.token_hex(12), "task": task_id, "node": node_id, "sid": sid, "tools": sorted(tools), "single_use": True, "ttl_s": float(ttl_s)}
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


def coordinator_id(public: bytes) -> str:
    return node_id_of(public)
