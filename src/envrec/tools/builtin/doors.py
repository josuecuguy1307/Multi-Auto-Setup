"""LEGACY (retired by docs/NETWORK_CONTRACT.md 2.5 and section 1.5; kept until Phase 1 replaces it, NOT reachable from the `mas` commands; conformance and tests/test_mas_legacy.py check that): The computer route's three gestures as tools: make a key for this computer, serve the one-time setup link, pair with the 6 digits the owner reads on the other computer.

The code that grants access is fixed and reviewed (envrec.doors); the model only chooses when each step happens. The link and the digits are shown to, and typed by, the owner through
`ctx.params["show"]` and `ctx.params["ask_code"]`: neither the link (a bearer secret for five minutes) nor the digits ever enter a fact, an event or a prompt.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from envrec.contracts import Actor, Event
from envrec.discovery.types import Reason
from envrec.doors import pin, ssh_keys
from envrec.doors.pin import PairingRefused
from envrec.doors.setup_server import SetupLink
from envrec.doors.windows_door import render_door
from envrec.tools.ctx import ToolContext, ToolResult
from envrec.store.store import append_event_from_thread
from envrec.tools.registry import tool


def _target(ctx: ToolContext) -> str:
    """The name of the key pair: the computer is not enrolled yet, so the pair is named for this door run."""
    return str(ctx.params.get("door_name") or "lan-peer")


def _ssh_dir(ctx: ToolContext) -> Path:
    return Path(ctx.params.get("ssh_dir") or ssh_keys.SSH_DIR)


def _key_text(ctx: ToolContext) -> str:
    return "make this computer's own key for the other computer: " + " ".join(ssh_keys.keygen_command(_target(ctx), _ssh_dir(ctx))) + "   (an ed25519 pair, mode 600, no passphrase; only the PUBLIC half is ever shown or sent)"


def _key_undo(ctx: ToolContext) -> dict:
    return {"tool": "ssh_key_remove", "args": {}, "text": "delete the two key files this step made"}


@tool(tags=("connect", "door", "active"), kinds=("door",), ring=1, approval=_key_text, undo=_key_undo, produces=("door.key_ready",), timeout_s=60)
def ssh_key_create(ctx: ToolContext) -> ToolResult:
    """Make this computer's SSH key for the enrolled computer.

    Args:
        none
    Returns:
        Fact door.key_ready; the public half is kept for the setup file, the private half is never read back.
    Tier: write
    Platforms: macos, linux
    Notes: never overwrites an existing key; the private file stays under ~/.ssh with mode 600 and targets.yaml names only the environment variable that points to it.
    """
    try:
        pub = ssh_keys.create_keypair(_target(ctx), _ssh_dir(ctx))
    except FileExistsError as exc:
        return ToolResult(facts={"door.key_ready": True}, provider="stdlib", limitations=[f"{exc}; the existing key is used"])
    except (RuntimeError, ValueError, OSError) as exc:
        return ToolResult.fail(Reason.unavailable, str(exc)[:160])
    ctx.params.setdefault("run_state", {}).setdefault("door", {})["public_key"] = pub
    return ToolResult(facts={"door.key_ready": True}, provider="stdlib")


@tool(tags=("connect", "door", "active"), kinds=("door",), ring=1, audience="undo", approval=lambda ctx: "delete the key files made for the enrolled computer",
      undo=lambda ctx: {"tool": "ssh_key_remove", "args": {}, "text": "nothing further"})
def ssh_key_remove(ctx: ToolContext) -> ToolResult:
    """Delete the key files this run made.

    Args:
        none
    Returns:
        Fact door.key_ready false.
    Tier: write
    Platforms: macos, linux
    Notes: the undo of ssh_key_create; run only by the undo executor.
    """
    path = ssh_keys.key_path(_target(ctx), _ssh_dir(ctx))
    for f in (path, path.with_name(path.name + ".pub")):
        f.unlink(missing_ok=True)
    return ToolResult(facts={"door.key_ready": False}, provider="stdlib")


def _serve_text(ctx: ToolContext) -> str:
    return (f"serve ONE file, the setup file for the other computer, on this computer's private network address {ctx.params.get('lan_address', '(its private address)')} for 5 minutes under a random "
            "token; it contains only this computer's PUBLIC key and address, and it asks the person at the other computer for administrator permission itself. After it runs it reports back "
            "the other computer's user name, computer name, Windows version and SSH host key. The link will be shown to you here, not stored")


@tool(tags=("connect", "door", "active"), kinds=("door",), ring=1, approval=_serve_text, undo=lambda ctx: {"tool": "ssh_door_stop", "args": {}, "text": "stop serving the file"},
      produces=("door.link_open",), timeout_s=60)
def ssh_door_serve(ctx: ToolContext) -> ToolResult:
    """Serve the one-time setup file to the other computer.

    Args:
        none
    Returns:
        Fact door.link_open; the link itself is shown to the owner only (as text and as a QR code).
    Tier: write
    Platforms: macos, linux
    Notes: works once, expires in 5 minutes, answers only clients of this computer's own private network, and accepts one report back from the machine that downloaded it.
    """
    state = ctx.params.setdefault("run_state", {}).setdefault("door", {})
    address = ctx.params.get("lan_address")
    if not address:
        return ToolResult.fail(Reason.unavailable, "this computer has no private network address to serve on")
    pub = state.get("public_key")
    if pub is None:
        path = ssh_keys.key_path(_target(ctx), _ssh_dir(ctx))
        pub_file = path.with_name(path.name + ".pub")
        if not pub_file.exists():
            return ToolResult.fail(Reason.not_applicable, "make the key first (ssh_key_create)")
        pub = pub_file.read_text().strip()
    extra = ctx.params.get("emit") or (lambda kind, info: None)  # the caller may also show the events (the mission runner prints them)
    store_root = getattr(ctx.store, "root", None)
    lost = state.setdefault("lost_events", [])

    def emit(kind: str, info: dict) -> None:
        """Runs on the link's server threads, at the moment something happens, tool running or not (the link may expire while nothing runs). It writes the event itself, through
        its own connection to the store, because a store belongs to the thread that opened it."""
        if store_root is not None:
            try:
                append_event_from_thread(store_root, Event(actor=Actor.loop, type=kind, payload=dict(info)))
            except (sqlite3.Error, OSError) as exc:
                lost.append(f"{kind}: {type(exc).__name__}")  # never silent: the run's summary can report what could not be written
        extra(kind, info)

    try:
        link = SetupLink(address, lambda report_url: render_door(address, pub, report_url).encode(), emit=emit, **ctx.params.get("link_options", {}))
        url = link.start()
    except (ValueError, OSError) as exc:
        return ToolResult.fail(Reason.unavailable, str(exc)[:160])
    state["link"] = link
    show = ctx.params.get("show")
    if show is not None:
        show(f"On the OTHER computer, open this link within 5 minutes (or scan the code), run the file, click Yes at the administrator prompt, and read the 6 digits it prints:\n  {url}\n{qr_text(url)}")
    return ToolResult(facts={"door.link_open": True}, provider="stdlib")


def qr_text(url: str) -> str:
    """The link as a QR code drawn with text, when the optional `segno` package is there; otherwise nothing (the short link is still shown)."""
    try:
        import io

        import segno
        buf = io.StringIO()
        segno.make(url, error="l").terminal(out=buf, compact=True)
        return buf.getvalue()
    except Exception:
        return ""


@tool(tags=("connect", "door", "active"), kinds=("door",), ring=1, audience="undo", approval=lambda ctx: "stop serving the setup file", undo=lambda ctx: {"tool": "ssh_door_stop", "args": {}, "text": "nothing further"})
def ssh_door_stop(ctx: ToolContext) -> ToolResult:
    """Stop serving the setup file.

    Args:
        none
    Returns:
        Fact door.link_open false.
    Tier: write
    Platforms: macos, linux
    Notes: the undo of ssh_door_serve; run only by the undo executor.
    """
    link = ctx.params.get("run_state", {}).get("door", {}).get("link")
    if link is not None:
        link.stop()
    return ToolResult(facts={"door.link_open": False}, provider="stdlib")


def _wait_text(ctx: ToolContext) -> str:
    return "wait up to 5 minutes for the other computer to open the link and report back who it is (read-only: it only listens on the link this run is serving)"


@tool(tags=("connect", "door", "passive"), kinds=("door",), ring=1, produces=("door.peer_address", "door.peer_user", "door.peer_computer", "door.peer_os"), timeout_s=1000)
def ssh_door_wait(ctx: ToolContext) -> ToolResult:
    """Wait for the other computer to report who it is.

    Args:
        none
    Returns:
        Facts door.peer_address (the address of the connection that came in), door.peer_user, door.peer_computer and door.peer_os, as the setup file reported them.
    Tier: observe
    Platforms: macos, linux
    Notes: never scans or probes the network; if nothing arrives it ends with a typed reason: either the link was never opened (the two computers cannot reach each other) or it was opened but the file did not report.
    """
    link = ctx.params.get("run_state", {}).get("door", {}).get("link")
    if link is None:
        return ToolResult.fail(Reason.not_applicable, "serve the setup file first (ssh_door_serve)")
    import time
    remaining = max(1.0, link._deadline - time.monotonic() + 2)  # as long as the link lives: the run must not end, and take the server with it, before the owner could open it
    link.reported.wait(float(ctx.params.get("door_wait_s", remaining)))
    rep = link.report
    if rep is None:
        if link.fetched_by is None:
            return ToolResult.fail(Reason.unavailable, "NOT_REACHED: nobody opened the link; the other computer cannot reach this one (a shared network can isolate clients). Use a network where both "
                                   "can reach each other (a phone hotspot or a home router). Nothing on the network was scanned.")
        reached = ("; the file said it reached: " + ", ".join(link.steps[-6:])) if link.steps else "; it never said it reached any step, so it was probably not run, or it stopped at its first line"
        return ToolResult.fail(Reason.unavailable, ("NOT_REPORTED: the file was downloaded but never reported back" + reached + ". The administrator prompt may have been declined. Nothing was changed on this side.")[:290])
    return ToolResult(facts={"door.peer_address": rep["address"], "door.peer_user": rep["user"] or None, "door.peer_computer": rep["computer"] or None, "door.peer_os": rep["os"] or None},
                      provider="stdlib", limitations=["reported by the setup file the owner ran; the address is that of the connection that came in"])


def _pair_text(ctx: ToolContext) -> str:
    return "ask you for the 6 digits shown on the other computer, compare them with the code computed from the SSH host key this computer sees at the address that reported, and only if they match (and it is the key the file reported) remember that host key"


@tool(tags=("connect", "door", "active"), kinds=("door",), ring=1, approval=_pair_text, produces=("door.paired", "door.host_fingerprint"), timeout_s=420)
def ssh_door_pair(ctx: ToolContext) -> ToolResult:
    """Pair with the other computer using 6 digits the owner reads.

    Args:
        none
    Returns:
        Fact door.paired: true only when the digits match the host key this computer sees and the file reported; nothing is pinned otherwise.
    Tier: read
    Platforms: macos, linux
    Notes: the digits are typed by the owner and never stored; a mismatch aborts without pinning or connecting; the host is the address that reported, never one typed.
    """
    link = ctx.params.get("run_state", {}).get("door", {}).get("link")
    rep = link.report if link is not None else None
    ask = ctx.params.get("ask_code")
    if rep is None or ask is None:
        return ToolResult.fail(Reason.not_applicable, "the other computer has not reported yet (ssh_door_wait) or there is no way to ask the owner")
    code = ask("Type the 6 digits shown on the other computer: ")
    if not code:
        return ToolResult.fail(Reason.denied, "no digits were given")
    scan = ctx.params.get("scan") or pin.scan_host_key
    try:
        seen = scan(rep["address"], 22)
        if rep["host_key"] and " ".join(rep["host_key"].split()[:2]) != " ".join(seen.split()[:2]):
            return ToolResult(facts={"door.paired": False}, provider="stdlib", limitations=["the host key this computer sees is not the one the file reported: nothing was pinned"])
        pin.pin_if_code_matches(rep["address"], code, 22, known_hosts=Path(ctx.params.get("known_hosts") or pin.KNOWN_HOSTS), scan=lambda h, p: seen)
    except PairingRefused as exc:
        return ToolResult(facts={"door.paired": False}, provider="stdlib", limitations=[str(exc)[:160]])
    import base64
    import hashlib
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(base64.b64decode(seen.split()[1])).digest()).decode().rstrip("=")
    return ToolResult(facts={"door.paired": True, "door.host_fingerprint": fp}, provider="stdlib")


def _propose_text(ctx: ToolContext) -> str:
    return "write a PROPOSAL of the target (address, user, computer name, host key fingerprint, from what the other computer reported) to proposals/targets.proposed.yaml. It authorizes nothing: only your typed name and yes in enrollment writes targets.yaml"


@tool(tags=("connect", "door", "active"), kinds=("door",), ring=1, approval=_propose_text, undo=lambda ctx: {"tool": "door_unpropose", "args": {}, "text": "delete the proposal file"},
      produces=("door.proposed",), timeout_s=30)
def door_propose_target(ctx: ToolContext) -> ToolResult:
    """Write the proposal of the target the door found.

    Args:
        none
    Returns:
        Fact door.proposed; the proposal file lists the target disabled, for the owner to confirm in enrollment.
    Tier: write
    Platforms: macos, linux
    Notes: writes only proposals/targets.proposed.yaml; never touches targets.yaml.
    """
    known = ctx.known()
    need = ("door.peer_address", "door.peer_user", "door.peer_computer", "door.host_fingerprint")
    if not all(known.get(k) for k in need) or known.get("door.paired") is not True:
        return ToolResult.fail(Reason.not_applicable, "the other computer has not reported and been paired yet")
    from envrec.doors.proposal import write_target_proposal
    path = write_target_proposal(Path(ctx.params.get("proposals_dir") or "proposals"), address=known["door.peer_address"], user=known["door.peer_user"], computer=known["door.peer_computer"],
                                 os_text=known.get("door.peer_os") or "", fingerprint=known["door.host_fingerprint"])
    return ToolResult(facts={"door.proposed": True}, provider="stdlib", limitations=[f"written to {path.name}"])


@tool(tags=("connect", "door", "active"), kinds=("door",), ring=1, audience="undo", approval=lambda ctx: "delete the proposal file", undo=lambda ctx: {"tool": "door_unpropose", "args": {}, "text": "nothing further"})
def door_unpropose(ctx: ToolContext) -> ToolResult:
    """Delete the proposal file this run wrote.

    Args:
        none
    Returns:
        Fact door.proposed false.
    Tier: write
    Platforms: macos, linux
    Notes: the undo of door_propose_target; run only by the undo executor.
    """
    (Path(ctx.params.get("proposals_dir") or "proposals") / "targets.proposed.yaml").unlink(missing_ok=True)
    return ToolResult(facts={"door.proposed": False}, provider="stdlib")
