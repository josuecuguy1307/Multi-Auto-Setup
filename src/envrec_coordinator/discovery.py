"""The coordinator's discovery responder: ONE optional UDP socket (docs/NETWORK_CONTRACT.md 1.8). It answers a node's broadcast query by unicast with the coordinator's name, LAN address, TCP
port and fingerprint, and nothing else.

Receiving a broadcast needs a socket that is not tied to one interface address on macOS and Windows, so the socket binds to every interface and the filtering is done HERE, in code: a query
is answered only when its source is a private or link-local IPv4 address inside the subnet of one of this computer's own interfaces (virtual ones are left out unless asked for), it
is a well-formed version-1 query, and the source and the whole socket are under a rate limit. Everything else is dropped without an answer. It never sends first and never reveals more
than the fields above."""

from __future__ import annotations

import asyncio
import ipaddress
import time
from collections import deque
from collections.abc import Callable

import envrec_os
from envrec_net import discovery
from envrec_net.identity import Identity

PER_SOURCE = (5, 10.0)  # at most 5 queries per source per 10 s
GLOBAL = (60, 10.0)


class _Limit:
    def __init__(self, clock: Callable[[], float], count: int, window: float) -> None:
        self.clock, self.count, self.window, self.hits = clock, count, window, deque()

    def allow(self) -> bool:
        now = self.clock()
        while self.hits and now - self.hits[0] > self.window:
            self.hits.popleft()
        if len(self.hits) >= self.count:
            return False
        self.hits.append(now)
        return True


class DiscoveryResponder(asyncio.DatagramProtocol):
    def __init__(self, identity: Identity, *, name: str, tcp_port: int, clock=time.time, include_virtual: bool = False, interfaces: Callable[[], list] | None = None,
                 log: Callable[[str, dict], None] = lambda t, p: None, allow_source: Callable[[str], bool] | None = None) -> None:
        self.identity, self.name, self.tcp_port, self.clock = identity, name, tcp_port, clock
        self._ifaces = interfaces or (lambda: envrec_os.interfaces(include_virtual))
        self.log, self.allow_source = log, allow_source  # allow_source is for tests that talk over loopback
        self.transport: asyncio.DatagramTransport | None = None
        self._per: dict[str, _Limit] = {}
        self._all = _Limit(clock, *GLOBAL)
        self.answered = 0
        self.dropped: dict[str, int] = {}

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    def _drop(self, reason: str) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1

    def reply_for(self, data: bytes, source: str) -> bytes | None:
        """The answer to send to `source`, or None (dropped). Pure apart from the rate-limit state, so it is testable without a socket."""
        try:
            ip = ipaddress.ip_address(source)
        except ValueError:
            self._drop("bad_source")
            return None
        local = self.allow_source(source) if self.allow_source else False
        mine = None
        if not local:
            if ip.version != 4 or not (ip.is_private or ip.is_link_local) or ip.is_loopback:
                self._drop("not_private")
                return None
            mine = next((i for i in self._ifaces() if i.contains(source)), None)
            if mine is None:
                self._drop("not_on_my_subnet")
                return None
        lim = self._per.setdefault(source, _Limit(self.clock, *PER_SOURCE))
        if len(self._per) > 512:
            self._per = {source: lim}
        if not lim.allow() or not self._all.allow():
            self._drop("rate_limited")
            return None
        nonce = discovery.parse_query(data)
        if nonce is None:
            self._drop("malformed")
            return None
        address = mine.address if mine else source
        self.answered += 1
        return discovery.make_answer(self.identity, nonce, name=self.name, address=address, port=self.tcp_port, clock=self.clock)

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        reply = self.reply_for(data, addr[0])
        if reply is not None and self.transport is not None:
            self.transport.sendto(reply, addr)

    def error_received(self, exc: Exception) -> None:
        self._drop("socket_error")

    def summary(self) -> dict:
        out = {"answered": self.answered, **{f"dropped_{k}": v for k, v in self.dropped.items()}}
        self.answered, self.dropped = 0, {}
        return out


async def open_responder(responder: DiscoveryResponder, port: int, *, host: str = "0.0.0.0"):
    """THE discovery socket. `host` is every interface by default because receiving broadcasts requires it on some systems; `reply_for` filters by subnet."""
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: responder, local_addr=(host, port))
    return transport
