"""The phone tool: read-only facts of an enrolled phone over its cable, asking the human every time.

Everything it may read is the fixed table in the connect-ios skill (loaded by envrec.remote.ios). The device and the route come from targets.yaml (the entry's ios_usb block); the model
chooses only which of four aspects to read. Nothing here pairs, retries around a Trust refusal, or reads messages, photos, contacts or app data."""

from __future__ import annotations

from envrec.discovery.types import Capture, Reason, short_hash
from envrec.remote import ios
from envrec.tools.ctx import ToolContext, ToolResult
from envrec.tools.registry import tool


def _approval(ctx: ToolContext, aspect: str = "identity") -> str:
    return ios.plan_text(aspect) if aspect in ios.ASPECTS else "read one fixed aspect of the attached phone over the cable (read-only)"


def _number(text: str):
    try:
        return int(text)
    except ValueError:
        return None


def _value(how: str, raw: str, salt):
    """The fact value for one raw lockdown value, by the conversion the table names for its fact."""
    if how == "hash":
        return short_hash(raw, salt=salt) if raw else None
    if how == "gb":
        n = _number(raw)
        return round(n / 1e9, 1) if n is not None else None
    if how == "int":
        return _number(raw)
    if how == "bool":
        return {"true": True, "false": False}.get(raw.lower())
    return raw or None


@tool(tags=("ios", "active", "generic_read"), kinds=("phone",), approval=_approval)
def ios_info(ctx: ToolContext, aspect: str) -> ToolResult:
    """Read one fixed aspect of the enrolled phone over its USB cable.

    Args:
        aspect: one of identity (model, iOS version, device class), battery, storage (totals only) or name (kept as a hash).
    Returns:
        Fact keys ios.product_type, ios.version, ios.device_class, ios.battery_pct, ios.battery_charging, ios.storage_total_gb, ios.storage_data_total_gb, ios.name_sha, and a capture with the lines read.
    Tier: read
    Platforms: macos, linux
    Notes: read-only; asks every time; the phone may ask its owner to tap Trust; never reads messages, photos, contacts or app data; the identifier is never stored.
    """
    runner, tid = ctx.params.get("ios_runner"), ctx.params.get("ios_target")
    if runner is None or not tid:
        return ToolResult.fail(Reason.not_applicable, "no enrolled phone is set for this run")
    if aspect not in ios.ASPECTS:
        return ToolResult.fail(Reason.not_applicable, f"aspect must be one of {', '.join(ios.ASPECTS)}")
    res = runner.ios_read(tid, aspect)
    if not res.ok:
        cat, sentence = ios.classify(res.exit_code, res.stdout, res.stderr)
        reason = Reason.permission if cat in ("trust_pending", "trust_denied", "locked", "not_trusted") else Reason.unavailable
        return ToolResult.fail(reason, f"{cat}: {sentence}")
    facts: dict = {}
    shown = []
    read = dict(line.split("=", 1) for line in res.stdout.splitlines() if "=" in line)
    for fact, _domain, key in ios.ASPECTS[aspect]:
        raw = read.get(key, "")
        facts[fact] = _value(ios.CONVERT[fact], raw, ctx.salts.stable)
        shown.append(f"{key}=<hashed>" if key in ios.HIDDEN_VALUE else f"{key}={raw}")
    cap = Capture(f"ios_{aspect}", " ".join(res.argv_shown[-1:]) or "ideviceinfo", "\n".join(shown).encode(), 0, "remote", ctx.platform)
    return ToolResult(captures=[cap], facts=facts, provider="native", limitations=["read over the cable after the owner's Trust tap; values as the phone reports them"])
