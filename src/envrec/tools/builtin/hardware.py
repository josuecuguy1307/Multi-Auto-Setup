"""Hands for attached hardware that need more than running a command: a passive Bluetooth LE listen and an exclusive serial identification exchange.

These tools collect; they never interpret. What the bytes mean is the job of a skill's script, which reads the capture."""

from __future__ import annotations

import json
import re

import yaml

from envrec.discovery.commands import data_dir
from envrec.discovery.types import Capture, Reason
from envrec.tools.ctx import ToolContext, ToolResult
from envrec.tools.registry import tool


def _helper_scan(seconds: float, salt: str, why: str, approve=None) -> Capture:
    """macOS blocked the scan or the process cannot ask for the permission: route it through envrec Helper, the harness's own app, which can."""
    from envrec import helper
    try:
        recs = helper.ble_records(seconds, salt, approve=approve)
    except helper.HelperError as exc:
        if exc.kind == "approval":
            return Capture("scan", "envrec Helper", b"", None, "local", "", Reason.denied, f"{why} The owner did not approve starting envrec Helper, so nothing was scanned.")
        if exc.kind == "missing":
            return Capture("scan", "envrec Helper", b"", None, "local", "", Reason.permission, f"{why} Run `{helper.FIX_BUILD}`: it builds the harness's own app, which macOS can ask for the permission. Then run this again.")
        if exc.kind == "permission":
            return Capture("scan", "envrec Helper", b"", None, "local", "", Reason.permission, f"{exc.detail}. Click Allow on the prompt from \"envrec Helper\" and run this again.")
        return Capture("scan", "envrec Helper", b"", None, "local", "", Reason.timeout if exc.kind == "timeout" else Reason.unavailable, f"helper: {exc.detail}"[:160])
    return Capture("scan", f"envrec Helper passive scan {seconds:g}s", json.dumps(recs).encode(), 0, "local", "")


def ble_collect(seconds: float, salt: str, approve=None) -> Capture:
    """Passive advertisement scan in a separate process (an OS permission abort must not take the harness down). Records carry only salted hashes.
    On macOS, when the direct scan is blocked or aborted for lack of a permission, the same scan goes through envrec Helper."""
    import importlib.util
    import subprocess
    import sys
    from pathlib import Path
    mac = sys.platform == "darwin"
    if importlib.util.find_spec("bleak") is None:
        if mac:
            return _helper_scan(seconds, salt, "bleak is not installed here.", approve)
        return Capture("scan", "bleak", b"", None, "local", "", Reason.unavailable, "bleak is not installed (pip install 'envrec[discovery]')")
    blocked = "macOS stopped the Bluetooth scan for this program."
    try:
        done = subprocess.run([sys.executable, "-I", "-m", "envrec.discovery._ble_scan", str(seconds), salt], capture_output=True, timeout=seconds + 25,
                              env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).resolve().parents[3])}, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return Capture("scan", "bleak", b"", None, "local", "", Reason.timeout, "the scan did not finish")
    if done.returncode != 0:
        err = done.stderr.decode("utf-8", "replace")
        if done.returncode in (-6, -11, 134, 139) or re.search(r"authori[sz]|permission|denied|not allowed|TCC|privacy", err, re.I):
            if mac:
                return _helper_scan(seconds, salt, blocked, approve)
            return Capture("scan", "bleak", b"", done.returncode, "local", "", Reason.permission, "the scan was blocked by the operating system: allow Bluetooth for the program that runs this")
        return Capture("scan", "bleak", b"", done.returncode, "local", "", Reason.unavailable, err.strip().splitlines()[-1][:160] if err.strip() else f"exit {done.returncode}")
    return Capture("scan", f"bleak passive scan {seconds:g}s", done.stdout, 0, "local", "")


@tool(tags=("hardware", "bluetooth", "ambient", "passive", "generic_read"))
def ble_scan_passive(ctx: ToolContext, seconds: float = 8.0) -> ToolResult:
    """Listen for nearby Bluetooth LE advertisers and capture what they say.

    Args:
        seconds: how long to listen.
    Returns:
        A capture named scan: one JSON record per advertiser with a salted hash, company ids, service UUIDs and a signal strength.
    Tier: observe
    Platforms: macos, linux
    Notes: passive only: no connection, no pairing; identifiers are salted-hashed with a per-run salt inside the scanning process and advertised names are dropped.
    """
    secs = float(ctx.params.get("seconds", seconds))
    if ctx.collector.origin == "replay":
        c = ctx.collector.read_file("scan", "scan")
    elif ctx.collector.origin != "local":
        return ToolResult.fail(Reason.not_applicable, "BLE scanning is local to this machine")
    else:
        c0 = ble_collect(secs, ctx.params.get("salt") or ctx.salts.run, approve=ctx.recording_approve("launch_helper"))  # starting envrec Helper is an effect: the owner's answer is an event
        c = Capture(c0.name, c0.source, c0.data, c0.exit_code, c0.origin, ctx.platform, c0.error, c0.detail)
    return ToolResult.from_capture(c)


def serial_plan() -> dict:
    """The fixed table of what may be listened for and sent: the only source of bytes a serial probe ever writes."""
    return yaml.safe_load((data_dir() / "serial_probes.yaml").read_text(encoding="utf-8")) or {}


def _serial_approval(ctx: ToolContext, port: str = "") -> str:
    p = serial_plan()
    sends = ", ".join(repr(x["send"]) for x in p.get("probes", []))
    return (f"open {port} exclusively, listen {p.get('listen_s')} s at each of {p.get('bauds')} baud, then send only {sends} "
            f"(waiting {p.get('reply_wait_s')} s for each reply), and stop at the first reply")


@tool(tags=("hardware", "serial", "active", "generic_read"), approval=_serial_approval)
def serial_identify(ctx: ToolContext, port: str = "") -> ToolResult:
    """Open one serial port, send fixed identify queries, keep the first reply.

    Args:
        port: device path of the serial port to ask.
    Returns:
        Two captures: probe (a JSON log of the port, the baud rate, what was heard unprompted, what was sent and the reply) and heard (only what the device said, as plain text).
    Tier: read
    Platforms: macos, linux
    Notes: exclusive lock; only the fixed queries in the table are ever written; one approval per port.
    """
    plan = serial_plan()
    port = port or ctx.params.get("port") or ("/dev/replay" if ctx.collector.origin == "replay" else "")
    if not port:
        return ToolResult.fail(Reason.not_applicable, "no port given")
    c = ctx.collector.serial_exchange("probe", str(port), plan)
    if not c.ok:
        return ToolResult.from_capture(c)
    try:
        log = json.loads(c.text)
        said = (log.get("passive") or log.get("reply") or "") if isinstance(log, dict) else ""
    except ValueError:
        said = c.text
    # `probe` is the log of the exchange; `heard` is only what the device said, as plain text: what a parser of the device's own format wants to read
    return ToolResult(captures=[c, Capture("heard", c.source, said.encode("utf-8", "replace"), 0, c.origin, c.platform)])
