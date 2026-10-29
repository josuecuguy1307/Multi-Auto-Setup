"""The unknown ladder as tools, for USB-serial targets backed by replay devices.

  rung 1  try_known_skill        a saved skill for this signature (execution comes with the sandbox phase)
  rung 2  read_usb_descriptor    generic, harmless read
          read_serial_ident      listen, then try allowlisted identification queries
  rung 3  lookup_usb_ids         public-database guess  -> hypothesis
          match_serial_protocol  signature guess        -> hypothesis
          confirm_hypotheses     ONE harmless confirming read -> tested, or contradicted
  rung 6  record_unknown / ask_human are loop actions, not tools

Nothing here becomes `verified`: that needs the verifier, which is a later phase.
"""

from __future__ import annotations

import json
import re
from typing import Any

from envrec.adapters.base import Adapter, ToolContext
from envrec.contracts import (
    Coverage,
    CreatedBy,
    Evidence,
    Fact,
    FactStatus,
    Hypothesis,
    HypothesisOutcome,
    Origin,
    ProbeResult,
    Target,
    TargetKind,
    Tier,
    ToolSpec,
    Unknown,
)
from envrec.fingerprint import UsbIds, match_serial, serial_protocol_result, usb_lookup_result
from envrec.ladder.replay import DeviceRegistry, ReplayDevice
from envrec.store import Store

PROVIDER = "replay-device"
LIMITS = ["replay device: a recorded transcript, not real hardware"]
CONFIRM_SEND = {"scpi": "*IDN?\r\n", "at": "AT\r\n", "gcode": "$I\r\n"}  # nmea is confirmed by listening
_GENERIC = {"inc", "ltd", "corp", "co", "gmbh", "llc", "electronics", "technology", "technologies",
            "devices", "international", "future", "systems", "the"}


def _spec(name: str, rung: int, line: str, produces: list[str], requires: list[str] | None = None) -> ToolSpec:
    return ToolSpec(name=name, ring=1, tier=Tier.read, target_kinds=[TargetKind.usb], one_line=line,
                    origin=Origin.builtin, rung=rung, source="ladder", produces=produces,
                    requires=requires or [])


_USB_IDS = ["usb.vendor_id", "usb.product_id"]
_SPECS = [
    _spec("try_known_skill", 1, "Check whether a saved skill already matches this device.", ["skill.match"], _USB_IDS),
    _spec("read_usb_descriptor", 2, "Read the device's USB descriptor: vendor id, product id, name.",
          ["usb.vendor_id", "usb.product_id", "usb.manufacturer"]),
    _spec("read_serial_ident", 2, "Listen on the serial port and try safe identification queries.",
          ["serial.ident_reply"]),
    _spec("lookup_usb_ids", 3, "Look up the vendor and product ids in the public usb.ids list.",
          ["usb.vendor_name", "usb.product_name"], _USB_IDS),
    _spec("match_serial_protocol", 3, "Match the serial replies against known protocol signatures.",
          ["serial.protocol"], ["serial.ident_reply"]),
    _spec("confirm_hypotheses", 3, "Run one harmless confirming read for each open guess.", []),
]


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) >= 3 and t not in _GENERIC}


class LadderAdapter(Adapter):
    name = "ladder"

    def __init__(self, devices: DeviceRegistry, usb_ids: UsbIds | None = None, runner=None) -> None:
        self.devices, self.usb_ids, self.runner = devices, usb_ids, runner

    def tool_specs(self) -> list[ToolSpec]:
        return list(_SPECS)

    def available(self, tool: str, target: Target, store: Store) -> bool:
        if tool != "confirm_hypotheses":
            return True
        return bool(self._untried(store, target))

    @staticmethod
    def _untried(store: Store, target: Target) -> list[Hypothesis]:
        """Pending guesses that have a confirming read and have not had their one attempt yet."""
        return [h for h in store.hypotheses(target.id)
                if h.outcome == HypothesisOutcome.pending and h.test and h.attempts == 0]

    async def run(self, tool: str, args: dict[str, Any], ctx: ToolContext) -> ProbeResult:
        handler = getattr(self, f"_{tool}", None)
        if handler is None or tool not in {s.name for s in _SPECS}:
            return ProbeResult(ok=False, error=f"ladder adapter has no tool {tool!r}")
        device = self.devices.get(ctx.target.handle)
        if device is None:
            return ProbeResult(ok=False, error=f"{ctx.target.handle!r} is not a replay device; nothing was touched")
        return handler(ctx, device)

    # ---- helpers ---------------------------------------------------------------------------------

    @staticmethod
    def _evidence(ctx: ToolContext, tool: str, raw_text: str, confidence: float = 0.9) -> Evidence:
        return Evidence(provider=PROVIDER, probe=tool, event_id=ctx.event_id,
                        raw_ref=ctx.store.put_raw(raw_text), confidence=confidence, limitations=LIMITS)

    @staticmethod
    def _unknown(ctx: ToolContext, key: str) -> Fact:
        return Fact(target_id=ctx.target.id, key=key, created_by=CreatedBy.probe)

    @staticmethod
    def _result(facts: list[Fact], unknowns: list[Unknown], scope: str) -> ProbeResult:
        return ProbeResult(ok=True, facts=facts, unknowns=unknowns, coverage=Coverage(
            scope=scope, status="complete" if not unknowns else "partial",
            checked=[f.key for f in facts if f.status != FactStatus.unknown], unknown=[u.key for u in unknowns]))

    def _fact(self, ctx: ToolContext, tool: str, key: str, value: Any, raw_text: str) -> Fact:
        return Fact(target_id=ctx.target.id, key=key, value=value, status=FactStatus.tested,
                    evidence=[self._evidence(ctx, tool, raw_text)], created_by=CreatedBy.probe)

    def _current(self, ctx: ToolContext, key: str) -> Fact | None:
        return ctx.store.current_fact(ctx.target.id, key)

    # ---- rung 1 ------------------------------------------------------------------------------------

    def _try_known_skill(self, ctx: ToolContext, device: ReplayDevice) -> ProbeResult:
        sig = ctx.target.signature
        skill = ctx.store.find_skill(sig) if sig else None
        if skill is None:
            # A negative lookup is a measured answer, not an unknown about the world: nothing to ask a human.
            fact = self._fact(ctx, "try_known_skill", "skill.match", False, f"no saved skill for signature {sig}")
            fact = fact.model_copy(update={"evidence": [fact.evidence[0].model_copy(update={
                "provider": "skill-store", "limitations": ["no skills exist yet: the skill library arrives with the verifier"]})]})
            return self._result([fact], [], "try_known_skill")
        if self.runner is not None:
            return self._run_skill(ctx, device, skill, sig)
        fact = self._fact(ctx, "try_known_skill", "skill.match", skill.id, f"skill {skill.id} matches {sig}")
        fact = fact.model_copy(update={"evidence": [fact.evidence[0].model_copy(update={
            "provider": "skill-store", "limitations": ["a saved skill matches; running it needs the sandbox phase"]})]})
        return self._result([fact], [], "try_known_skill")

    def _run_skill(self, ctx: ToolContext, device: ReplayDevice, skill, sig: str) -> ProbeResult:
        """Rung 1 with a saved skill. On a device it was not verified on, its tests must pass there first."""
        from envrec.ladder.generate import read_skill_code
        from envrec.skills.reverify import VERIFIED_HERE, reverify_skill, skill_status_here
        here = skill_status_here(ctx.store, skill, ctx.target.id)
        newly = False
        if here != VERIFIED_HERE:
            rv = reverify_skill(ctx.store, skill, ctx.target.id, list(device.stream), self.runner)
            if not rv.passed:  # a negative result, measured: the skill does not carry over to this device
                fact = self._fact(ctx, "try_known_skill", "skill.match", False, f"skill {skill.id} unverified here: {rv.reason}")
                fact = fact.model_copy(update={"evidence": [fact.evidence[0].model_copy(update={
                    "provider": "skill-store", "limitations": [f"saved skill is unverified_here: {rv.reason}"[:200]]})]})
                return self._result([fact], [], "try_known_skill")
            newly = True
        res = self.runner.run(read_skill_code(ctx.store, skill), device.stream[0] if device.stream else "")
        if not res.ok:
            return ProbeResult(ok=False, error=f"saved skill {skill.id} failed on this device: {res.error}"[:300])
        raw = ctx.store.put_raw(res.stdout)
        facts = [self._fact(ctx, "try_known_skill", "skill.match", skill.id, f"skill {skill.id} matches {sig}")]
        status = FactStatus.verified if (newly or here == VERIFIED_HERE) else FactStatus.tested
        for f in res.facts:
            ev = [Evidence(provider=f"skill:{skill.id}", probe="try_known_skill", event_id=ctx.event_id, raw_ref=raw,
                           confidence=0.85, limitations=["saved skill re-run on this device"])]
            if status == FactStatus.verified:
                ev.append(Evidence(provider="skill-reverify", probe="stored tests on this device", event_id=ctx.event_id,
                                   raw_ref=raw, confidence=0.9, limitations=["verified against this device's recorded data only"]))
            facts.append(Fact(target_id=ctx.target.id, key=f["key"], value=f["value"], status=status, evidence=ev,
                              created_by=CreatedBy.verifier if status == FactStatus.verified else CreatedBy.probe))
        ctx.store.put_skill(skill.model_copy(update={"verified_runs": skill.verified_runs + 1}))
        return self._result(facts, [], "try_known_skill")

    # ---- rung 2 ------------------------------------------------------------------------------------

    def _read_usb_descriptor(self, ctx: ToolContext, device: ReplayDevice) -> ProbeResult:
        d = device.descriptor
        raw = json.dumps(d, sort_keys=True)
        facts = [self._fact(ctx, "read_usb_descriptor", "usb.vendor_id", d["vendor_id"], raw),
                 self._fact(ctx, "read_usb_descriptor", "usb.product_id", d["product_id"], raw)]
        unknowns: list[Unknown] = []
        if d.get("manufacturer"):
            facts.append(self._fact(ctx, "read_usb_descriptor", "usb.manufacturer", d["manufacturer"], raw))
        else:
            facts.append(self._unknown(ctx, "usb.manufacturer"))
            unknowns.append(Unknown(key="usb.manufacturer", question="the device reports no manufacturer string"))
        sig = f"usb:{d['vendor_id']}:{d['product_id']}"
        ctx.store.put_target(ctx.target.model_copy(update={"signature": sig}))
        return self._result(facts, unknowns, "read_usb_descriptor")

    def _read_serial_ident(self, ctx: ToolContext, device: ReplayDevice) -> ProbeResult:
        exchanges = [{"sent": "", "recv": device.passive_read()}]
        for cmd in ("*IDN?\r\n", "AT\r\n"):  # only when listening alone did not identify the protocol
            if any(m.confidence >= 0.9 for e in exchanges for m in match_serial(e["recv"], e["sent"].strip() or None)):
                break
            exchanges.append({"sent": cmd, "recv": device.send(cmd)})
        raw = json.dumps(exchanges, indent=2)
        total = sum(len(e["recv"]) for e in exchanges)
        if total == 0:
            return self._result([self._unknown(ctx, "serial.ident_reply")],
                                [Unknown(key="serial.ident_reply", question="the device sent nothing")], "read_serial_ident")
        fact = self._fact(ctx, "read_serial_ident", "serial.ident_reply",
                          {"exchanges": len(exchanges), "bytes": total}, raw)
        return self._result([fact], [], "read_serial_ident")

    # ---- rung 3 ------------------------------------------------------------------------------------

    def _lookup_usb_ids(self, ctx: ToolContext, device: ReplayDevice) -> ProbeResult:
        if self.usb_ids is None:
            return ProbeResult(ok=False, error="usb.ids is not loaded (run scripts/fetch_data.py)")
        vid, pid = self._current(ctx, "usb.vendor_id"), self._current(ctx, "usb.product_id")
        result = usb_lookup_result(ctx.store, ctx.target, self.usb_ids, str(vid.value), str(pid.value), ctx.event_id)
        for fact in result.facts:
            if fact.status == FactStatus.hypothesis:
                label = "vendor" if fact.key == "usb.vendor_name" else "product"
                ctx.store.put_hypothesis(Hypothesis(
                    target_id=ctx.target.id, claim=f"the usb {label} is {fact.value}", rung=3,
                    based_on=[vid.id, pid.id], source="usb.ids", fact_id=fact.id,
                    test="usb_manufacturer_string" if label == "vendor" else "",
                    expected=str(fact.value)))
        return result

    def _match_serial_protocol(self, ctx: ToolContext, device: ReplayDevice) -> ProbeResult:
        ident = self._current(ctx, "serial.ident_reply")
        exchanges = json.loads(ctx.store.read_raw(ident.evidence[0].raw_ref))
        best = max(((m, e) for e in exchanges for m in match_serial(e["recv"], e["sent"].strip() or None)),
                   key=lambda pair: pair[0].confidence, default=None)
        if best is None:
            return serial_protocol_result(ctx.store, ctx.target, "", None, ctx.event_id)
        match, exch = best
        result = serial_protocol_result(ctx.store, ctx.target, exch["recv"], exch["sent"].strip() or None, ctx.event_id)
        fact = result.facts[0]
        if fact.status == FactStatus.hypothesis:
            ctx.store.put_hypothesis(Hypothesis(
                target_id=ctx.target.id, claim=f"the serial protocol is {fact.value}", rung=3,
                based_on=[ident.id], source="serial.signatures", fact_id=fact.id,
                test=f"serial_confirm:{fact.value}", expected=str(fact.value)))
        return result

    def _confirm_hypotheses(self, ctx: ToolContext, device: ReplayDevice) -> ProbeResult:
        facts: list[Fact] = []
        notes: list[Unknown] = []
        for hyp in self._untried(ctx.store, ctx.target):
            ctx.store.put_hypothesis(hyp.model_copy(update={"attempts": 1}))  # one attempt, whatever happens
            if hyp.test.startswith("serial_confirm:"):
                fact = self._confirm_serial(ctx, device, hyp, hyp.test.split(":", 1)[1])
            elif hyp.test == "usb_manufacturer_string":
                fact = self._confirm_usb_vendor(ctx, device, hyp)
            else:
                continue  # no safe confirming read exists for this guess; it stays a hypothesis
            if fact is not None:
                facts.append(fact)
            else:
                about = ctx.store.get_fact(hyp.fact_id) if hyp.fact_id else None
                notes.append(Unknown(key=about.key if about else "serial.protocol",
                                     question=f"could not confirm: {hyp.claim}"[:200]))
        return self._result(facts, notes, "confirm_hypotheses")

    def _confirm_serial(self, ctx: ToolContext, device: ReplayDevice, hyp: Hypothesis, proto: str) -> Fact | None:
        if proto in CONFIRM_SEND:
            cmd = CONFIRM_SEND[proto]
            reply, sent = device.send(cmd), cmd.strip()
        elif proto == "nmea":
            reply, sent = device.passive_read(), None
        else:
            return None  # e.g. modbus: confirming needs a device address we do not have
        if not reply:
            return None  # silence proves nothing either way: the guess stays pending
        matches = match_serial(reply, sent)
        seen = matches[0].protocol if matches else "unrecognized"
        ctx.store.resolve_hypothesis(
            hyp.id, HypothesisOutcome.confirmed if seen == proto else HypothesisOutcome.contradicted)
        return self._fact(ctx, "confirm_hypotheses", "serial.protocol", seen,
                          json.dumps({"sent": sent or "(listened)", "recv": reply}))

    def _confirm_usb_vendor(self, ctx: ToolContext, device: ReplayDevice, hyp: Hypothesis) -> Fact | None:
        manufacturer = device.descriptor.get("manufacturer")
        if not manufacturer or not (_tokens(manufacturer) & _tokens(hyp.expected)):
            return None  # no agreement is not disagreement: leave the guess pending
        ctx.store.resolve_hypothesis(hyp.id, HypothesisOutcome.confirmed)
        return self._fact(ctx, "confirm_hypotheses", "usb.vendor_name", hyp.expected,
                          json.dumps({"descriptor_manufacturer": manufacturer}))
