"""A node's own state: its identity, the coordinator it is paired with (the pinned certificate and public key), and its own event log. The pin is public material; the private key
lives in the identity folder under owner-only protection."""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import envrec_os
from envrec_net import identity as ident

PIN_FILE = "coordinator.json"


@dataclass(frozen=True)
class Pin:
    address: str  # host:port
    cert_der: bytes
    paired_at: float

    @property
    def host_port(self) -> tuple[str, int]:
        host, _, port = self.address.rpartition(":")
        return host.strip("[]"), int(port)

    @property
    def coordinator_id(self) -> str:
        return ident.node_id_of(ident.public_raw_of_cert(self.cert_der))

    @property
    def public_raw(self) -> bytes:
        return ident.public_raw_of_cert(self.cert_der)


class NodeState:
    def __init__(self, state_dir: Path) -> None:
        self.dir = Path(state_dir) / "node"
        self.layer = envrec_os.current()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.layer.protect_private(self.dir)
        self.identity_dir = self.dir / "identity"
        self.log_dir = self.dir / "log"

    def identity(self) -> ident.Identity:
        return ident.load_or_create(self.identity_dir, self.layer)

    def pin(self) -> Pin | None:
        f = self.dir / PIN_FILE
        if not f.is_file():
            return None
        d = json.loads(f.read_text())
        return Pin(d["address"], base64.b64decode(d["cert_der_b64"]), d["paired_at"])

    def save_pin(self, address: str, cert_der: bytes, clock=time.time) -> Pin:
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".pin-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"address": address, "cert_der_b64": base64.b64encode(cert_der).decode(), "paired_at": clock()}, fh)
        self.layer.protect_private(Path(tmp))
        os.replace(tmp, self.dir / PIN_FILE)
        return self.pin()  # type: ignore[return-value]
