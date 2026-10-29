"""A guard for protocols that CODE walks (pairing, node connect): the same enforced state machine the loop uses (`envrec.protocols`, defined by a SKILL.md folder), driven here
without a model. The code produces the evidence (a fact, recorded as such); the guard moves only when the protocol's rule for that move is met, refuses and logs every other move, and
ends a step that outlives its timeout. Code never skips a state: the protocol says what has to be true to leave each one."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from envrec.contracts import FactStatus
from envrec.protocols.machine import EvidenceView, ProtocolRun
from envrec.skills.skillmd import load_skill
from envrec_os import SKILLS_DIR

Log = Callable[[str, dict], None]


class IllegalTransition(Exception):
    pass


@dataclass
class _Fact:
    status: FactStatus
    value: Any


class ProtocolGuard:
    def __init__(self, skill_name: str, log: Log, *, clock: Callable[[], float] = time.time, skills_dir: Path | None = None, subject: str = "") -> None:
        self.run = ProtocolRun(load_skill((skills_dir or SKILLS_DIR) / skill_name))
        self.log, self.clock, self.subject = log, clock, subject
        self.facts: dict[str, _Fact] = {}
        self.entered_at = clock()
        self.log("protocol_start", {"protocol": self.run.id, "state": self.run.state, "subject": subject})

    @property
    def state(self) -> str:
        return self.run.state

    @property
    def terminal(self) -> bool:
        return self.run.terminal

    @property
    def history(self) -> list[dict]:
        return self.run.history

    def record(self, key: str, value: Any) -> None:
        """Evidence the code produced (a message arrived, a confirmation held, the owner answered). It moves nothing by itself."""
        self.facts[key] = _Fact(FactStatus.tested, value)

    def _view(self) -> EvidenceView:
        return EvidenceView(facts=self.facts)  # type: ignore[arg-type]

    def advance(self, to: str) -> None:
        """Move to `to` if the protocol allows that move from here with the evidence recorded; otherwise refuse, log the refusal and raise."""
        if self.terminal:
            self._refuse(to, [f"the protocol has ended in {self.state}"])
        check = self.run.check(to, self._view())
        if not check.ok:
            self._refuse(to, check.missing)
        frm = self.state
        self.run.move(to, "evidence")
        self.entered_at = self.clock()
        self.log("protocol_state", {"protocol": self.run.id, "from": frm, "to": to, "by": "evidence", "subject": self.subject})

    def _refuse(self, to: str, missing: list[str]) -> None:
        msg = self.run.refuse(to, missing)
        self.log("protocol_refused", {"protocol": self.run.id, "state": self.state, "to": to, "missing": [m[:120] for m in missing], "subject": self.subject})
        raise IllegalTransition(msg)

    def time_out(self, reason: str) -> bool:
        """The step ran out of time by a clock the caller keeps (an awaited answer that never came): move to the state the protocol names for that."""
        st = self.run.current
        if self.terminal or st.on_timeout is None:
            return False
        frm = self.state
        self.run.move(st.on_timeout, "timeout")
        self.entered_at = self.clock()
        self.log("protocol_state", {"protocol": self.run.id, "from": frm, "to": self.state, "by": "evidence", "reason": reason[:120], "subject": self.subject})
        return True

    def expire_if_due(self) -> bool:
        """If this step has a timeout and it has passed, move to the state the protocol names for that. True when it moved."""
        st = self.run.current
        if self.terminal or st.timeout_s is None or self.clock() - self.entered_at < st.timeout_s:
            return False
        frm = self.state
        self.run.move(st.on_timeout, "timeout")  # type: ignore[arg-type]
        self.entered_at = self.clock()
        self.log("protocol_state", {"protocol": self.run.id, "from": frm, "to": self.state, "by": "evidence", "reason": f"no progress for {st.timeout_s:g} s", "subject": self.subject})
        return True
