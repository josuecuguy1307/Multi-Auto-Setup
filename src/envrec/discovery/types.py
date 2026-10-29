"""Types shared by every discovery adapter: what was captured, and what a parser may return."""

from __future__ import annotations

import hashlib
import platform as _platform
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

MAX_CAPTURE_BYTES = 2_000_000


class Reason(StrEnum):
    """Why something could not be determined. An adapter never raises: it returns one of these."""

    unavailable = "unavailable"
    denied = "denied"
    timeout = "timeout"
    not_applicable = "not_applicable"
    permission = "permission"


def this_platform() -> str:
    return {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(_platform.system(), _platform.system().lower())


@dataclass(frozen=True)
class Cmd:
    """One fixed command. argv is a list, there is never a shell. `name` is the stable label used by fixtures and evidence."""

    name: str
    argv: tuple[str, ...]
    timeout_s: float = 10.0
    optional: bool = False  # a missing program is then not_applicable, not unavailable
    merge_stderr: bool = False  # some tools print their version on stderr
    window_s: float | None = None  # a streaming command (browse, listen): let it run this long, stop it, keep what it printed


@dataclass(frozen=True)
class Capture:
    """Raw bytes plus the exact source. Hashed and stored in the raw store; every fact cites one."""

    name: str
    source: str  # the argv joined for display, or a file path / socket label
    data: bytes = b""
    exit_code: int | None = None
    origin: str = "local"  # local | remote | replay
    platform: str = ""
    error: Reason | None = None
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None and (self.exit_code in (None, 0))

    @property
    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


@dataclass
class FactOut:
    key: str
    value: Any
    confidence: float = 0.9
    limitations: list[str] = field(default_factory=list)
    src: str | None = None  # capture name this fact comes from (default: the first usable capture)
    hypothesis: bool = False  # an inference, not an observation


@dataclass
class UnknownOut:
    key: str
    reason: Reason
    detail: str = ""


@dataclass
class Entity:
    """Something found in the world. Identifiers are hashed; attrs hold only typed, cleaned values."""

    kind: str  # usb | serial | ble | host | app | agent | sensor | volume | display | process_class ...
    ring: int
    ident: str  # raw identifier: hashed into the id and NEVER stored
    owned: str = "unknown"  # owned | ambient | unknown
    attrs: dict[str, Any] = field(default_factory=dict)
    resolved: bool = True  # False: a lookup found nothing, so this is also an Unknown
    prefix: str | None = None  # fact-key prefix (default derived from kind)
    src: str | None = None
    hypothesis: bool = False
    confidence: float = 0.9
    links: list[tuple[str, str]] = field(default_factory=list)  # (kind, ident) of related entities, resolved to ids by the runner


Out = FactOut | UnknownOut | Entity

KIND_PREFIX = {"usb": "usb", "serial": "serial", "ble": "ble", "host": "net", "service": "net", "app": "app", "agent": "app",
               "sensor": "sensor", "volume": "hw", "display": "hw", "gpu": "hw", "camera": "hw", "audio": "hw", "printer": "hw",
               "thunderbolt": "hw", "pci": "hw", "process_class": "app", "instrument": "instr", "signal": "signal", "container": "app",
               "model_server": "app", "unknown_exe": "app", "wifi": "net"}


def entity_key(prefix: str, entity_id: str) -> str:
    return f"{prefix}.entity.{entity_id}"


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:limit] or "x"


def short_hash(*parts: str, salt: str = "") -> str:
    h = hashlib.sha256((salt + "\x1f" + "\x1f".join(parts)).encode("utf-8", "replace")).hexdigest()
    return h[:8]
