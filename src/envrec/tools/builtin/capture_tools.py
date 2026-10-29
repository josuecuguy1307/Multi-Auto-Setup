"""Tools that work on something already collected. They take a REFERENCE (a capture id the view lists), never the content: code reads the capture from the store, the model
only chooses which one. Raw output still never enters a prompt (T6)."""

from __future__ import annotations

import json

from envrec.discovery.types import Capture, Reason
from envrec.tools.ctx import ToolContext, ToolResult
from envrec.tools.registry import CaptureRef, tool


def _said_and_sent(text: str) -> tuple[str, str | None]:
    """What the device said, and the fixed query that was sent just before it, from either shape of serial capture: the plain `heard` text or the JSON log of the exchange."""
    try:
        log = json.loads(text)
    except ValueError:
        return text, None
    if not isinstance(log, dict):
        return text, None
    said = log.get("passive") or log.get("reply") or ""
    sent_name = (log.get("sent") or [None])[-1]
    sent = None
    if sent_name:
        from envrec.tools.builtin.hardware import serial_plan
        sent = next((x["send"].strip() for x in serial_plan().get("probes", []) if x["name"] == sent_name), None)
    return said, sent


@tool(tags=("table_lookup", "identify"), produces=("serial.protocol",))
def identify_serial_capture(ctx: ToolContext, capture: CaptureRef) -> ToolResult:
    """Match a stored serial capture against the known protocol signatures.

    Args:
        capture: the id of a stored capture of what a serial device said (the `heard` or `probe` capture of a serial exchange), from the CAPTURES list.
    Returns:
        Fact serial.protocol: a hypothesis naming the best-matching signature, or unknown when none matched; a capture `match` holds the lines that matched.
    Tier: observe
    Platforms: any
    Notes: signatures cover NMEA, SCPI, AT, G-code and Modbus; a match is a hypothesis; code reads the capture, the model never sees it.
    """
    text = ctx.ref_text("capture")
    if text is None:
        return ToolResult.fail(Reason.not_applicable, "the capture could not be read: choose an id from the CAPTURES list")
    said, sent = _said_and_sent(text)
    from envrec.fingerprint import match_serial
    ms = match_serial(said, sent) if said.strip() else []
    evidence = json.dumps([{"protocol": m.protocol, "confidence": m.confidence, "line": m.evidence} for m in ms[:5]]).encode()
    cap = Capture("match", "serial signatures", evidence, 0, "local", ctx.platform)
    if not ms:
        reason = "the capture holds nothing the device said" if not said.strip() else "no known protocol signature matched what the device said"
        return ToolResult(captures=[cap], facts={"serial.protocol": None}, reasons={"serial.protocol": reason}, provider="stdlib",
                          limitations=["a signature table cannot name a protocol it does not list"])
    best = ms[0]
    return ToolResult(captures=[cap], facts={"serial.protocol": best.protocol}, provider="stdlib",
                      limitations=["a signature match is a guess about the protocol, not a confirmed identification", *([f"also matched: {[m.protocol for m in ms[1:]]}"] if len(ms) > 1 else [])])
