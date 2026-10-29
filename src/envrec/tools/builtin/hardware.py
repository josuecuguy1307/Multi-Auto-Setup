"""Hands for attached hardware that need more than running a command: a passive Bluetooth LE listen and an exclusive serial identification exchange.

These tools collect; they never interpret. What the bytes mean is the job of a skill's script, which reads the capture."""

from __future__ import annotations

import re

import yaml

from envrec.discovery.commands import data_dir
from envrec.discovery.types import Capture, Reason
from envrec.tools.ctx import ToolContext, ToolResult
from envrec.tools.registry import tool


def ble_collect(seconds: float, salt: str) -> Capture:
    """Passive advertisement scan in a separate process (an OS permission abort must not take the harness down). Records carry only salted hashes."""
    import importlib.util
    import subprocess
    import sys
    from pathlib import Path
    if importlib.util.find_spec("bleak") is None:
        return Capture("scan", "bleak", b"", None, "local", "", Reason.unavailable, "bleak is not installed (pip install 'envrec[discovery]')")
    bt_settings = ("macOS stopped the Bluetooth scan. Open System Settings > Privacy & Security > Bluetooth and allow the program that runs this "
                   "(Terminal or iTerm). If it is not listed there, run `envrec discover` from Terminal.app, which can ask for the permission. Then run it again.")
    try:
        done = subprocess.run([sys.executable, "-I", "-m", "envrec.discovery._ble_scan", str(seconds), salt], capture_output=True, timeout=seconds + 25,
                              env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).resolve().parents[3])}, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return Capture("scan", "bleak", b"", None, "local", "", Reason.timeout, "the scan did not finish")
    if done.returncode != 0:
        err = done.stderr.decode("utf-8", "replace")
        if done.returncode in (-6, -11, 134, 139) or re.search(r"authori[sz]|permission|denied|not allowed|TCC|privacy", err, re.I):
            return Capture("scan", "bleak", b"", done.returncode, "local", "", Reason.permission, bt_settings)
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
        c0 = ble_collect(secs, ctx.params.get("salt") or ctx.salts.run)
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
        A capture named probe: a JSON log of the port, the baud rate, what was heard unprompted, what was sent and the reply.
    Tier: read
    Platforms: macos, linux
    Notes: exclusive lock; only the fixed queries in the table are ever written; one approval per port.
    """
    plan = serial_plan()
    port = port or ctx.params.get("port") or ("/dev/replay" if ctx.collector.origin == "replay" else "")
    if not port:
        return ToolResult.fail(Reason.not_applicable, "no port given")
    return ToolResult.from_capture(ctx.collector.serial_exchange("probe", str(port), plan))
