"""Three stand-in environments through the whole admission path: proposal, human-gesture approval, the exam in the REAL sandbox (a Linux container), the stored fact, the options relabel and
the stale label after a model change. SIMULATED-LOCAL: no real model and no router dispatch (that is the final integrated test)."""
from __future__ import annotations

import pytest

from envrec.skills import sbx
from envrec_coordinator import options as O
from envrec_coordinator.admission.decision import AdmissionDesk
from envrec_coordinator.admission.exam import Reply
from envrec_coordinator.admission.facts import AdmissionFacts, Combo, allows
from envrec_coordinator.admission.suite import load_suite
from envrec_net.log import EventLog

pytestmark = pytest.mark.skipif(not sbx.docker_ready(), reason="Docker (or the sandbox image) is not ready")


class StandIn:
    def __init__(self, suite, wrong=(), garbage=False):
        self.ref, self.wrong, self.garbage = {t.id: t.reference for t in suite.tasks}, set(wrong), garbage

    def ask(self, prompt, *, task_id, timeout_s):
        if self.garbage or task_id in self.wrong:
            return Reply("```python\ndef nothing():\n    pass\n```", 0.5)
        return Reply("```python\n" + self.ref[task_id] + "\n```", 2.0)


def combo(model, sha):
    return Combo("code", "m1", "llamacpp", model, "local", sha, "b1")


def test_nine_of_ten_then_an_early_failure_then_a_model_change_that_goes_stale(tmp_path):
    log = EventLog(tmp_path / "state")
    sent = []
    desk = AdmissionDesk(AdmissionFacts(log, clock=lambda: 5.0), outbound=lambda i: sent.append(i) or i)
    facts, suite = desk.facts, load_suite("code")
    good, bad = combo("good", "aa" * 32), combo("bad", "bb" * 32)

    for c, env in ((good, StandIn(suite, wrong={"code-03-intervals"})), (bad, StandIn(suite, garbage=True))):
        p = desk.propose(c, suite, tokens_per_s=30.0)
        assert "10 code tasks" in p["text"]
        assert desk.answer(p["id"], "approve", gesture="panel")
        desk.start(c, suite, env)

    f = facts.current(good.key)
    assert (f["passed"], f["of"], f["label"], f["early_stop"]) == (9, 10, "measured here: 9/10", False)
    g = facts.current(bad.key)
    assert (g["ran"], g["early_stop"], g["stop_reason"], g["outcome"]) == (3, True, "first_3", "not_admitted")
    assert facts.view(good, suite, 8).admitted and allows("confidential", facts.view(good, suite, 8)) == (True, "measured_here")

    opts = {"o-good": facts.view(good, suite, 8), "o-bad": facts.view(bad, suite, 8)}
    kept = O.relabel_measured([{"id": "o-good", "cost_class": "free_local", "kind": "local", "origin": "catalog_runtime", "title": "a", "params_b": 8.0}], lambda o: opts[o["id"]])
    assert kept[0]["admission_label"] == "measured here: 9/10" and kept[0]["measured"]["pass_rate"] == 0.9

    swapped = combo("good", "cc" * 32)  # the model file under the same name changed
    v = facts.view(swapped, suite, 8)
    assert v.state == "stale" and not v.admitted and v.label == "stale: 9/10 was measured with another model file; measure again"
    assert allows("confidential", v) == (False, "provisional_only")
    assert desk.propose(swapped, suite)["id"] != desk.propose(good, suite)["id"]  # a new model needs its own owner decision

    tags = {e.effective_tag for e in log.events()}
    assert tags == {"MODEL", "GUARD", "OWNER"} and sum(1 for e in log.events() if e.effective_tag in ("SCRIPT", "CLAUDE-CODE")) == 0
    log.close()
