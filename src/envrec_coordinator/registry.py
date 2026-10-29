"""The paired nodes, on disk: id, name, OS, architecture, the PUBLIC certificate that was pinned, when it was paired and when it last sent a heartbeat. Private keys never go here."""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from envrec_net.identity import fingerprint_of, node_id_of, public_raw_of_cert

FILE = "nodes.json"


@dataclass
class NodeRecord:
    node_id: str
    name: str
    os: str
    arch: str
    cert_der_b64: str
    fingerprint: str
    paired_at: float
    last_heartbeat: float | None = None

    @property
    def cert_der(self) -> bytes:
        return base64.b64decode(self.cert_der_b64)

    @property
    def public_raw(self) -> bytes:
        return public_raw_of_cert(self.cert_der)


class Registry:
    def __init__(self, directory: Path, layer) -> None:
        self.dir, self.layer = Path(directory), layer
        self.path = self.dir / FILE
        self._nodes: dict[str, NodeRecord] = {}
        self.reload()

    def reload(self) -> None:
        self._nodes = {}
        if self.path.is_file():
            for row in json.loads(self.path.read_text()):
                rec = NodeRecord(**row)
                self._nodes[rec.node_id] = rec

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.layer.protect_private(self.dir)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".nodes-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump([asdict(n) for n in self._nodes.values()], fh, indent=1)
        self.layer.protect_private(Path(tmp))
        os.replace(tmp, self.path)

    def add(self, *, name: str, os_family: str, arch: str, cert_der: bytes, clock=time.time) -> NodeRecord:
        nid = node_id_of(public_raw_of_cert(cert_der))
        rec = NodeRecord(nid, name[:80], os_family[:20], arch[:20], base64.b64encode(cert_der).decode(), fingerprint_of(cert_der), clock())
        self._nodes[nid] = rec
        self._save()
        return rec

    def get(self, node_id: str) -> NodeRecord | None:
        return self._nodes.get(node_id)

    def by_cert(self, cert_der: bytes) -> NodeRecord | None:
        fp = fingerprint_of(cert_der)
        return next((n for n in self._nodes.values() if n.fingerprint == fp), None)

    def public_key(self, node_id: str) -> bytes | None:
        n = self._nodes.get(node_id)
        return n.public_raw if n else None

    def all(self) -> list[NodeRecord]:
        return sorted(self._nodes.values(), key=lambda n: n.paired_at)

    def trusted_certs(self) -> list[bytes]:
        return [n.cert_der for n in self._nodes.values()]

    def heartbeat(self, node_id: str, at: float) -> None:
        n = self._nodes.get(node_id)
        if n is not None:
            n.last_heartbeat = at
            self._save()

    def remove(self, node_id: str) -> bool:
        gone = self._nodes.pop(node_id, None) is not None
        if gone:
            self._save()
        return gone
