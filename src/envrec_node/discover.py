"""A node looking for coordinators (docs/NETWORK_CONTRACT.md 1.8). It opens ONE short-lived UDP socket that it never binds to a port of its own: the operating system gives it an ephemeral
port the first time it sends. It sends its query, listens for the answers to THAT query until the window ends, and closes the socket. It never answers anyone and never listens afterwards.

Where it sends: the subnet-directed broadcast address of every private or link-local IPv4 interface that is up (virtual ones are skipped unless asked for) and 255.255.255.255. Nothing is sent
to a public address. Before pairing an answer is a hint; a paired node passes its pinned coordinator and accepts only an answer signed by that key."""

from __future__ import annotations

import ipaddress
import select
import socket
import time
from collections.abc import Callable

import envrec_os
from envrec_net import discovery

WINDOW_S = 2.0
LIMITED_BROADCAST = "255.255.255.255"


class DiscoveryFailed(Exception):
    """`code` is typed (no_interface, blocked_locally, nothing_answered); `guidance` is the owner-facing text, in this operating system's words, from the discovery-guidance skill."""

    def __init__(self, code: str, message: str, guidance: list[str]) -> None:
        super().__init__(message)
        self.code, self.guidance = code, guidance

    def text(self) -> str:
        return " ".join([str(self), *self.guidance])


def destinations(port: int, include_virtual: bool = False, ifaces=None) -> list[tuple[str, int]]:
    """Where a query goes: each interface's directed broadcast, then the limited broadcast. Only private or link-local networks ever appear here."""
    out: list[tuple[str, int]] = []
    for i in (ifaces if ifaces is not None else envrec_os.interfaces(include_virtual)):
        if (i.broadcast, port) not in out:
            out.append((i.broadcast, port))
    if out:
        out.append((LIMITED_BROADCAST, port))
    return out


def discover(port: int = discovery.DEFAULT_PORT, window_s: float = WINDOW_S, *, include_virtual: bool = False, targets: list[tuple[str, int]] | None = None,
             pinned: tuple[str, bytes] | None = None, clock: Callable[[], float] = time.time, on_socket: Callable[[socket.socket], None] | None = None) -> list[discovery.Answer]:
    """Ask the network which coordinators are there. `targets` replaces the broadcast addresses (a test, or an owner who named an address). `pinned` is (coordinator id, public key) for a paired
    node. Raises DiscoveryFailed with typed guidance when nothing can be sent or nothing answers."""
    dests = targets if targets is not None else destinations(port, include_virtual)
    if not dests:
        raise DiscoveryFailed("no_interface", "No private network interface is up on this computer.", envrec_os.discovery_guidance("no_interface"))
    data, nonce = discovery.new_query()
    found: dict[tuple[str, int], discovery.Answer] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
        if on_socket:
            on_socket(sock)
        sent = 0
        for d in dests:
            try:
                sock.sendto(data, d)
                sent += 1
            except OSError:
                continue
        if not sent:
            raise DiscoveryFailed("blocked_locally", "This computer refused to send the discovery query.", envrec_os.discovery_guidance("blocked_locally"))
        end = time.monotonic() + window_s
        while True:
            left = end - time.monotonic()
            if left <= 0:
                break
            ready, _, _ = select.select([sock], [], [], left)
            if not ready:
                break
            try:
                raw, _addr = sock.recvfrom(discovery.MAX_ANSWER + 1)
            except OSError:
                continue
            try:
                ans = discovery.parse_answer(raw, nonce, pinned=pinned)
            except discovery.BadAnswer:
                continue
            found[(ans.address, ans.port)] = ans
    finally:
        sock.close()  # the window is over: nothing of this node listens any more
    if not found:
        raise DiscoveryFailed("nothing_answered", "No coordinator answered.", envrec_os.discovery_guidance("nothing_answered"))
    return sorted(found.values(), key=lambda a: (a.name, a.address))


def parse_address(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    ipaddress.ip_address(host.strip("[]"))
    if not port.isdigit():
        raise ValueError(text)
    return host.strip("[]"), int(port)
