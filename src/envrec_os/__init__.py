"""The per-OS layer: the ONLY place that touches operating-system specifics (permissions, access control lists, which family we run on).

Everything else (coordinator, node agent, channel) is OS-independent and asks this layer through the typed interface below. What is *known* about an operating system (where it keeps
a program's state, say) is not written here: it is data in a skill (`skills/os-state-paths/references/state-dirs.json`) that this layer reads. Conformance P1 and H1 keep it that way.
"""

from __future__ import annotations

import json
import os
import platform
import re
import socket
import sys
import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

SKILLS_DIR = Path(os.environ.get("MAS_SKILLS_DIR") or Path(__file__).resolve().parents[2] / "skills")
STATE_PATHS_FILE = SKILLS_DIR / "os-state-paths" / "references" / "state-dirs.json"


class OsLayer(Protocol):
    family: str  # darwin | linux | windows

    def protect_private(self, path: Path) -> None:
        """Make a file or folder readable and writable by the current user only."""

    def is_private(self, path: Path) -> bool:
        """True when only the current user can read the file or folder."""


def family() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    return "linux"


def current() -> OsLayer:
    if family() == "windows":
        from envrec_os.windows import WindowsLayer
        return WindowsLayer()
    from envrec_os.posix import PosixLayer
    return PosixLayer(family())


def _expand(template: str, env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    s = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-([^}]*)\}", lambda m: env.get(m.group(1)) or m.group(2), template)
    s = re.sub(r"%([A-Za-z_][A-Za-z0-9_]*)%", lambda m: env.get(m.group(1), m.group(0)), s)
    return Path(os.path.expanduser(s))


def default_state_dir(env: dict[str, str] | None = None) -> Path:
    """`MAS_STATE_DIR`, else the folder the os-state-paths skill gives for this family. A family without a row has no default."""
    env = os.environ if env is None else env
    if env.get("MAS_STATE_DIR"):
        return Path(env["MAS_STATE_DIR"]).expanduser()
    table = json.loads(STATE_PATHS_FILE.read_text())
    template = table.get(family())
    if not template:
        raise LookupError(f"no default state folder is known for the {family()} family: pass --state-dir (add a row to {STATE_PATHS_FILE.name} to give it one)")
    return _expand(template, env)


def card() -> dict:
    """The node card basics a machine can report about itself: host name, operating system family and release, CPU architecture. Facts, not hardware detail."""
    return {"hostname": socket.gethostname(), "os": family(), "os_release": platform.release(), "arch": platform.machine().lower()}


# ---- network interfaces and what to tell the owner (the knowledge is data in skills; this layer reads it) --------------------------------------------------------

VIRTUAL_FILE = SKILLS_DIR / "net-interface-kinds" / "references" / "virtual-interfaces.json"
GUIDANCE_FILE = SKILLS_DIR / "discovery-guidance" / "references" / "guidance.json"


@dataclass(frozen=True)
class Iface:
    name: str
    address: str
    netmask: str
    broadcast: str
    network: ipaddress.IPv4Network

    def contains(self, ip: str) -> bool:
        return ipaddress.ip_address(ip) in self.network


def is_virtual(name: str, fam: str | None = None) -> bool:
    """True for a VPN, container, bridge or virtual-machine interface, by the per-family name table in the net-interface-kinds skill."""
    table = json.loads(VIRTUAL_FILE.read_text())
    low = name.lower()
    return any(frag in low for frag in table.get(fam or family(), table.get("common", [])))


def interfaces(include_virtual: bool = False, *, raw=None, fam: str | None = None) -> list[Iface]:
    """The up IPv4 interfaces on a private or link-local network, each with its subnet and directed-broadcast address. Virtual interfaces are left out unless asked for.
    `raw` lets a test supply `[(name, address, netmask, is_up)]` instead of reading the machine."""
    if raw is None:
        import psutil
        stats = psutil.net_if_stats()
        raw = [(n, a.address, a.netmask, stats.get(n).isup if stats.get(n) else False) for n, addrs in psutil.net_if_addrs().items() for a in addrs if a.family == socket.AF_INET]
    out = []
    for name, address, mask, up in raw:
        if not up or not address or not mask:
            continue
        try:
            net = ipaddress.IPv4Network(f"{address}/{mask}", strict=False)
            ip = ipaddress.IPv4Address(address)
        except ValueError:
            continue
        if not (ip.is_private or ip.is_link_local) or ip.is_loopback:
            continue
        if not include_virtual and is_virtual(name, fam):
            continue
        out.append(Iface(name, address, mask, str(net.broadcast_address), net))
    return out


def discovery_guidance(code: str, role: str = "node", fam: str | None = None) -> list[str]:
    """Plain guidance for the owner for a typed discovery failure, in this operating system's words (data in the discovery-guidance skill). `role` is accepted for symmetry."""
    table = json.loads(GUIDANCE_FILE.read_text()).get(code, {})
    return list(table.get("common", [])) + list(table.get(fam or family(), []))
