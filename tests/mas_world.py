"""A coordinator and its nodes on loopback inside one process (SIMULATED-LOCAL: real TLS sockets, one machine). Shared by the mas tests."""
from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from envrec_coordinator.server import Coordinator
from envrec_node.pair import pair


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


@contextlib.asynccontextmanager
async def coordinator(tmp: Path, *, clock=None, answer=True, owner_ttl_s: float = 120.0, idle_timeout_s: float = 30.0, web: bool = False, discovery: bool = False):
    """A running coordinator whose owner answers `answer` (True, False, or an async function of the summary); `answer=None` leaves the real front ends (terminal files, web) in charge."""
    c = Coordinator(tmp / "coord", "127.0.0.1", 0, clock=clock or Clock(), idle_timeout_s=idle_timeout_s, owner_ttl_s=owner_ttl_s, web=web, discovery_port=0 if discovery else None)
    await c.start()
    if c.responder is not None:
        c.responder.allow_source = lambda ip: ip.startswith("127.")  # the tests talk over loopback
    asked: list[dict] = []

    async def owner(summary: dict) -> bool:
        asked.append(summary)
        if callable(answer):
            return await answer(summary)
        return answer

    if answer is not None:
        c.desk.ask_owner = owner
    c.asked = asked  # type: ignore[attr-defined]
    try:
        yield c
    finally:
        await c.stop()


def offer(c: Coordinator, code: str = "482915", oid: str = "offer1") -> str:
    c.desk.open_offer(oid, code)
    return code


async def pair_node(c: Coordinator, tmp: Path, code: str, *, node: str = "node1"):
    return await pair(f"127.0.0.1:{c.port}", code, tmp / node, name=node)


def events(c: Coordinator, type: str | None = None):
    return c.log.events(type)


async def wait_event(c: Coordinator, type: str, timeout: float = 5.0):
    """The coordinator finishes its own bookkeeping after the node has already returned: wait for the event instead of racing it."""
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        found = events(c, type)
        if found:
            return found
        await asyncio.sleep(0.02)
    return []
