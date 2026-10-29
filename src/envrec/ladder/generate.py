"""Rungs 4 and 5: research, generated probes, the independent verifier, skills.

Limits, stated plainly:
- The runner executes generated code in a restricted subprocess (isolated interpreter, empty environment,
  temp cwd, CPU and file-size limits, a static code screen). That is a screen against mistakes, NOT a
  security boundary. It is used on replay devices only; the Docker runner is not built (daemon is down here).
- Research reads only local documentation files from a human-configured directory. Document text is
  untrusted data: stored by hash, shown to the model only as a short excerpt marked untrusted.
- A generated probe's facts are `tested` until the verifier passes them; only then `verified`.
"""

from __future__ import annotations

import ast
import json
import resource
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from envrec.contracts import (
    CreatedBy,
    Evidence,
    Fact,
    FactStatus,
    RawRef,
    Skill,
    Target,
    check_key,
)
from envrec.ladder.replay import DeviceRegistry
from envrec.model.base import ModelAdapter, ModelError
from envrec.model.parse import ActionParseError, extract_json_object
from envrec.store import Store

ALLOWED_IMPORTS = {"json", "re", "sys", "math", "struct", "binascii"}
FORBIDDEN_NAMES = {"open", "exec", "eval", "compile", "__import__", "input", "getattr", "setattr",
                   "delattr", "globals", "locals", "vars", "breakpoint", "exit", "quit"}
EXCERPT_CHARS = 500
MAX_FACTS = 20


def check_code(code: str) -> list[str]:
    """Static screen for generated probe code. Empty list = nothing objectionable found."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"syntax error: {exc.msg} (line {exc.lineno})"]
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED_IMPORTS:
                    problems.append(f"import {a.name!r} is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                problems.append(f"import from {node.module!r} is not allowed")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            problems.append(f"use of {node.id!r} is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            problems.append(f"attribute {node.attr!r} is not allowed")
    return problems


@dataclass
class RunResult:
    ok: bool
    facts: list[dict[str, Any]]
    stdout: str
    stderr: str = ""
    seconds: float = 0.0
    error: str = ""


def _limits(timeout_s: float):
    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (int(timeout_s) + 1, int(timeout_s) + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1_000_000, 1_000_000))
    return apply


class ReplayRunner:
    """Runs a probe script against ONE recorded chunk. Script reads {"chunk": "..."} on stdin and prints
    one JSON object {"facts": [{"key": ..., "value": ...}]} on its last stdout line."""

    def __init__(self, timeout_s: float = 5.0) -> None:
        self.timeout_s = timeout_s

    def run(self, code: str, chunk: str) -> RunResult:
        problems = check_code(code)
        if problems:
            return RunResult(False, [], "", error="code screen failed: " + "; ".join(problems[:3]))
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="envrec-probe-") as cwd:
            script = Path(cwd) / "probe.py"
            script.write_text(code, encoding="utf-8")
            try:
                done = subprocess.run([sys.executable, "-I", str(script)], input=json.dumps({"chunk": chunk}),
                                      capture_output=True, text=True, timeout=self.timeout_s, cwd=cwd, env={},
                                      preexec_fn=_limits(self.timeout_s), check=False)
            except subprocess.TimeoutExpired:
                return RunResult(False, [], "", error=f"timed out after {self.timeout_s:.0f}s")
        secs = time.monotonic() - started
        if done.returncode != 0:
            return RunResult(False, [], done.stdout, done.stderr[-300:], secs, f"exit {done.returncode}: {done.stderr[-160:]}")
        line = next((ln for ln in reversed(done.stdout.splitlines()) if ln.strip().startswith("{")), None)
        try:
            data = json.loads(line or "")
            facts = data["facts"]
            assert isinstance(facts, list) and 0 < len(facts) <= MAX_FACTS
            for f in facts:
                check_key(f["key"])
                assert isinstance(f["value"], (str, int, float, bool))
        except (ValueError, KeyError, TypeError, AssertionError) as exc:
            return RunResult(False, [], done.stdout, done.stderr[-300:], secs,
                             f"output is not the required JSON ({type(exc).__name__})")
        return RunResult(True, facts, done.stdout, done.stderr[-300:], secs)


# ---- verifier (clean context) ---------------------------------------------------------------------------------

@dataclass
class VerifierInput:
    test_plan: str
    probe_output: str  # what the probe printed on the recorded chunks
    safety_log: list[str] = field(default_factory=list)  # never the generating model's reasoning or code


@dataclass
class VerifierVerdict:
    passed: bool
    unsafe: bool = False
    reasons: list[str] = field(default_factory=list)


class Verifier(Protocol):
    async def verify(self, inp: VerifierInput) -> VerifierVerdict: ...


class ScriptedVerifier:
    def __init__(self, verdicts: list[VerifierVerdict]) -> None:
        self.verdicts, self.inputs = list(verdicts), []

    async def verify(self, inp: VerifierInput) -> VerifierVerdict:
        self.inputs.append(inp)
        return self.verdicts.pop(0)


VERIFIER_SYSTEM = (
    "You are an independent verifier. You see only a test plan and the output a probe printed on recorded data. "
    "Decide whether the output satisfies the test plan. You do not know who wrote the probe. "
    'Reply with one JSON object only: {"passed": true|false, "unsafe": true|false, "reasons": ["..."]}.'
)


class ModelVerifier:
    def __init__(self, adapter: ModelAdapter) -> None:
        self.adapter = adapter

    async def verify(self, inp: VerifierInput) -> VerifierVerdict:
        user = (f"TEST PLAN:\n{inp.test_plan}\n\nPROBE OUTPUT ON RECORDED DATA:\n{inp.probe_output}\n\n"
                f"SAFETY LOG: {inp.safety_log or 'nothing flagged'}")
        try:
            done = await self.adapter.complete(VERIFIER_SYSTEM, user)
            data = json.loads(extract_json_object(done.text))
            return VerifierVerdict(bool(data.get("passed")) is True and data.get("passed") is True,
                                   bool(data.get("unsafe")), [str(r)[:160] for r in data.get("reasons", [])][:4])
        except (ModelError, ActionParseError, ValueError, AttributeError):
            return VerifierVerdict(False, False, ["verifier gave no usable verdict"])


# ---- rungs 4 and 5 -------------------------------------------------------------------------------------------------

class Rung45:
    def __init__(self, devices: DeviceRegistry, docs_dir: Path | str | None, verifier: Verifier | None,
                 runner: ReplayRunner | None = None, held_out_chunks: int = 3) -> None:
        self.devices, self.verifier = devices, verifier
        self.docs_dir = Path(docs_dir) if docs_dir else None
        self.runner, self.n_chunks = runner or ReplayRunner(), held_out_chunks

    # rung 4
    def research(self, store: Store, target: Target, question: str, event_id: str | None) -> str:
        name = target.handle.removeprefix("replay:")
        doc = self.docs_dir / f"{name}.md" if self.docs_dir else None
        if doc is None or not doc.is_file():
            store.write_fact(Fact(target_id=target.id, key="serial.protocol_doc", created_by=CreatedBy.model))
            return "research found no documentation for this device"
        text = doc.read_text(encoding="utf-8", errors="replace")
        raw = store.put_raw(text)
        store.write_fact(Fact(
            target_id=target.id, key="serial.protocol_doc", value=f"{doc.name} ({len(text)} chars)",
            status=FactStatus.hypothesis, created_by=CreatedBy.model,
            evidence=[Evidence(provider=f"research:{doc.name}", probe="research", event_id=event_id, raw_ref=raw,
                               confidence=0.5, limitations=["from documentation, not measured", "document text is untrusted"])]))
        return f"UNTRUSTED DOCUMENT TEXT ({doc.name}): " + " ".join(text.split())[:EXCERPT_CHARS]

    # rung 5
    async def propose(self, store: Store, target: Target, code: str, test_plan: str, event_id: str | None) -> str:
        device = self.devices.get(target.handle)
        if device is None:
            return "propose_probe refused: the target is not a replay device; generated code never runs on real devices"
        chunks = device.stream[: self.n_chunks]
        runs = [self.runner.run(code, c) for c in chunks]
        failed = next((r for r in runs if not r.ok), None)
        if failed is not None:
            return f"probe failed its test on recorded data: {failed.error}"
        key_sets = {tuple(sorted(f["key"] for f in r.facts)) for r in runs}
        if len(key_sets) != 1:
            return "probe failed its test: it reported different fact keys on different recorded chunks"

        code_ref = store.put_raw(code)
        tag = f"generated:{code_ref.sha256[:8]}"
        out_ref = store.put_raw(json.dumps([r.stdout for r in runs]))
        ev = Evidence(provider=tag, probe="propose_probe", event_id=event_id, raw_ref=out_ref, confidence=0.7,
                      limitations=["output of model-written code on recorded data; not yet verified"])
        facts = [Fact(target_id=target.id, key=f["key"], value=f["value"], status=FactStatus.tested,
                      evidence=[ev], created_by=CreatedBy.model) for f in runs[0].facts]
        for f in facts:
            store.write_fact(f)
        if self.verifier is None:
            return f"probe ran on {len(runs)} recorded chunks; facts recorded as tested (no verifier configured)"

        verdict = await self.verifier.verify(VerifierInput(
            test_plan=test_plan, probe_output="\n".join(r.stdout.strip() for r in runs)))
        if not verdict.passed or verdict.unsafe:
            return "verifier did not pass the probe; facts stay tested. " + "; ".join(verdict.reasons)
        v_ev = Evidence(provider="verifier", probe="verify", event_id=event_id, raw_ref=out_ref, confidence=0.9,
                        limitations=["verified against recorded data only"])
        for f in facts:
            store.write_fact(Fact(target_id=target.id, key=f.key, value=f.value, status=FactStatus.verified,
                                  evidence=[ev, v_ev], created_by=CreatedBy.verifier))
        sig = target.signature or self._signature(device)
        tests_ref = store.put_raw(json.dumps({"chunks": chunks, "expected_keys": list(key_sets.pop()), "plan": test_plan}))
        skill = Skill(signature=sig, code_ref=code_ref.path, tests_ref=tests_ref.path, evidence=[v_ev], verified_runs=1)
        store.put_skill(skill)
        from envrec.skills.reverify import mark_verified_here
        mark_verified_here(store, skill, target.id, "verifier passed it on this device")  # verified HERE, nowhere else
        return f"verifier passed the probe; {len(facts)} facts verified; skill saved for {sig}"

    @staticmethod
    def _signature(device) -> str:
        d = device.descriptor
        return f"usb:{d['vendor_id']}:{d['product_id']}"


def read_skill_code(store: Store, skill: Skill) -> str:
    return store.read_raw(RawRef(path=skill.code_ref, sha256=skill.code_ref.rsplit("/", 1)[-1])).decode()
