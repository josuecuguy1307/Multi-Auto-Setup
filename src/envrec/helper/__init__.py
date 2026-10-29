"""envrec-helper.app: the harness's OWN macOS identity for privacy prompts, built locally from the Swift source in this package.

Why it exists: macOS attributes a Bluetooth / Local Network / Location request to the app that is "responsible" for the process. A program started from another app (a terminal,
an editor, an agent) that has no usage text in its Info.plist is simply aborted. The helper is a small app with those usage texts, started with `open`, so the prompt reads
"envrec Helper wants to use Bluetooth" whoever called it.

What it may do is fixed in two places that must agree: the Swift gate (WHITELIST and WHITELIST_TEXT in main.swift) and `WHITELIST` / `TEXT_WHITELIST` here. Seven requests; numeric
arguments in a range, text arguments of one fixed shape (a salted hash, a salt, an enumerated word); no command, no path. It observes, with three exceptions that need a fresh owner
approval on this side: `connect_paired` (connect or disconnect a device macOS ALREADY has paired, named by a salted hash; it never pairs a new one and writes nothing to it),
`audio_set` (make one of the Mac's own output devices the default output) and `audio_get` (read them). It talks over a unix socket (mode 0600, same user) in a private directory, with a per-run nonce.
Output is raw observations as JSON; the harness keeps its own privacy gate, and `ble_records` hashes ambient ids with the run's salt before anything is stored.
"""

from __future__ import annotations

import json
import os
import plistlib
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

APP_NAME = "envrec-helper.app"
BUNDLE_ID = "com.envrec.helper"
DISPLAY_NAME = "envrec Helper"
SOURCE = Path(__file__).parent / "swift" / "main.swift"
FRAMEWORKS = ("CoreBluetooth", "IOBluetooth", "CoreWLAN", "CoreLocation", "Network", "AppKit")
BONJOUR = ["_airplay._tcp", "_raop._tcp", "_companion-link._tcp", "_apple-mobdev2._tcp", "_matter._tcp", "_spotify-connect._tcp", "_googlecast._tcp",
           "_hap._tcp", "_homekit._tcp", "_http._tcp", "_ipp._tcp", "_printer._tcp", "_smb._tcp", "_device-info._tcp", "_sleep-proxy._udp"]
USAGE = {
    "NSBluetoothAlwaysUsageDescription": "envrec Helper listens passively for nearby Bluetooth advertisements so the harness can tell which of your own devices are around; it never pairs or connects.",
    "NSLocalNetworkUsageDescription": "envrec Helper looks for services your own devices announce on your local network so the harness can recognize them; it sends nothing to them.",
    "NSLocationWhenInUseUsageDescription": "envrec Helper asks for Location only so macOS will tell it the name of the Wi-Fi network this Mac is on; it never reads your position.",
}
# request name -> {argument: (min, max)}; the same table is in main.swift
WHITELIST: dict[str, dict[str, tuple[float, float]]] = {"ble_scan": {"seconds": (1, 20)}, "ble_paired": {}, "wifi_info": {}, "mdns_browse": {"seconds": (1, 15)}}
# text arguments: request -> argument -> the only shape it may have; the same table is in main.swift
TEXT_WHITELIST: dict[str, dict[str, str]] = {
    "connect_paired": {"device": r"[0-9a-f]{8}", "salt": r"[0-9a-f]{16,64}", "action": r"connect|disconnect"},
    "audio_get": {"salt": r"[0-9a-f]{16,64}"},
    "audio_set": {"device": r"[0-9a-f]{8}", "salt": r"[0-9a-f]{16,64}"},
}
PERMISSION_WAIT_S = 90
FIX_BUILD = "envrec helper build"
FIX_XCODE = "xcode-select --install"


class HelperError(Exception):
    """kind: missing | refused | permission | approval | unavailable | timeout | no_start"""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail = kind, detail


def app_path() -> Path:
    base = os.environ.get("ENVREC_HELPER_DIR")
    return (Path(base).expanduser() if base else Path.home() / "Library" / "Application Support" / "envrec") / APP_NAME


Approve = Callable[[str], bool]


def approval_log_path() -> Path:
    return app_path().parent / "helper-approvals.jsonl"


def log_approval(entry: dict) -> None:
    """Every approval (and refusal) the helper asked for is written to a local log: what, the exact command, how to undo it or run it yourself, the answer."""
    path = approval_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}) + "\n")


def ask(approve: Approve | None, action: str, command: str, undo: str, why: str) -> bool:
    """Show the exact command and why, take the owner's answer, and record it. No approver means no."""
    text = f"{why}\n  command: {command}\n  undo / run it yourself: {undo}"
    granted = bool(approve is not None and approve(text))
    log_approval({"action": action, "command": command, "undo": undo, "granted": granted})
    return granted


def check_request(req: str, args: dict | None = None) -> dict:
    """The client's copy of the gate: refuse anything that is not a whitelisted request with in-range numeric arguments."""
    import re
    allowed, texts = WHITELIST.get(req), TEXT_WHITELIST.get(req)
    if allowed is None and texts is None:
        raise HelperError("refused", f"unknown request {req!r}; the helper answers only {', '.join([*WHITELIST, *TEXT_WHITELIST])}")
    clean: dict = {}
    for k, v in (args or {}).items():
        if texts is not None and k in texts:
            if not isinstance(v, str) or not re.fullmatch(texts[k], v):
                raise HelperError("refused", f"argument {k!r} is not allowed for {req}")
            clean[k] = v
            continue
        lo_hi = (allowed or {}).get(k)
        if lo_hi is None or isinstance(v, bool) or not isinstance(v, (int, float)) or not lo_hi[0] <= v <= lo_hi[1]:
            raise HelperError("refused", f"argument {k!r} is not allowed for {req}")
        clean[k] = v
    if texts is not None and set(clean) != set(texts):
        raise HelperError("refused", f"{req} needs exactly {sorted(texts)}")
    return clean


# ---- build / status / remove -----------------------------------------------------------------------------------------------------------------

def info_plist() -> dict:
    return {"CFBundleIdentifier": BUNDLE_ID, "CFBundleName": DISPLAY_NAME, "CFBundleDisplayName": DISPLAY_NAME, "CFBundleExecutable": "envrec-helper",
            "CFBundlePackageType": "APPL", "CFBundleVersion": "1", "CFBundleShortVersionString": "1.0", "LSMinimumSystemVersion": "12.0", "LSUIElement": True,
            "NSBonjourServices": list(BONJOUR), **USAGE}


RESET_COMMAND = f"tccutil reset All {BUNDLE_ID}"


def build(*, run: Callable = subprocess.run, which: Callable = shutil.which, dest: Path | None = None, approve: Approve | None = None) -> Path:
    """Compile the Swift source into the app, write its Info.plist, ad-hoc sign it. Raises HelperError('missing', fix) when swiftc is not installed: it never installs anything.
    Clearing macOS's saved privacy answers for this bundle id (`tccutil reset`) is an effect on the system: it runs only if `approve` says yes to the exact command; otherwise the
    app is still built and the command is left for the owner to run (`last_reset_skipped`)."""
    global last_reset_skipped
    swiftc = which("swiftc")
    if swiftc is None:
        raise HelperError("missing", f"swiftc is not installed. Install the Xcode command line tools with `{FIX_XCODE}` and then run `{FIX_BUILD}` again.")
    app = dest or app_path()
    with tempfile.TemporaryDirectory(prefix="envrec-helper-build-") as tmp:
        stage = Path(tmp) / APP_NAME
        (stage / "Contents" / "MacOS").mkdir(parents=True)
        (stage / "Contents" / "Info.plist").write_bytes(plistlib.dumps(info_plist()))
        cmd = [swiftc, "-O", "-swift-version", "5", str(SOURCE), "-o", str(stage / "Contents" / "MacOS" / "envrec-helper"), *[a for f in FRAMEWORKS for a in ("-framework", f)]]
        done = run(cmd, capture_output=True, text=True, timeout=600)
        if done.returncode != 0:
            raise HelperError("unavailable", "swiftc failed: " + (done.stderr or done.stdout).strip().splitlines()[-1][:200])
        signed = run(["codesign", "--force", "--sign", "-", "--identifier", BUNDLE_ID, str(stage)], capture_output=True, text=True, timeout=120)
        if signed.returncode != 0:
            raise HelperError("unavailable", "codesign failed: " + (signed.stderr or "").strip()[:200])
        # An ad-hoc signature changes with every build, so macOS's old entries for this bundle id no longer match the new code and it would not ask again. Clearing them (this id
        # only) needs the owner's yes: it cannot be undone (macOS asks again once), so the owner-runnable command is recorded instead of an undo.
        last_reset_skipped = not ask(approve, "tccutil_reset", RESET_COMMAND, f"cannot be undone; macOS will ask again once. To do it yourself: {RESET_COMMAND}",
                                     "macOS keeps its privacy answers for this app tied to the build's code hash, and a rebuild changes it, so macOS would not ask again")
        if not last_reset_skipped:
            run(["tccutil", "reset", "All", BUNDLE_ID], capture_output=True, text=True, timeout=30)
        if app.exists():
            shutil.rmtree(app)
        app.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(stage), str(app))
    return app


last_reset_skipped = False  # set by the last build(): True when the owner did not approve the privacy reset


def status(*, run: Callable = subprocess.run, which: Callable = shutil.which) -> dict:
    app = app_path()
    out = {"path": str(app), "built": (app / "Contents" / "MacOS" / "envrec-helper").is_file(), "swiftc": which("swiftc") is not None, "signed": False, "bundle_id": BUNDLE_ID}
    if out["built"]:
        out["signed"] = run(["codesign", "--verify", "--strict", str(app)], capture_output=True, timeout=30).returncode == 0
        out["running"] = run(["pgrep", "-f", str(app / "Contents" / "MacOS" / "envrec-helper")], capture_output=True, timeout=10).returncode == 0
    return out


def remove(*, run: Callable = subprocess.run) -> dict:
    """Delete the app and clear the privacy entries macOS holds for ITS OWN bundle id (only that id). Safe to repeat."""
    app = app_path()
    existed = app.exists()
    run(["pkill", "-f", str(app / "Contents" / "MacOS" / "envrec-helper")], capture_output=True, timeout=10)
    shutil.rmtree(app, ignore_errors=True)
    reset = run(["tccutil", "reset", "All", BUNDLE_ID], capture_output=True, text=True, timeout=30)
    return {"removed": existed, "path": str(app), "privacy_entries_cleared": reset.returncode == 0}


# ---- the client ------------------------------------------------------------------------------------------------------------------------------

def _launch(app: Path, rundir: str) -> None:
    subprocess.run(["open", "-n", "-a", str(app), "--args", rundir], check=True, capture_output=True, timeout=20)


def _roundtrip(sock_path: str, payload: bytes, timeout: float) -> bytes:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall(payload + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        return buf


class HelperClient:
    """One helper session: a private directory with the nonce, the app started with `open`, requests one at a time. Use as a context manager."""

    def __init__(self, app: Path | None = None, *, launch: Callable[[Path, str], None] = _launch, roundtrip: Callable[[str, bytes, float], bytes] = _roundtrip,
                 start_wait_s: float = 12.0, approve: Approve | None = None) -> None:
        self.app, self._launch, self._roundtrip, self.start_wait_s = app or app_path(), launch, roundtrip, start_wait_s
        self.approve = approve  # launching the app is an effect: without an approver (or a yes) it is refused
        self.rundir: str | None = None
        self.nonce = ""

    def __enter__(self) -> HelperClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self.rundir:
            shutil.rmtree(self.rundir, ignore_errors=True)
            self.rundir = None

    def _start(self) -> None:
        if self.rundir:
            return
        if not (self.app / "Contents" / "MacOS" / "envrec-helper").is_file():
            raise HelperError("missing", f"envrec Helper is not built. Run `{FIX_BUILD}` (it compiles locally; nothing is downloaded).")
        rundir = tempfile.mkdtemp(prefix="eh-")
        os.chmod(rundir, 0o700)
        self.nonce = secrets.token_hex(16)
        fd = os.open(os.path.join(rundir, "nonce"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(self.nonce)
        self.rundir = rundir
        exe = self.app / "Contents" / "MacOS" / "envrec-helper"
        if not ask(self.approve, "launch_helper", f"open -n -a {self.app} --args {rundir}", f"pkill -f {exe} (it also exits by itself after 20 s idle)",
                   "start envrec Helper, the app that asks macOS for Bluetooth, Local Network or Location on this program's behalf"):
            self.close()
            raise HelperError("approval", "starting envrec Helper needs the owner's approval and it was not given")
        try:
            self._launch(self.app, rundir)
        except (OSError, subprocess.SubprocessError) as exc:
            self.close()
            raise HelperError("no_start", f"could not open the helper ({type(exc).__name__})") from exc
        end = time.monotonic() + self.start_wait_s
        while not os.path.exists(os.path.join(rundir, "sock")):
            if time.monotonic() > end:
                self.close()
                raise HelperError("no_start", "the helper did not open its socket in time")
            time.sleep(0.1)

    def request(self, req: str, **args) -> object:
        """Send one whitelisted request and return the `data` of the answer. Raises HelperError with a kind the caller can branch on."""
        clean = check_request(req, args)  # refused here, before anything is launched
        self._start()
        timeout = float(clean.get("seconds", 5)) + PERMISSION_WAIT_S + 15
        try:
            raw = self._roundtrip(os.path.join(self.rundir, "sock"), json.dumps({"nonce": self.nonce, "req": req, **clean}).encode(), timeout)
        except TimeoutError as exc:
            raise HelperError("timeout", f"no answer to {req} in {timeout:.0f} s") from exc
        except OSError as exc:
            raise HelperError("unavailable", f"the helper socket failed ({type(exc).__name__})") from exc
        try:
            reply = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise HelperError("unavailable", "the helper sent something that is not JSON") from exc
        if not isinstance(reply, dict) or not reply.get("ok"):
            err = reply.get("error", "unavailable") if isinstance(reply, dict) else "unavailable"
            raise HelperError(err if err in ("refused", "permission", "unavailable") else "unavailable", str(reply.get("detail", ""))[:200] if isinstance(reply, dict) else "")
        return reply.get("data")


def ble_records(seconds: float, salt: str, client: HelperClient | None = None, approve: Approve | None = None) -> list[dict]:
    """A passive scan through the helper, shaped like the direct scan's records: ids are salted-hashed HERE, names are never kept, so the raw identifier never leaves this function."""
    import hashlib
    own = client is None
    c = client or HelperClient(approve=approve)
    try:
        raw = c.request("ble_scan", seconds=seconds)
    finally:
        if own:
            c.close()
    recs = []
    for r in raw if isinstance(raw, list) else []:
        ident = str(r.get("id", ""))
        recs.append({"id": hashlib.sha256((salt + "\x1f" + ident).encode()).hexdigest()[:8], "rssi": r.get("rssi"), "company": sorted(int(x) for x in r.get("company", [])),
                     "services": sorted(str(u).lower() for u in r.get("services", [])), "tx": r.get("tx")})
    return recs


def connect_paired(device: str, salt: str, action: str, *, client: HelperClient | None = None, approve: Approve | None = None) -> bool:
    """Connect or disconnect ONE device macOS already has paired (named by its salted hash). Returns whether it is connected afterwards. The caller has already shown the exact action and
    got the owner's yes; the launch of the helper asks again, as for any helper session."""
    own = client is None
    c = client or HelperClient(approve=approve)
    try:
        data = c.request("connect_paired", device=device, salt=salt, action=action)
    finally:
        if own:
            c.close()
    return bool(isinstance(data, dict) and data.get("connected"))


def audio_outputs(salt: str, *, client: HelperClient | None = None, approve: Approve | None = None) -> list[dict]:
    """The Mac's output devices as {id: salted hash, transport: builtin|bluetooth|usb|other, current: bool}. Names are not returned."""
    own = client is None
    c = client or HelperClient(approve=approve)
    try:
        data = c.request("audio_get", salt=salt)
    finally:
        if own:
            c.close()
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def audio_set(device: str, salt: str, *, client: HelperClient | None = None, approve: Approve | None = None) -> bool:
    """Make one output device (by its salted hash) the default output. Returns whether it is the default afterwards."""
    own = client is None
    c = client or HelperClient(approve=approve)
    try:
        data = c.request("audio_set", device=device, salt=salt)
    finally:
        if own:
            c.close()
    return bool(isinstance(data, dict) and data.get("current"))
