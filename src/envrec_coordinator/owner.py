"""What the owner sees and answers about pairing, in ONE place for every front end. The CLI (`mas pair`, through control files) and the local web screen are two ways to read this state and
to give the answer; neither holds protocol logic. The protocol itself (the code, the key exchange, the single attempt, the timeouts) is `PairingDesk` + `protocol-pair`; this class only
holds the owner's view of it and the owner's pending yes or no.

It lives on the coordinator's event loop and is touched only from there."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from envrec_coordinator.pairing import Decision

TERMINAL = ("accepted", "refused", "failed", "expired", "rejected")
CANDIDATE_FIELDS = ("name", "os", "arch", "fingerprint")


class OwnerDesk:
    def __init__(self, clock: Callable[[], float]) -> None:
        self.clock = clock
        self._current: dict | None = None
        self._pending: dict[str, asyncio.Future[Decision]] = {}

    # ---- the protocol tells the desk what is happening ---------------------------------------------------------------------------------------

    def opened(self, offer_id: str, code: str, expires_at: float, via: str, target: str | None = None) -> None:
        self._current = {"offer": offer_id, "status": "waiting", "code": code, "expires_at": expires_at, "reason": "", "candidate": None, "via": via, "target": target}

    def exchanging(self, offer_id: str) -> None:
        cur = self._of(offer_id)
        if cur and cur["status"] == "waiting":
            cur.update(status="exchange", code=None)  # the code is used up: it leaves memory and the screen

    def outcome(self, offer_id: str, status: str, reason: str) -> None:
        cur = self._of(offer_id)
        if cur:
            cur.update(status=status, reason=reason, code=None)

    async def ask(self, summary: dict) -> Decision:
        """The OWNER is asked about one candidate. Resolves when a front end calls `decide`."""
        oid = summary["offer"]
        fut: asyncio.Future[Decision] = asyncio.get_running_loop().create_future()
        self._pending[oid] = fut
        cur = self._of(oid)
        if cur:
            cur.update(status="confirm", code=None, candidate={k: str(summary.get(k, "")) for k in CANDIDATE_FIELDS})
        try:
            return await fut
        finally:
            self._pending.pop(oid, None)

    # ---- a front end gives the owner's answer --------------------------------------------------------------------------------------------------

    def decide(self, offer_id: str, accept: bool, via: str) -> bool:
        """True when this answer was taken. False when there is nothing to decide (already answered, expired, or an id nobody asked about): the first answer wins."""
        fut = self._pending.get(offer_id)
        if fut is None or fut.done():
            return False
        cur = self._of(offer_id)
        if cur:
            cur["status"] = "deciding"
        fut.set_result(Decision(bool(accept), via))
        return True

    # ---- what a front end shows -----------------------------------------------------------------------------------------------------------------

    def view(self) -> dict | None:
        cur = self._current
        if cur is None:
            return None
        out = {k: cur[k] for k in ("offer", "status", "expires_at", "reason", "candidate", "target")}
        if cur["status"] == "waiting":
            out["code"] = cur["code"]  # only while it can still be used
        return out

    def _of(self, offer_id: str) -> dict | None:
        cur = self._current
        return cur if cur is not None and cur["offer"] == offer_id else None
