"""ROUTER (R1-R3) and its import rule (R4): checks that DRIVE the real router over scenarios and report every violation. Each check also runs against a router with the guard taken out (a fixture that
must fail), which is what keeps a passing check from being vacuous. Nothing here touches a network or starts a program."""
from __future__ import annotations

import ast
import itertools
from pathlib import Path

from envrec_coordinator.router import DictFacts, EntryChat, MachineFacts, Policy, Refusal, RouteDecision, Router, Task
from envrec_net import envrun as E

ENVS = E.load_environments()
FORBIDDEN_IMPORTS = ("envrec.model", "envrec.loop", "envrec.agent", "envrec.protocols", "envrec_coordinator.agent", "envrec_coordinator.setup_agent", "envrec_coordinator.runs")


def _machine(mid: str, *, paired: bool = True, local: bool = True) -> MachineFacts:
    return MachineFacts(machine_id=mid, os_family="darwin", paired=paired, installed={"claude-code", "codex", "muse-code", "opencode", "pi"}, signed_in={"claude-code:plan", "codex:plan", "muse-code:plan"},
                        env_names={"META_API_KEY"}, sandbox_backends=["docker"], model_endpoint="127.0.0.1:8080" if local else None, model_id="qwen3-8b")


def _policy(**kw) -> Policy:
    p = Policy.defaults()
    p.open_subtypes = {"local_model", "own_harness", "provider", "embedded"}
    p.automated_use_cleared = {"claude-code", "codex", "muse-code"}
    p.approved_providers_for_internal = {"claude-code", "codex", "muse-code"}
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def _admission(machines: list[MachineFacts], everything: bool) -> dict:
    out = {}
    for m in machines:
        for e in ENVS.values():
            for tt in e["task_types"]:
                for acc in e["accesses"]:
                    model = (m.model_id or "unknown") if e["subtype"] in ("local_model", "own_harness") else "default"
                    out[f"admission.{tt}.{m.machine_id}.{e['id']}.{model}.{acc['id']}"] = {"status": "tested", "graded_by": "code", "passed": 10 if (everything or e["subtype"] != "provider") else 3, "of": 10, "identity": {}}
    return out


def _task(tid: str, ttype: str, sens: str, chat: str) -> Task:
    return Task(task_id=tid, task_type=ttype, sensitivity=sens, entry_chat=EntryChat(kind=chat, name="x"), created_by="OWNER", payload_ref="r")


def _router(machines, adm, **pol) -> Router:
    return Router(envs=ENVS, facts=DictFacts(machines, admission=adm), policy=_policy(**pol))


def r1_violations(make=_router) -> list[str]:
    """A confidential task is never routed to a cloud environment, a commercial harness, an embedded one, or a machine that is not one of the owner's paired computers; and a confidential task asked
    from a cloud chat is never created. Driven over: with and without a third-party machine that has everything, with and without measurements for every combination."""
    out = []
    for stranger, measured, ttype, local in itertools.product((False, True), (False, True), ("code", "text"), (True, False)):  # `local` False: only providers are installed, the case where cost alone would pick one
        machines = [_machine("own", local=local)] + ([_machine("stranger", paired=False)] if stranger else [])
        r = make(machines, _admission(machines, everything=measured))
        d = r.route(_task(f"c{stranger}{measured}{ttype}{local}", ttype, "confidential", "local"))
        if isinstance(d, RouteDecision):
            env = ENVS[d.route.environment_id]
            if env["cloud"] or env["subtype"] in ("provider", "embedded") or d.route.machine_id != "own":
                out.append(f"confidential {ttype} routed to {d.route.key} (stranger={stranger}, measured={measured}, local={local})")
        cloud = r.route(_task(f"cc{stranger}{measured}{ttype}{local}", ttype, "confidential", "cloud"))
        if isinstance(cloud, RouteDecision):
            out.append(f"confidential {ttype} created from a cloud chat was routed to {cloud.route.key}")
    return out


def r2_violations(make=_router, *, failures: int = 8) -> list[str]:
    """However many times the code grader fails, the escalation step never passes 2, and the task ends with escalation_exhausted."""
    machines = [_machine("own")]
    r = make(machines, _admission(machines, everything=True))
    d = r.route(_task("e1", "code", "public", "local"))
    steps = [d.escalation_step] if isinstance(d, RouteDecision) else []
    last = d
    for _ in range(failures):
        if not isinstance(last, RouteDecision):
            break
        try:
            last = r.report_result("e1", last.escalation_step, {"passed": False, "grader": "code", "evidence_ref": "g"})
        except ValueError:  # the type of RouteDecision itself refuses a step beyond 2: a router that tried is still a violation
            return [f"the router tried escalation step {len(steps)}, beyond the cap"]
        if isinstance(last, RouteDecision):
            steps.append(last.escalation_step)
    out = [f"escalation reached step {max(steps)}" for _ in [0] if steps and max(steps) > 2]
    if not (isinstance(last, Refusal) and last.code == "escalation_exhausted"):
        out.append("the task did not end with escalation_exhausted")
    return out


def r3_violations(make=_router) -> list[str]:
    """A combination that is not measured here (provisional) never receives a confidential task, even when it is the only candidate."""
    out = []
    for ttype in ("code", "text"):
        machines = [_machine("own")]
        r = make(machines, {})
        d = r.route(_task(f"p{ttype}", ttype, "confidential", "local"))
        if isinstance(d, RouteDecision):
            out.append(f"unmeasured combination {d.route.key} received a confidential {ttype} task")
    return out


def router_import_violations(root: Path) -> list[str]:
    """R4: the router never asks a model. Nothing under the router package imports a module that drives a model or an agent loop."""
    out = []
    for f in sorted(Path(root).rglob("*.py")):
        for n in ast.walk(ast.parse(f.read_text(encoding="utf-8"), filename=str(f))):
            names = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""] if isinstance(n, ast.ImportFrom) else []
            out += [f"{f.name}:{n.lineno}: imports {x}" for x in names if any(x == b or x.startswith(b + ".") for b in FORBIDDEN_IMPORTS)]
    return out


# ---- API additions of 2026-10-31 (docs/ROUTER_API.md sections 10-12) -------------------------------------------------------------------------------------------------------------------

def exclusion_violations(make=_router) -> list[str]:
    """X1: a task's `exclude` is a hard filter. Whatever the rule would prefer, a route never uses an excluded machine or environment, at the first decision and at every escalation; when nothing is left the answer is a
    refusal, never a relaxed choice; and a confidential task still never reaches a cloud environment because its local candidates were excluded."""
    from envrec_coordinator.router import Exclude
    out = []
    machines = [_machine("a"), _machine("b")]
    adm = _admission(machines, everything=True)
    for sens, excl in itertools.product(("public", "confidential"), (Exclude(machine_ids=["a"]), Exclude(environment_ids=["llamacpp-local", "opencode"]), Exclude(machine_ids=["a", "b"]))):
        r = make(machines, adm)
        t = _task(f"x{sens}{len(excl.machine_ids)}{len(excl.environment_ids)}", "code", sens, "local").model_copy(update={"exclude": excl})
        d = t
        for step in range(4):
            d = r.route(t) if step == 0 else r.report_result(t.task_id, d.escalation_step, {"passed": False, "grader": "code", "evidence_ref": "g"})
            if not isinstance(d, RouteDecision):
                break
            if d.route.machine_id in excl.machine_ids or d.route.environment_id in excl.environment_ids:
                out.append(f"{sens} task routed to {d.route.key} despite exclude {excl.model_dump()} at step {d.escalation_step}")
            if sens == "confidential" and ENVS[d.route.environment_id]["cloud"]:
                out.append(f"confidential task reached a cloud environment after exclusions: {d.route.key}")
        if len(excl.machine_ids) == 2 and isinstance(d, RouteDecision):
            out.append("everything was excluded and the router still routed")
    return out


def inheritance_violations(make=_router) -> list[str]:
    """I1: a task takes the highest level among itself and its `result-of:` inputs. A verification declared public over the output of a confidential task is routed as confidential: never to a cloud environment, and
    the router does not relax it. Driven with every environment measured, so only the level can keep the verifier off the providers."""
    out = []
    machines = [_machine("own")]
    adm = _admission(machines, everything=True)
    r = make(machines, adm)
    src = r.route(_task("src", "text", "confidential", "local"))
    if not isinstance(src, RouteDecision):
        return ["setup: the confidential source task was not routed"]
    ver = _task("ver", "text", "public", "local").model_copy(update={"payload_ref": "result-of:src"})
    d = r.route(ver)
    if isinstance(d, RouteDecision):
        if ENVS[d.route.environment_id]["cloud"]:
            out.append(f"a public verification over a confidential result was routed to {d.route.key}")
        if d.effective_sensitivity != "confidential":
            out.append(f"the verification was routed under {d.effective_sensitivity}, not confidential")
    cloudv = r.route(_task("ver2", "text", "public", "cloud").model_copy(update={"payload_ref": "result-of:src"}))
    if isinstance(cloudv, RouteDecision):
        out.append("a verification of a confidential result asked from a cloud chat was routed")
    unknown = r.route(_task("ver3", "text", "public", "local").model_copy(update={"payload_ref": "result-of:never-seen"}))
    if isinstance(unknown, RouteDecision) and (ENVS[unknown.route.environment_id]["cloud"] or unknown.effective_sensitivity != "confidential"):
        out.append("an input of unknown source was not treated as confidential")
    return out


def grading_violations(make=_router) -> list[str]:
    """G1: a grader that executes model-written code never reaches its runner unless a sandbox backend is ready; a model- or person-graded kind never passes."""
    from envrec_coordinator.router.outputs import MemoryOutputs
    out = []
    calls = []

    def runner(rec, spec):
        calls.append(spec)
        return {"passed": True, "log_sha256": "0" * 64}
    machines = [_machine("own")]
    r = make(machines, {})
    r.sandbox_ready, r.sandbox_runner = (lambda: False), runner
    r.outputs = outputs = MemoryOutputs()
    ref = outputs.put("g1", "public", {"output_text": "print(1)"})
    r.set_grader("g1", {"kind": "run_tests", "value": "x"})
    res = r.grade("g1", ref)
    if calls or not (isinstance(res, Refusal) and res.code == "sandbox_missing"):
        out.append("model-written code was handed to the runner with no sandbox ready")
    for kind in ("model", "human"):
        r.set_grader("g1", {"kind": kind, "value": ""})
        res = r.grade("g1", ref)
        if not (isinstance(res, Refusal) and res.code == "weak_grader"):
            out.append(f"a {kind}-graded result was not refused as weak")
    return out
