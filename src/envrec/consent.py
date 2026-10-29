"""Network consent: the owner says ONCE that a network is theirs. It is kept in its own file (never targets.yaml, which authorizes devices, not networks), bound to a fingerprint of the
network (its subnet and the hardware address of its gateway), with the date and the owner as actor. On any other network it does not apply. With consent, read-only discovery needs no
prompt; without it, only passive discovery runs. Consent never authorizes a device: only enrollment does."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

FILE = Path(__file__).resolve().parents[2] / "consent" / "networks.json"


def _run(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def current_network(subnet: str | None = None, *, run=_run) -> dict | None:
    """{subnet, gateway, fingerprint} of the network of the default route, or None when it cannot be told. The gateway's hardware address is hashed into the fingerprint, never stored."""
    route = run(["route", "-n", "get", "default"])
    gw = re.search(r"gateway:\s*(\S+)", route)
    if gw is None:
        return None
    mac = re.search(r"at ([0-9a-f:]{11,17})", run(["arp", "-n", gw.group(1)]), re.I)
    if mac is None:
        return None
    if subnet is None:
        from envrec.discovery.scope import scope_for_interface
        subnet = str(scope_for_interface().network)
    norm = ":".join(p.zfill(2) for p in mac.group(1).lower().split(":"))
    return {"subnet": subnet, "gateway": gw.group(1), "fingerprint": hashlib.sha256(f"{subnet}|{norm}".encode()).hexdigest()[:24]}


class NetworkConsent:
    def __init__(self, path: Path = FILE) -> None:
        self.path = Path(path)

    def _read(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []

    def granted(self, net: dict | None) -> bool:
        return bool(net) and any(r["fingerprint"] == net["fingerprint"] for r in self._read())

    def record(self, net: dict, *, said: str = "", actor: str = "OWNER") -> dict:
        rows = [r for r in self._read() if r["fingerprint"] != net["fingerprint"]]
        row = {"fingerprint": net["fingerprint"], "subnet": net["subnet"], "actor": actor, "at": time.strftime("%Y-%m-%dT%H:%M:%S"), "said": said[:200]}
        rows.append(row)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(rows, indent=1))
        return row
