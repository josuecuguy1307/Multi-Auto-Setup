"""Passive listeners for the names Windows announces by itself: NetBIOS name service (UDP 137, broadcasts) and LLMNR (UDP 5355, multicast). Listening only: nothing is sent, nothing answers."""

from __future__ import annotations

import socket
import struct
import time


def _nbns_name(data: bytes) -> str | None:
    """The 15-character NetBIOS name from the first question/record of a name-service packet (first-level encoding, two letters A-P per byte)."""
    if len(data) < 12 + 34 or data[12] != 0x20:
        return None
    enc = data[13:45].decode("ascii", "replace")
    try:
        raw = bytes(((ord(enc[i]) - 65) << 4) | (ord(enc[i + 1]) - 65) for i in range(0, 30, 2))
    except (ValueError, IndexError):
        return None
    name = raw.decode("ascii", "replace").rstrip(" \x00")
    return name if name.isprintable() and name else None


def _llmnr_name(data: bytes) -> str | None:
    i, labels = 12, []
    while i < len(data) and data[i]:
        n = data[i]
        labels.append(data[i + 1:i + 1 + n].decode("ascii", "replace"))
        i += n + 1
    name = ".".join(labels)
    return name if name.isprintable() and name else None


def listen(seconds: float = 20.0, *, own_ip: str | None = None) -> tuple[list[dict], list[str]]:
    """([{ip, protocol, name}], problems). A socket that cannot be opened is reported, not hidden."""
    socks, problems = [], []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)  # the system's own name service may hold the port; sharing it lets this listener hear the broadcasts too
        s.bind(("", 137))
        socks.append((s, "netbios"))
    except OSError as exc:
        problems.append(f"netbios (udp 137): {exc.strerror}")
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind(("", 5355))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, struct.pack("4s4s", socket.inet_aton("224.0.0.252"), socket.inet_aton(own_ip or "0.0.0.0")))
        socks.append((s, "llmnr"))
    except OSError as exc:
        problems.append(f"llmnr (udp 5355): {exc.strerror}")
    seen: dict[tuple, dict] = {}
    end = time.monotonic() + seconds
    import select
    while socks and time.monotonic() < end:
        ready, _, _ = select.select([s for s, _ in socks], [], [], 0.5)
        for r in ready:
            data, (ip, _port) = r.recvfrom(1500)
            proto = next(p for s, p in socks if s is r)
            name = (_nbns_name if proto == "netbios" else _llmnr_name)(data)
            if name and ip != own_ip:
                seen[(ip, proto, name)] = {"ip": ip, "protocol": proto, "name": name}
    for s, _ in socks:
        s.close()
    return list(seen.values()), problems
