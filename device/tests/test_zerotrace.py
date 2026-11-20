"""P2-ZEROTRACE: nothing installed after unplug; OS residue only from the enumerated allowlist (D-010 three-arm diff; D-035).

EMULATED: each arm is a fresh container of the pinned clean image (P2-PORTABLE) whose root filesystem is writable, so any write the
session makes lands in it; after the container exits, `docker export` gives the whole filesystem and every entry is fingerprinted by
type, mode, owner, size and sha256 (timestamps are left out: an undo does not restore them, D-022). Arms:

    control    a blank stick at the same mount point, running a trivial script
    session    the medium, one command, reads only
    approved   the session plus one approved write (a changed file, a new folder with a new file)
    undone     the approved session plus its approved undo

Pass: session - control within the allowlist for this platform (empty for a Linux container); approved - session == the approved set
exactly; undone - session == nothing. A positive control (a "bad stick" that installs a service, a cron job, a PATH change, a driver
module, a program and a stray file) must be caught, each under its own name. Real OS residue (journald, udev, Windows registry, ...) is
not modelled by a container: those classes are enumerated in the allowlist and marked not measured.
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

import pytest
import yaml

import mas_device
from mas_device import approval as ap
from mas_device.keymodel import KeyModel

SRC = Path(mas_device.__file__).resolve().parents[1]  # the tree under test
TREE = SRC.parent
ALLOWLIST = TREE / "device" / "acceptance" / "residue_allowlist.yaml"
LOCK = TREE / "device" / "medium" / "medium.lock.json"
sys.path.insert(0, str(TREE / "device" / "medium"))
import mas_medium_build as md  # noqa: E402

TIMEOUT = 300
MOUNT = "/media/mas"
PREP = "mkdir -p /host/etc && printf 'port=1\\n' > /host/etc/app.conf"
SESSION_ARGS = f"sh {MOUNT}/launch.sh --root /host --state /state --target vm-a"
FLAGS = ["--network", "none", "--cap-drop", "ALL", "--security-opt", "no-new-privileges"]

# what an install looks like on Linux, by kind (D-010: no service, driver, autostart, PATH change or file outside the declared temp dir)
KINDS = [
    ("service", ["/etc/systemd/*", "/lib/systemd/*", "/usr/lib/systemd/*", "/etc/init.d/*", "/etc/rc?.d/*", "/etc/rc.local"]),
    ("autostart", ["/etc/cron*", "/var/spool/cron/*", "/etc/xdg/autostart/*", "/root/.config/autostart/*", "/home/*/.config/autostart/*"]),
    ("path", ["/etc/profile", "/etc/profile.d/*", "/etc/environment", "/etc/bash.bashrc", "/root/.profile", "/root/.bashrc",
              "/root/.bash_profile", "/home/*/.profile", "/home/*/.bashrc", "/home/*/.bash_profile", "/etc/ld.so.conf*"]),
    ("driver", ["/etc/modules", "/etc/modules-load.d/*", "/etc/modprobe.d/*", "/lib/modules/*", "/etc/udev/rules.d/*"]),
    ("program", ["/usr/local/*", "/opt/*", "/usr/bin/*", "/usr/sbin/*", "/bin/*", "/sbin/*"]),
]


def kind_of(path: str) -> str:
    for kind, pats in KINDS:
        if any(fnmatch.fnmatchcase(path, p) for p in pats):
            return kind
    return "file"


# ---- snapshots -------------------------------------------------------------------------------------------------------------------------

def snapshot(cid: str) -> dict[str, tuple]:
    """Every entry of the container's filesystem: path -> (type, mode, uid, gid, size, sha256 or link target). No timestamps."""
    p = subprocess.Popen(["docker", "export", cid], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = {}
    with tarfile.open(fileobj=p.stdout, mode="r|") as tf:
        for m in tf:
            name = m.name[2:] if m.name.startswith("./") else m.name
            path = "/" + name.strip("/")
            if m.isfile():
                h = hashlib.sha256()
                f = tf.extractfile(m)
                while chunk := f.read(1 << 20):
                    h.update(chunk)
                what = h.hexdigest()
            else:
                what = m.linkname if (m.issym() or m.islnk()) else ""
            out[path] = (m.type, m.mode & 0o7777, m.uid, m.gid, m.size if m.isfile() else 0, what)
    assert p.wait(timeout=TIMEOUT) == 0, p.stderr.read().decode("utf-8", "replace")
    return out


def diff(a: dict, b: dict) -> dict[str, str]:
    """b relative to a: path -> "added" | "removed" | "changed"."""
    d = {p: "added" for p in b.keys() - a.keys()}
    d.update({p: "removed" for p in a.keys() - b.keys()})
    d.update({p: "changed" for p in a.keys() & b.keys() if a[p] != b[p]})
    return d


def load_allowlist() -> dict:
    return yaml.safe_load(ALLOWLIST.read_text(encoding="utf-8"))


def violations(d: dict[str, str], platform: str, allow: dict | None = None) -> dict[str, str]:
    """Changes outside the platform's allowlist and its declared temp dirs, each named by kind ("service", "path", ..., "file")."""
    plat = (allow or load_allowlist())["platforms"][platform]
    pats = [p for c in plat["classes"] for p in c["paths"]]
    temps = plat["declared_temp_dirs"]
    out = {}
    for path, change in d.items():
        if any(fnmatch.fnmatchcase(path, p) for p in pats):
            continue
        if any(path == t or path.startswith(t.rstrip("/") + "/") for t in temps):
            out[path] = "temp_left"  # a declared temp dir is removed on exit: anything left in it is a violation too
            continue
        out[path] = f"{kind_of(path)}:{change}"
    return out


# ---- the arms ---------------------------------------------------------------------------------------------------------------------------

class Tick:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _docker_ok() -> None:
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.fail(f"ERROR docker unavailable: {exc}")
    if r.returncode != 0 or not r.stdout.strip():
        pytest.fail("ERROR the Docker engine is not running (start Docker Desktop from the Start menu, HANDOFF §2 step 0)")


def _cache() -> Path:
    cands = [TREE / "runs" / "medium-cache"]
    common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=TREE, capture_output=True,
                            text=True, timeout=30).stdout.strip()
    if common:
        cands.append(Path(common).parent / "runs" / "medium-cache")
    for c in cands:
        if c.is_dir():
            return c
    pytest.fail("ERROR medium cache missing: run `python device/medium/mas_medium_build.py fetch`")


class Arm:
    """One container: `docker run -i --name <unique>` (kept after exit so it can be exported), the model's side on stdin/stdout."""

    def __init__(self, world, name: str, medium: Path, command: str) -> None:
        self.cid = f"mas-zt-{name}-{uuid.uuid4().hex[:8]}"
        self.state = world.base / f"state-{name}"
        self.state.mkdir()
        world.containers.append(self.cid)
        cmd = ["docker", "run", "-i", "--name", self.cid, *FLAGS, "-v", f"{medium}:{MOUNT}:ro", "-v", f"{self.state}:/state",
               world.image, "sh", "-c", command]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.n = 0

    def call(self, method: str, **params) -> dict:
        self.n += 1
        self.p.stdin.write(json.dumps({"id": self.n, "method": method, "params": params}).encode() + b"\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        assert line, "the arm died: " + self.p.stderr.read().decode("utf-8", "replace")[-3000:]
        r = json.loads(line)
        assert r["ok"], r
        return r["result"]

    def finish(self) -> dict[str, tuple]:
        try:
            self.p.stdin.close()
        except OSError:
            pass
        rc = self.p.wait(timeout=TIMEOUT)
        err = self.p.stderr.read().decode("utf-8", "replace")
        self.p.stdout.close()
        self.p.stderr.close()
        assert rc == 0, err[-3000:]
        return snapshot(self.cid)


def approve(world, kr: dict) -> str:
    req = ap.KeyRequest(*(bytes.fromhex(kr[n]) for n in ("session_id", "nonce", "action_hash", "undo_hash", "summary")),
                        kr["cls"], kr["press_window_s"])
    h = world.key.request(req)
    assert world.key.display_text()
    world.clock.t += 2
    world.key.button.press()
    return world.key.sign(h).hex()


WRITE = {"etc/app.conf": b"port=2\n", "etc/mas.d/new.conf": b"enabled=1\n"}


def write(world, arm: Arm) -> dict:
    action = arm.call("plan_write", files={k: base64.b64encode(v).decode() for k, v in WRITE.items()})["action"]
    kr = arm.call("begin", action=action)["key_request"]
    arm.call("execute", token=approve(world, kr), action=action)
    return action


@pytest.fixture(scope="module")
def world():
    _docker_ok()
    root = TREE / "runs" / "zerotrace-tests"
    root.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="run-", dir=root))
    clock = Tick()
    key = KeyModel.create(base / "key", clock=clock)
    w = type("World", (), {})()
    w.base, w.clock, w.key, w.containers = base, clock, key, []
    w.image = json.loads(LOCK.read_text(encoding="utf-8"))["base_image"]
    w.medium = md.build(base / "medium", cache=_cache(), key_pub=key.public_key, tree=TREE)
    blank = base / "blank-stick"
    blank.mkdir()
    (blank / "hello.sh").write_bytes(b"#!/bin/sh\necho hello\n")
    w.blank = blank
    w.snaps = {}
    yield w
    for cid in w.containers:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=TIMEOUT)
    shutil.rmtree(base, ignore_errors=True)


@pytest.fixture(scope="module")
def arms(world):
    """The four arms, run once and shared by the tests below."""
    s = world.snaps
    s["control"] = Arm(world, "control", world.blank, f"{PREP} && sh {MOUNT}/hello.sh").finish()
    a = Arm(world, "session", world.medium, f"{PREP} && exec {SESSION_ARGS}")
    a.call("read", path="etc/app.conf")
    a.call("log_head")
    s["session"] = a.finish()
    a = Arm(world, "approved", world.medium, f"{PREP} && exec {SESSION_ARGS}")
    write(world, a)
    s["approved"] = a.finish()
    a = Arm(world, "undone", world.medium, f"{PREP} && exec {SESSION_ARGS}")
    action = write(world, a)
    undo = a.call("undo_for", action_hash=ap.action_hash(action).hex())["undo"]
    kr = a.call("begin_undo", undo=undo)["key_request"]
    a.call("execute_undo", token=approve(world, kr), undo=undo)
    s["undone"] = a.finish()
    return s


# ---- the allowlist -------------------------------------------------------------------------------------------------------------------------

def test_the_allowlist_enumerates_d010_classes_and_says_what_is_measured():
    allow = load_allowlist()
    plats = allow["platforms"]
    assert set(plats) == {"linux-container", "linux", "windows", "macos"}
    lc = plats["linux-container"]
    assert lc["measured"] is True and lc["classes"] == [] and lc["declared_temp_dirs"] == []
    for name in ("linux", "windows", "macos"):
        assert plats[name]["measured"] is False, name  # enumerated (D-010), not measured in v0: no VM image of these yet
        assert plats[name]["classes"], name
        for c in plats[name]["classes"]:
            assert set(c) == {"name", "paths"} and c["paths"], (name, c)
    names = {p: {c["name"] for c in plats[p]["classes"]} for p in ("linux", "windows", "macos")}
    assert {"journald", "udev-db", "recently-used", "shell-history"} <= names["linux"]
    assert {"usbstor", "mounted-devices", "setupapi-log", "prefetch", "amcache", "shimcache", "bam", "userassist", "srum", "shellbags",
            "event-logs", "defender-history", "usn-journal"} <= names["windows"]
    assert {"unified-log", "launchservices", "tcc"} <= names["macos"]


def test_the_detector_names_each_kind_of_install():
    allow = load_allowlist()
    d = {"/etc/systemd/system/mas.service": "added", "/etc/cron.d/mas": "added", "/root/.profile": "changed",
         "/etc/modules-load.d/mas.conf": "added", "/usr/local/bin/mas": "added", "/var/tmp/mas-left": "added", "/etc/rc.local": "added",
         "/home/u/.config/autostart/mas.desktop": "added", "/etc/profile.d/mas.sh": "added", "/etc/udev/rules.d/99-mas.rules": "added"}
    v = violations(d, "linux-container", allow)
    assert v == {"/etc/systemd/system/mas.service": "service:added", "/etc/cron.d/mas": "autostart:added",
                 "/root/.profile": "path:changed", "/etc/modules-load.d/mas.conf": "driver:added", "/usr/local/bin/mas": "program:added",
                 "/var/tmp/mas-left": "file:added", "/etc/rc.local": "service:added", "/home/u/.config/autostart/mas.desktop":
                 "autostart:added", "/etc/profile.d/mas.sh": "path:added", "/etc/udev/rules.d/99-mas.rules": "driver:added"}
    # an allowlisted class passes only on its own platform; a declared temp dir left behind is a violation
    linux = {"platforms": {"x": {"classes": [{"name": "journald", "paths": ["/var/log/journal/*"]}], "declared_temp_dirs": ["/tmp/mas"]}}}
    assert violations({"/var/log/journal/abc/system.journal": "changed", "/tmp/mas/x": "added"}, "x", linux) == {"/tmp/mas/x": "temp_left"}
    assert violations({"/var/log/journal/abc/system.journal": "changed"}, "linux-container", allow) != {}


def test_diff_ignores_timestamps_but_not_content_mode_owner_or_type():
    a = {"/f": ("0", 0o644, 0, 0, 3, "aa"), "/d": ("5", 0o755, 0, 0, 0, "")}
    assert diff(a, dict(a)) == {}
    assert diff(a, {**a, "/f": ("0", 0o644, 0, 0, 3, "bb")}) == {"/f": "changed"}
    assert diff(a, {**a, "/f": ("0", 0o755, 0, 0, 3, "aa")}) == {"/f": "changed"}
    assert diff(a, {**a, "/f": ("0", 0o644, 1000, 0, 3, "aa")}) == {"/f": "changed"}
    assert diff(a, {**a, "/f": ("2", 0o777, 0, 0, 0, "/x")}) == {"/f": "changed"}
    assert diff(a, {"/f": a["/f"]}) == {"/d": "removed"}
    assert diff({}, a) == {"/f": "added", "/d": "added"}


# ---- the three arms (D-010) --------------------------------------------------------------------------------------------------------------

def test_the_arms_are_real_snapshots(arms):
    for name, snap in arms.items():
        assert len(snap) > 1000, name  # the whole root filesystem, not a fragment
        assert "/host/etc/app.conf" in snap and "/usr/bin/sh" in snap and snap["/bin"][0] == tarfile.SYMTYPE, name  # /bin -> usr/bin
    assert arms["control"]["/host/etc/app.conf"][5] == hashlib.sha256(b"port=1\n").hexdigest()


def test_session_minus_control_is_within_the_allowlist(arms):
    d = diff(arms["control"], arms["session"])
    assert violations(d, "linux-container") == {}, d
    assert d == {}  # the Linux container allowlist is empty: the session leaves nothing at all


def test_approved_minus_session_is_exactly_the_approved_set(arms):
    d = diff(arms["session"], arms["approved"])
    assert d == {"/host/etc/app.conf": "changed", "/host/etc/mas.d": "added", "/host/etc/mas.d/new.conf": "added"}, d
    for rel, data in WRITE.items():
        assert arms["approved"]["/host/" + rel][5] == hashlib.sha256(data).hexdigest(), rel
    assert arms["approved"]["/host/etc/mas.d"][0] == tarfile.DIRTYPE


def test_undo_returns_to_the_session_arm(arms):
    assert diff(arms["session"], arms["undone"]) == {}


# ---- positive control: a bad stick is caught, each install under its own name -------------------------------------------------------------

BAD = """#!/bin/sh
set -e
mkdir -p /etc/systemd/system /etc/cron.d /etc/modules-load.d
echo '[Service]' > /etc/systemd/system/mas.service
echo '* * * * * root true' > /etc/cron.d/mas
echo 'export PATH=/media/mas:$PATH' >> /root/.profile
echo mas > /etc/modules-load.d/mas.conf
printf '#!/bin/sh\\n' > /usr/local/bin/mas && chmod 755 /usr/local/bin/mas
echo left > /var/tmp/mas-left
"""


def test_a_bad_stick_is_caught_by_kind(world, arms):
    bad = world.base / "bad-stick"
    bad.mkdir()
    (bad / "bad.sh").write_bytes(BAD.encode("ascii"))
    snap = Arm(world, "bad", bad, f"{PREP} && sh {MOUNT}/bad.sh").finish()
    v = violations(diff(arms["control"], snap), "linux-container")
    kinds = {k.split(":")[0] for k in v.values()}
    assert {"service", "autostart", "path", "driver", "program", "file"} <= kinds, v
    assert v["/root/.profile"] == "path:changed" and v["/usr/local/bin/mas"] == "program:added", v
