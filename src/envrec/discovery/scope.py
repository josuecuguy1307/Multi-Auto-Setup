"""The network scope guard. Active discovery may touch ONLY private (RFC 1918) or link-local IPv4 addresses that lie inside a subnet of one of this
machine's own interfaces, no wider than the configured cap (default /24, at most /22), and at a limited rate. Everything else is refused with a typed error,
including a hostname that resolves to a public address. Multicast groups are allowed only for the two discovery protocols that need them."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

MULTICAST_ALLOWED = {"239.255.255.250": 1900, "224.0.0.251": 5353}  # SSDP and mDNS only
MAX_CAP_PREFIX, MIN_CAP_PREFIX = 30, 22


class ScopeCode(StrEnum):
    public_address = "public_address"
    not_private = "not_private"
    wrong_subnet = "wrong_subnet"
    too_wide = "too_wide"
    hostname_public = "hostname_public"
    unresolvable = "unresolvable"
    no_interface = "no_interface"
    bad_cap = "bad_cap"
    multicast_not_allowed = "multicast_not_allowed"


class ScopeError(ValueError):
    def __init__(self, code: ScopeCode, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code


def is_lan_address(ip: ipaddress._BaseAddress) -> bool:
    return isinstance(ip, ipaddress.IPv4Address) and (ip.is_private and not ip.is_loopback and not ip.is_unspecified or ip.is_link_local) and not ip.is_multicast


def own_networks() -> list[tuple[str, ipaddress.IPv4Interface]]:
    """(interface name, address/prefix) for each of this machine's own private or link-local IPv4 addresses."""
    import psutil
    out: list[tuple[str, ipaddress.IPv4Interface]] = []
    for name, entries in psutil.net_if_addrs().items():
        for e in entries:
            if e.family != socket.AF_INET or not e.netmask:
                continue
            try:
                ifc = ipaddress.IPv4Interface(f"{e.address}/{e.netmask}")
            except ValueError:
                continue
            if is_lan_address(ifc.ip):
                out.append((name, ifc))
    return out


@dataclass(frozen=True)
class ScanScope:
    network: ipaddress.IPv4Network
    interface: str
    own_address: ipaddress.IPv4Address

    @property
    def hosts(self) -> list[ipaddress.IPv4Address]:
        return [h for h in self.network.hosts() if h != self.own_address]

    @property
    def host_count(self) -> int:
        return len(self.hosts)


def scope_for_interface(*, cap_prefix: int = 24, own: Callable[[], list[tuple[str, ipaddress.IPv4Interface]]] = own_networks,
                        interface: str | None = None) -> ScanScope:
    """The default scan scope: the subnet of one of this machine's own interfaces, narrowed to the cap."""
    _check_cap(cap_prefix)
    nets = [(n, i) for n, i in own() if interface in (None, n)]
    if not nets:
        raise ScopeError(ScopeCode.no_interface, "this machine has no private or link-local IPv4 interface to scan" + (f" named {interface!r}" if interface else ""))
    name, ifc = sorted(nets, key=lambda x: (x[1].network.is_link_local, x[0]))[0]
    net = ifc.network if ifc.network.prefixlen >= cap_prefix else ipaddress.IPv4Network(f"{ifc.ip}/{cap_prefix}", strict=False)
    return ScanScope(net, name, ifc.ip)


def _check_cap(cap_prefix: int) -> None:
    if not MIN_CAP_PREFIX <= cap_prefix <= MAX_CAP_PREFIX:
        raise ScopeError(ScopeCode.bad_cap, f"the cap /{cap_prefix} is outside /{MIN_CAP_PREFIX} to /{MAX_CAP_PREFIX}")


def validate_target(spec: str, *, cap_prefix: int = 24, own: Callable[[], list[tuple[str, ipaddress.IPv4Interface]]] = own_networks,
                    resolver: Callable[..., list] = socket.getaddrinfo) -> list[ipaddress.IPv4Address]:
    """Resolve and check what a caller wants to touch. Returns the allowed addresses or raises ScopeError.

    `spec` is an IPv4 address, a CIDR range, or a hostname. A range wider than the cap, an address outside every own subnet, a public
    address, and a hostname with ANY public address are all refused."""
    _check_cap(cap_prefix)
    nets = own()
    if not nets:
        raise ScopeError(ScopeCode.no_interface, "this machine has no private or link-local IPv4 interface")
    addrs: list[ipaddress.IPv4Address]
    spec = spec.strip()
    try:
        if "/" in spec:
            net = ipaddress.IPv4Network(spec, strict=False)
            if net.prefixlen < cap_prefix:
                raise ScopeError(ScopeCode.too_wide, f"{net} is wider than the /{cap_prefix} cap")
            addrs = list(net.hosts()) or [net.network_address]
        else:
            addrs = [ipaddress.IPv4Address(spec)]
    except ipaddress.AddressValueError:
        addrs = _resolve(spec, resolver)
    except ValueError as exc:
        if isinstance(exc, ScopeError):
            raise
        addrs = _resolve(spec, resolver)
    for a in addrs:
        _check_address(a, nets, cap_prefix, hostname=None)
    return addrs


def _resolve(host: str, resolver) -> list[ipaddress.IPv4Address]:
    try:
        infos = resolver(host, None, family=socket.AF_INET)
    except OSError as exc:
        raise ScopeError(ScopeCode.unresolvable, f"{host!r} does not resolve ({type(exc).__name__})") from None
    addrs = sorted({ipaddress.IPv4Address(i[4][0]) for i in infos})
    if not addrs:
        raise ScopeError(ScopeCode.unresolvable, f"{host!r} has no IPv4 address")
    pub = [a for a in addrs if not is_lan_address(a)]
    if pub:
        raise ScopeError(ScopeCode.hostname_public, f"{host!r} resolves to {pub[0]}, which is not a private or link-local address")
    return addrs


def _check_address(a: ipaddress.IPv4Address, nets, cap_prefix: int, hostname) -> None:
    if a.is_multicast:
        raise ScopeError(ScopeCode.multicast_not_allowed, f"{a} is a multicast group")
    if a.is_loopback or a.is_unspecified:
        raise ScopeError(ScopeCode.not_private, f"{a} is a loopback or unspecified address")
    if not a.is_private and not a.is_link_local:
        raise ScopeError(ScopeCode.public_address, f"{a} is a public address")
    for _name, ifc in nets:
        net = ifc.network if ifc.network.prefixlen >= cap_prefix else ipaddress.IPv4Network(f"{ifc.ip}/{cap_prefix}", strict=False)
        if a in net:
            return
    raise ScopeError(ScopeCode.wrong_subnet, f"{a} is not on a subnet of this machine's own interfaces")


def allow_multicast(group: str, port: int) -> None:
    if MULTICAST_ALLOWED.get(group) != port:
        raise ScopeError(ScopeCode.multicast_not_allowed, f"{group}:{port} is not one of the allowed discovery groups")


class RateLimiter:
    """At most `per_s` events per second, shared by every thread of one scan."""

    def __init__(self, per_s: int, clock=None, sleep=None) -> None:
        import threading
        import time
        self.interval, self._lock, self._next = 1.0 / max(1, per_s), threading.Lock(), 0.0
        self._clock, self._sleep = clock or time.monotonic, sleep or time.sleep

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            at = max(now, self._next)
            self._next = at + self.interval
        if at > now:
            self._sleep(at - now)
