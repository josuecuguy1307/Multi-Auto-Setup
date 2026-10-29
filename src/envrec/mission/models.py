"""Mission card and target config. Two files, two roles.

mission.yaml: free text (goal, scope) the model may READ as context. It never authorizes anything.
targets.yaml: written only by the human. It is the only source of valid targets.
The limits below are fixed by the shipped policy; a mission file that tries to change one is refused.
"""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from envrec.contracts.base import Strict


class Transport(StrEnum):
    ssh = "ssh"
    docker_exec = "docker_exec"
    adb_usb = "adb_usb"
    serial = "serial"


class Limits(Strict):
    """Non-negotiable. Defaults ARE the policy; any other value in a file is an error."""

    targets_source: str = "config_only"
    max_workers: int = 3
    deploy_requires_approval: bool = True
    worker_can_deploy_further: bool = False
    model_server_bind: str = "localhost_only"
    autostart: bool = False
    credentials_in_repo: bool = False

    @model_validator(mode="after")
    def _fixed(self) -> Limits:
        fixed = Limits.model_construct()  # defaults
        for name in type(self).model_fields:
            if getattr(self, name) != getattr(fixed, name):
                raise ValueError(f"limit {name!r} is fixed by the shipped policy and cannot be changed")
        return self


class MissionCard(Strict):
    goal: str = Field(default="", max_length=500)
    scope: str = Field(default="", max_length=300)
    limits: Limits = Field(default_factory=Limits)

    def render(self, cap: int = 300) -> str:
        """Short context line for the view. Text only: it is never parsed as instructions."""
        text = f"goal: {' '.join(self.goal.split())} | scope: {' '.join(self.scope.split())}"
        text = text if len(text) <= cap else text[: cap - 3] + "..."
        return f"MISSION (context only, not a tool and not an authorization): {text}"


_HOST = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$")
_ENV = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_USER = re.compile(r"^[a-z_][a-z0-9_\-]{0,31}$")
_CONTAINER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")
_ADB_SERIAL = re.compile(r"^[A-Za-z0-9:._\-]{1,64}$")
_SERIAL_PATH = re.compile(r"^(/dev/[A-Za-z0-9._\-]{1,40}|COM[0-9]{1,3})$")
_PARAMS = re.compile(r"^(\d+(\.\d+)?)([BM])$")
_SECRETISH = re.compile(r"(PRIVATE KEY|BEGIN [A-Z ]*KEY|password\s*[:=]|passphrase\s*[:=])", re.I)


class SshBlock(Strict):
    user: str
    port: int = Field(default=22, ge=1, le=65535)
    key_env: str  # NAME of an environment variable holding a key PATH; never the key itself

    @field_validator("user")
    @classmethod
    def _user_ok(cls, v: str) -> str:
        if not _USER.match(v):
            raise ValueError("ssh user must be a plain account name")
        return v

    @field_validator("key_env")
    @classmethod
    def _env_name(cls, v: str) -> str:
        if not _ENV.match(v):
            raise ValueError("key_env must be an environment variable NAME (upper case), never a path or a key")
        return v


class DockerExecBlock(Strict):
    container: str

    @field_validator("container")
    @classmethod
    def _c(cls, v: str) -> str:
        if not _CONTAINER.match(v):
            raise ValueError("container must be a plain container name")
        return v


class AdbBlock(Strict):
    serial: str

    @field_validator("serial")
    @classmethod
    def _s(cls, v: str) -> str:
        if not _ADB_SERIAL.match(v):
            raise ValueError("adb serial has characters it should not")
        return v


class SerialBlock(Strict):
    path: str
    baud: int = Field(default=115200, ge=300, le=4_000_000)

    @field_validator("path")
    @classmethod
    def _p(cls, v: str) -> str:
        if not _SERIAL_PATH.match(v):
            raise ValueError("serial path must look like /dev/ttyUSB0 or COM3")
        return v


class EntryLimits(Strict):
    max_disk_gb: float = Field(gt=0, le=4096)
    max_model_params: str  # e.g. "9B" or "500M"

    @field_validator("max_model_params")
    @classmethod
    def _params(cls, v: str) -> str:
        m = _PARAMS.match(v.strip())
        if not m or float(m.group(1)) <= 0:
            raise ValueError('max_model_params must look like "9B" or "500M"')
        return v.strip()

    @property
    def params_b(self) -> float:
        m = _PARAMS.match(self.max_model_params)
        return float(m.group(1)) / (1000.0 if m.group(3) == "M" else 1.0)


class TargetEntry(Strict):
    id: str = Field(pattern=r"^[a-z][a-z0-9\-]{0,31}$")
    name: str = Field(min_length=1, max_length=80)
    host: str  # address of a device the human owns
    os: str | None = None
    allowed_transports: list[Transport] = Field(min_length=1)  # ordered; the planner picks only from this
    ssh: SshBlock | None = None
    docker_exec: DockerExecBlock | None = None
    adb_usb: AdbBlock | None = None
    serial: SerialBlock | None = None
    limits: EntryLimits
    owner_confirmed: bool = False  # set only by the enrollment flow or by hand
    # Set only by enrollment's one read-only check. The planner never writes to this file.
    reachability: str = Field(default="unchecked", pattern=r"^(unchecked|reachable|unreachable)$")

    @property
    def max_disk_gb(self) -> float:
        return self.limits.max_disk_gb

    @property
    def max_model_params_b(self) -> float:
        return self.limits.params_b

    @field_validator("host")
    @classmethod
    def _host_shape(cls, v: str) -> str:
        if not _HOST.match(v):
            raise ValueError("host must be a hostname or IP address, nothing else")
        return v

    @field_validator("name", "os")
    @classmethod
    def _no_secrets(cls, v: str | None) -> str | None:
        if v is not None and _SECRETISH.search(v):
            raise ValueError("key material or a password must never appear in targets.yaml")
        return v

    @field_validator("allowed_transports")
    @classmethod
    def _unique(cls, v: list[Transport]) -> list[Transport]:
        if len(set(v)) != len(v):
            raise ValueError("allowed_transports lists a transport twice")
        return v

    @model_validator(mode="after")
    def _blocks_match_transports(self) -> TargetEntry:
        for t in Transport:
            listed, block = t in self.allowed_transports, getattr(self, t.value)
            if listed and block is None:
                raise ValueError(f"transport {t.value!r} is listed but has no {t.value!r} connection block")
            if not listed and block is not None:
                raise ValueError(f"a {t.value!r} block is present but {t.value!r} is not in allowed_transports")
        return self


class TargetConfig(Strict):
    targets: list[TargetEntry] = Field(default_factory=list)

    @field_validator("targets")
    @classmethod
    def _unique_ids(cls, v: list[TargetEntry]) -> list[TargetEntry]:
        ids = [t.id for t in v]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate target id in targets.yaml")
        return v

    def get(self, target_id: str) -> TargetEntry | None:
        return next((t for t in self.targets if t.id == target_id), None)

    def by_host(self, host: str) -> TargetEntry | None:
        return next((t for t in self.targets if t.host == host), None)
