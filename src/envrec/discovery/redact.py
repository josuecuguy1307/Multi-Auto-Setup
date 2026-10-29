"""Redaction of captured output before it is committed as a fixture, and the leak scan that guards the committed files.

Serials, MACs, hostnames, SSIDs, usernames and IPs outside the documentation ranges never reach a committed file. The mapping is
consistent within one capture session so that relations (the gateway in the route table is the same host in the ARP table) survive.
"""

from __future__ import annotations

import getpass
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path

MAC = re.compile(r"(?<![0-9A-Fa-f:])((?:[0-9A-Fa-f]{1,2}:){5}[0-9A-Fa-f]{1,2})(?![0-9A-Fa-f:])")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
IPV6 = re.compile(r"(?<![0-9A-Fa-f:])((?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4})(?![0-9A-Fa-f:])")


def _is_v6(v: str) -> bool:
    """An IPv6 address has `::` or seven colons; anything shorter (a MAC, a clock, a colon-separated table row) is not one."""
    return ("::" in v or v.count(":") >= 7) and bool(re.search(r"[0-9A-Fa-f]", v))
UUID = re.compile(r"\b([0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12})\b")
SECRET_KEY = re.compile(r"(serial|udid|uuid|unique[-_]id|ecid|hardware_uuid|provisioning|imei|device_address|controller_address|mac_address|ethernet address)", re.I)
DOC_NETS = ["192.0.2.", "198.51.100.", "203.0.113."]
SAFE_V4 = ("127.", "0.0.0.0", "255.")


def identity_terms() -> list[str]:
    """Strings that must never appear in a committed fixture: this machine's own names."""
    terms: set[str] = set()
    try:
        terms.add(getpass.getuser())
    except Exception:
        pass
    host = socket.gethostname()
    terms |= {host, host.split(".")[0]}
    try:
        import subprocess
        for flag in ("ComputerName", "LocalHostName", "HostName"):
            r = subprocess.run(["scutil", "--get", flag], capture_output=True, text=True, timeout=3)
            if r.returncode == 0:
                terms.add(r.stdout.strip())
        full = subprocess.run(["id", "-F"], capture_output=True, text=True, timeout=3).stdout.strip()
        terms |= {full} | {w for w in re.split(r"\W+", full) if len(w) >= 4}
    except Exception:
        pass
    import os
    terms |= {t.strip() for t in os.environ.get("ENVREC_REDACT_TERMS", "").split(",") if t.strip()}  # e.g. names of private projects
    return sorted(t for t in terms if t and len(t) >= 3 and t.lower() not in {"user", "root", "localhost", "mac", "host"})


@dataclass
class Redactor:
    terms: list[str] = field(default_factory=identity_terms)
    macs: dict[str, str] = field(default_factory=dict)
    ips: dict[str, str] = field(default_factory=dict)
    v6: dict[str, str] = field(default_factory=dict)
    uuids: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)
    nets: dict[str, str] = field(default_factory=dict)

    def mac(self, m: str) -> str:
        norm = ":".join(p.zfill(2) for p in m.lower().split(":"))
        if norm not in self.macs:
            n = len(self.macs) + 1
            self.macs[norm] = ":".join(norm.split(":")[:3]) + f":00:00:{n:02x}"  # the vendor prefix is public and kept; the device part is not
        return self.macs[norm]

    def ip4(self, ip: str) -> str:
        if ip.startswith(SAFE_V4):
            return ip
        try:
            ipaddress.IPv4Address(ip)
        except ValueError:
            return ip
        if ip not in self.ips:
            net = ".".join(ip.split(".")[:3])
            if net not in self.nets:
                self.nets[net] = DOC_NETS[len(self.nets) % 3]
            self.ips[ip] = self.nets[net] + ip.split(".")[3]
        return self.ips[ip]

    def ip6(self, v: str) -> str:
        if not _is_v6(v) or v.lower() in ("::1", "::", "fe80::1") or not re.search(r"[0-9a-fA-F]", v.replace("::", "")):
            return v
        if v not in self.v6:
            self.v6[v] = f"2001:db8::{len(self.v6) + 1:x}"
        return self.v6[v]

    def uuid(self, v: str) -> str:
        if v not in self.uuids:
            self.uuids[v] = f"00000000-0000-0000-0000-{len(self.uuids) + 1:012d}"
        return self.uuids[v]

    def secret(self, v: str) -> str:
        if v not in self.secrets:
            self.secrets[v] = f"REDACTED-{len(self.secrets) + 1}"
        return self.secrets[v]

    def text(self, s: str) -> str:
        s = re.sub(r'("[^"]*(?:serial|udid|uuid|unique[-_]id|ecid|provisioning)[^"]*"\s*:\s*)"([^"]*)"', lambda m: f'{m.group(1)}"{self.secret(m.group(2))}"', s, flags=re.I)
        s = re.sub(r"((?:serial number|hardware uuid|ethernet address)[^:\n]*:\s*)(\S+)", lambda m: m.group(1) + self.secret(m.group(2)), s, flags=re.I)
        s = UUID.sub(lambda m: self.uuid(m.group(1)), s)
        s = MAC.sub(lambda m: self.mac(m.group(1)), s)
        s = IPV6.sub(lambda m: self.ip6(m.group(1)), s)
        s = IPV4.sub(lambda m: self.ip4(m.group(1)), s)
        for t in self.terms:
            s = re.sub(re.escape(t), "REDACTED-NAME", s, flags=re.I)
        s = re.sub(r"/Users/[^/\s\"]+", "/Users/user", s)
        s = re.sub(r"/home/[^/\s\"]+", "/home/user", s)
        return s

    def json_value_keys(self, s: str) -> str:
        """Second pass for JSON whose KEYS are device addresses or names (Bluetooth lists) handled by text(); kept for symmetry."""
        try:
            return json.dumps(json.loads(s), indent=2, ensure_ascii=False)
        except ValueError:
            return s


LEAK_PATTERNS = {
    "mac_address": re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])"),
    "ipv4": IPV4,
    "ipv6": IPV6,
}


def find_leaks(text: str, terms: list[str] | None = None) -> list[str]:
    """What a committed fixture must not contain. Returns human-readable findings (empty when clean)."""
    out: list[str] = []
    for m in LEAK_PATTERNS["mac_address"].finditer(text):
        if not m.group(0).lower().endswith(("00:00:" + m.group(0)[-2:].lower(),)):
            out.append(f"MAC {m.group(0)}")
    for m in IPV4.finditer(text):
        ip = m.group(1)
        if ip.startswith(SAFE_V4) or any(ip.startswith(d) for d in DOC_NETS):
            continue
        parts = ip.split(".")
        if len(parts) == 4 and all(p.isdigit() and int(p) <= 255 for p in parts) and not re.search(r"\d+\.\d+\.\d+\.\d+\.\d", text[m.start():m.end() + 2]):
            if _looks_like_version(text, m):
                continue
            out.append(f"IPv4 {ip}")
    for m in IPV6.finditer(text):
        v = m.group(1)
        if not _is_v6(v) or v.lower() in ("::1", "::", "fe80::1") or v.lower().startswith("2001:db8:") or not re.search(r"[0-9a-fA-F]{2}", v.replace(":", "")):
            continue
        out.append(f"IPv6 {v}")
    for t in terms or []:
        if re.search(re.escape(t), text, re.I):
            out.append(f"identity term {t!r}")
    if re.search(r"/Users/(?!user\b)[^/\s\"]+", text):
        out.append("home path")
    return out


def _looks_like_version(text: str, m: re.Match) -> bool:
    """4-part numbers such as 10.0.19045.1 or 23.40.31.0 are versions, not addresses, when the context says so."""
    ctx = text[max(0, m.start() - 24):m.start()].lower()
    return any(w in ctx for w in ("version", "ver ", "build", "firmware", "release", "v=", "@", "fwid", "kernel"))
