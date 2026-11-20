"""P2-FLEET: pairing, per-write approval, auto-setup and undo between two or more isolated machines, with no packets to non-peers
(D-006 rule 7, D-021, D-036).

EMULATED: machines are containers of the pinned clean image on an `--internal` Docker network (no route out). A = the device owner's
coordinator, B = a host with the device plugged in, C = a sentinel that only captures frames. Both A and B run from the read-only
medium. The key is the test's software key model; the host owner's keyboard and the key's link are emulated by B's stdin/stdout.

Flow: B's owner runs `launch.sh fleet-join`, types `JOIN <fleet>`, the device owner presses for JOIN_FLEET (A verifies the token
against the key it pins); B's gate then writes A's setup plan one step per press (MAS node sources and node.json); leaving undoes
each step with its own press and A drops B from its roster. C must see no frame addressed to it and no ARP for anyone but A and B.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import mas_device
from mas_device import approval as ap
from mas_device import pairing as pr
from mas_device.keymodel import KeyModel

SRC = Path(mas_device.__file__).resolve().parents[1]  # the tree under test
TREE = SRC.parent
LOCK = TREE / "device" / "medium" / "medium.lock.json"
sys.path.insert(0, str(TREE / "device" / "medium"))
import mas_medium_build as md  # noqa: E402
from mas_fleet import coordinator as fc  # noqa: E402
from mas_fleet import plan as fp  # noqa: E402

TIMEOUT = 300
MOUNT = "/media/mas"
PORT = 7450
FLEET = "lab"
PY = f"{MOUNT}/runtime/python/bin/python3.13"
NOV6 = ["--sysctl", "net.ipv6.conf.all.disable_ipv6=1", "--sysctl", "net.ipv6.conf.default.disable_ipv6=1"]
LOCKED = ["--cap-drop", "ALL", "--security-opt", "no-new-privileges", *NOV6]


# ---- shared fixtures -----------------------------------------------------------------------------------------------------------------

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


def tree_hashes(root: Path) -> dict[str, str]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        out["dir:" + rel_dir] = ""
        for n in filenames:
            p = Path(dirpath, n)
            out[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def records(state: Path, name: str = "audit.log") -> list[dict]:
    p = state / name
    return [json.loads(x) for x in p.read_bytes().splitlines() if x] if p.exists() else []


@pytest.fixture(scope="module")
def world():
    _docker_ok()
    root = TREE / "runs" / "fleet-tests"
    root.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="run-", dir=root))
    clock = Tick()
    key = KeyModel.create(base / "key", clock=clock)
    w = type("World", (), {})()
    w.base, w.clock, w.key, w.containers, w.networks, w.macs = base, clock, key, [], [], set()
    w.image = json.loads(LOCK.read_text(encoding="utf-8"))["base_image"]
    w.medium = md.build(base / "medium", cache=_cache(), key_pub=key.public_key, tree=TREE)
    payload = base / "payload"
    # MAS's node keeps its own key in the OS credential store (envrec_node/credentials.py); the gate refuses any file named credentials*
    # (D-025, guardrail 8), so the v0 payload leaves it out and the installed node is not runnable yet (D-036)
    shutil.copytree(TREE / "src" / "envrec_node", payload / "envrec_node",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "credentials.py"))
    w.payload = payload
    yield w
    for cid in w.containers:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True, timeout=TIMEOUT)
    for net in w.networks:
        subprocess.run(["docker", "network", "rm", net], capture_output=True, timeout=TIMEOUT)
    shutil.rmtree(base, ignore_errors=True)


# ---- the machines ----------------------------------------------------------------------------------------------------------------------

def network(world) -> tuple[str, str]:
    """An --internal network (no route out) on its own /24; every machine gets a fixed address on it (no lookups, no races)."""
    for _ in range(20):
        name, prefix = f"mas-fleet-{uuid.uuid4().hex[:8]}", f"10.231.{secrets.randbelow(250) + 1}"
        r = subprocess.run(["docker", "network", "create", "--internal", "--subnet", f"{prefix}.0/24", name], capture_output=True,
                           text=True, timeout=TIMEOUT)
        if r.returncode == 0:
            world.networks.append(name)
            return name, prefix
    pytest.fail("ERROR could not create an isolated network: " + r.stderr)


class Machine:
    def __init__(self, world, role: str, net: tuple[str, str], host: int, args: list[str], mounts: list[str],
                 flags: list[str] = LOCKED) -> None:
        self.cid = f"mas-fl-{role}-{uuid.uuid4().hex[:8]}"
        self.ip = f"{net[1]}.{host}"
        self.mac = "02:00:0a:e7:%02x:%02x" % (int(net[1].split(".")[-1]), host)  # fixed, so every frame names its machine
        world.containers.append(self.cid)
        world.macs.add(self.mac)
        cmd = ["docker", "run", "-i", "--rm", "--name", self.cid, "--network", net[0], "--ip", self.ip, "--mac-address", self.mac, *flags, "-v", f"{world.medium}:{MOUNT}:ro",
               *mounts, world.image, *args]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def line(self) -> dict | None:
        raw = self.p.stdout.readline()
        return json.loads(raw) if raw else None

    def send(self, msg: dict) -> None:
        self.p.stdin.write(json.dumps(msg).encode() + b"\n")
        self.p.stdin.flush()

    def finish(self) -> tuple[int, str, bytes]:
        try:
            self.p.stdin.close()
        except OSError:
            pass
        out = self.p.stdout.read()
        rc = self.p.wait(timeout=TIMEOUT)
        err = self.p.stderr.read().decode("utf-8", "replace")
        return rc, err, out


CANARY = r'''
import json, select, socket, sys
s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(3))
own = None
frames = []
print("ready", flush=True)
while True:
    r, _, _ = select.select([s, sys.stdin], [], [], 1.0)
    if sys.stdin in r and not sys.stdin.buffer.read1(4096):
        break
    if s in r:
        data, addr = s.recvfrom(65535)
        if addr[2] == socket.PACKET_OUTGOING:
            continue
        et = int.from_bytes(data[12:14], "big")
        f = {"type": hex(et), "dst_mac": data[0:6].hex(), "src_mac": data[6:12].hex()}
        if et == 0x0806:
            f.update(op=int.from_bytes(data[20:22], "big"), sender=socket.inet_ntoa(data[28:32]), target=socket.inet_ntoa(data[38:42]))
        elif et == 0x0800:
            f.update(src=socket.inet_ntoa(data[26:30]), dst=socket.inet_ntoa(data[30:34]), proto=data[23])
        elif et == 0x86DD:
            f.update(src6=socket.inet_ntop(socket.AF_INET6, data[22:38]), dst6=socket.inet_ntop(socket.AF_INET6, data[38:54]),
                     next=data[20])
        frames.append(f)
print(json.dumps(frames), flush=True)
'''


class Sentinel:
    """C: captures every frame it receives (CAP_NET_RAW only here); reports them when its stdin closes."""

    def __init__(self, world, net: tuple[str, str]) -> None:
        script = world.base / f"canary-{uuid.uuid4().hex[:6]}"
        script.mkdir()
        (script / "canary.py").write_text(CANARY, encoding="ascii")
        self.m = Machine(world, "canary", net, 12, [PY, "-I", "-B", "/canary/canary.py"], ["-v", f"{script}:/canary:ro"],
                         flags=["--cap-drop", "ALL", "--cap-add", "NET_RAW", *NOV6])
        assert self.m.p.stdout.readline().strip() == b"ready", self.m.p.stderr.read()[-2000:]
        self.ip, self.mac = self.m.ip, self.m.mac

    def frames(self) -> list[dict]:
        rc, err, out = self.m.finish()
        assert rc == 0, err[-2000:]
        return json.loads(out.decode().strip().splitlines()[-1])


def non_peer_violations(frames: list[dict], peers: dict[str, str], sentinel, machines: set[str]) -> list[dict]:
    """peers: mac -> ip of the fleet's machines. Every frame names its sender by MAC (each test machine has a fixed one).

    A peer may send only ARP between peers and IPv4 between peers (never to the sentinel, never broadcast or multicast). Any frame
    from another test machine (a scanner) is a violation. Frames from MACs that belong to no test machine are the emulated switch
    itself (Docker's bridge in its VM): only IPv6 link-local multicast (its own MLD/ND chatter) is tolerated from it, as emulator noise.
    """
    ips = set(peers.values())
    bad = []
    for f in frames:
        src = f["src_mac"]
        if src == sentinel.mac.replace(":", ""):
            continue
        if src in {m.replace(":", "") for m in peers}:
            if f["type"] == "0x806":
                ok = f["sender"] in ips and f["target"] in ips
            elif f["type"] == "0x800":
                ok = f["src"] in ips and f["dst"] in ips and f["dst"] != sentinel.ip
            else:
                ok = False  # IPv6 is off on every machine: a peer sends none
            if not ok:
                bad.append(f)
        elif src in {m.replace(":", "") for m in machines}:
            bad.append(f)  # a test machine that is not a peer said something on the fleet's network
        elif not (f["type"] == "0x86dd" and f.get("dst6", "").startswith("ff02:")):
            bad.append(f)  # the switch itself may only chatter IPv6 link-local multicast
    return bad


def approve(world, kr: dict) -> str:
    req = ap.KeyRequest(*(bytes.fromhex(kr[n]) for n in ("session_id", "nonce", "action_hash", "undo_hash", "summary")),
                        kr["cls"], kr["press_window_s"])
    h = world.key.request(req)
    assert world.key.display_text()
    world.clock.t += 2
    world.key.button.press()
    return world.key.sign(h).hex()


def fleet_round(world, *, typed: str, then: str = "leave") -> dict:
    """Run C, A and B on a fresh isolated network; play B's owner (typing) and the device owner (pressing). Returns what happened."""
    net = network(world)
    c = Sentinel(world, net)
    a_state = world.base / f"a-state-{uuid.uuid4().hex[:6]}"
    a_state.mkdir()
    a = Machine(world, "a", net, 10, ["sh", f"{MOUNT}/launch.sh", "fleet-coordinator", "--state", "/a-state", "--listen", f"0.0.0.0:{PORT}",
                                  "--fleet", FLEET, "--target", "vm-a", "--payload", "/payload", "--connections", "1"],
                ["-v", f"{a_state}:/a-state", "-v", f"{world.payload}:/payload:ro"])
    ready = a.line()
    assert ready and ready.get("listening") == PORT, (ready, a.p.stderr.read()[-2000:] if not ready else "")
    a_ip = a.ip
    host = world.base / f"host-{uuid.uuid4().hex[:6]}"
    (host / "etc").mkdir(parents=True)
    (host / "etc" / "app.conf").write_bytes(b"port=1\n")
    b_state = world.base / f"b-state-{uuid.uuid4().hex[:6]}"
    b_state.mkdir()
    before = tree_hashes(host)
    b = Machine(world, "b", net, 11, ["sh", f"{MOUNT}/launch.sh", "fleet-join", "--root", "/host", "--state", "/state", "--target", "vm-b",
                                  "--coordinator", f"{a_ip}:{PORT}"], ["-v", f"{host}:/host", "-v", f"{b_state}:/state"])
    b_ip = b.ip
    seen = {"prompts": [], "classes": [], "status": [], "after_setup": None}
    while (msg := b.line()) is not None:
        if "owner_prompt" in msg:
            seen["prompts"].append(msg["owner_prompt"])
            b.send({"owner": typed})
        elif "key_request" in msg:
            seen["classes"].append(msg["key_request"]["cls"])
            b.send({"token": approve(world, msg["key_request"])})
        elif "status" in msg:
            seen["status"].append(msg["status"])
            if msg["status"] == "setup_done":
                seen["after_setup"] = tree_hashes(host)
                b.send({"cmd": then})
    seen["b"] = b.finish()
    seen["a"] = a.finish()
    seen["frames"] = c.frames()
    seen.update(host=host, before=before, after=tree_hashes(host), a_state=a_state, b_state=b_state, peers={a.mac: a_ip, b.mac: b_ip},
                sentinel=c, machines=set(world.macs))
    return seen


@pytest.fixture(scope="module")
def joined(world):
    return fleet_round(world, typed=f"JOIN {FLEET}")


# ---- the whole flow ------------------------------------------------------------------------------------------------------------------------

def test_join_setup_and_leave_across_isolated_machines(world, joined):
    rc, err, _ = joined["b"]
    assert rc == 0, err[-3000:]
    assert joined["a"][0] == 0, joined["a"][1][-3000:]
    assert joined["prompts"] == [f"Type JOIN {FLEET} to let this computer join the fleet '{FLEET}' of vm-a"]
    # one press to join, one per setup step, one per undo: nothing is written or undone without its own press
    assert joined["classes"] == [ap.Class.JOIN_FLEET, ap.Class.WRITE, ap.Class.WRITE, ap.Class.UNDO, ap.Class.UNDO]
    assert joined["status"] == ["joined", "setup_done", "left"]
    # after setup B holds the MAS node sources and its node.json, exactly; after leaving, B is as it was
    mid = joined["after_setup"]
    for f in sorted(world.payload.rglob("*.py")):
        rel = ".mas/node/" + f.relative_to(world.payload).as_posix()
        assert mid[rel] == hashlib.sha256(f.read_bytes()).hexdigest(), rel
    extra = {k for k in set(mid) - set(joined["before"]) if not k.startswith(("dir:.mas", ".mas/"))}
    assert extra == set()
    assert joined["after"] == joined["before"]


def test_the_node_config_never_lets_the_node_deploy_further(world, joined):
    # the plan A offers (recomputed here from A's own inputs) carries node.json with deploy-further off (MAS worker_can_deploy_further)
    plan = fc.build_plan(FLEET, "vm-a", world.payload, pr.load(world.medium / pr.RECORD_NAME))
    node = json.loads(base64.b64decode(plan["steps"][-1]["files"][".mas/node.json"]))
    assert node == {"coordinator": "vm-a", "fleet": FLEET, "key_id": ap.key_id_of(world.key.public_key).hex(),
                    "worker_can_deploy_further": False}


def test_the_coordinator_logs_the_typed_confirmation_next_to_the_press_and_keeps_a_roster(joined):
    recs = records(joined["a_state"])
    kinds = [r["type"] for r in recs]
    assert kinds.index("fleet.joined") < kinds.index("fleet.setup_done") < kinds.index("fleet.left")
    j = next(r["payload"] for r in recs if r["type"] == "fleet.joined")
    assert j["target"] == "vm-b" and j["owner_typed"] == f"JOIN {FLEET}" and isinstance(j["counter"], int) and j["counter"] >= 1
    assert json.loads((joined["a_state"] / "roster.json").read_text(encoding="utf-8")) == {"fleet": FLEET, "nodes": {}}


def test_no_packet_reaches_a_non_peer(joined):
    assert non_peer_violations(joined["frames"], joined["peers"], joined["sentinel"], joined["machines"]) == []


def test_without_the_host_owners_typed_confirmation_nothing_happens(world):
    r = fleet_round(world, typed="join lab")
    rc, err, _ = r["b"]
    assert rc != 0 and "ERROR" in err and "confirm" in err.lower(), err[-2000:]
    assert r["classes"] == [] and r["status"] == ["refused"]  # the key was never asked
    assert r["after"] == r["before"]
    kinds = [x["type"] for x in records(r["a_state"])]
    assert "fleet.joined" not in kinds and "fleet.join_refused" not in kinds  # B never even sent a join request
    assert json.loads((r["a_state"] / "roster.json").read_text(encoding="utf-8")) == {"fleet": FLEET, "nodes": {}}
    assert non_peer_violations(r["frames"], r["peers"], r["sentinel"], r["machines"]) == []


def test_the_sentinel_catches_a_scanner(world):
    """Positive control: a machine on the network that probes the sentinel is seen."""
    net = network(world)
    c = Sentinel(world, net)
    probe = f"import socket; s = socket.socket(); s.settimeout(2); s.connect_ex(('{c.ip}', 22)); s.close()"
    s = Machine(world, "scanner", net, 13, [PY, "-I", "-B", "-c", probe], [], flags=["--cap-drop", "ALL", *NOV6])
    assert s.finish()[0] == 0
    bad = non_peer_violations(c.frames(), {}, c, set(world.macs))  # no peers here: the scanner is not one
    assert any(f["src_mac"] == s.mac.replace(":", "") and f["type"] == "0x806" and f["target"] == c.ip for f in bad), bad


# ---- the coordinator, in-process -----------------------------------------------------------------------------------------------------------

def _coordinator(world, tmp_path, *, pinned_pub=None):
    medium = tmp_path / "medium"
    pr.provision(medium, pinned_pub or world.key.public_key)
    state = tmp_path / "a-state"
    state.mkdir()
    return fc.Coordinator(pairing=medium / pr.RECORD_NAME, state=state, fleet=FLEET, target="vm-a", payload=world.payload), state


def _join(co, typed: str, key, clock, *, sign_with=None) -> list[dict]:
    out = co.handle({"t": "hello", "target": "vm-b"})
    assert out[0]["t"] == "offer"
    plan_hash = out[0]["plan_hash"]
    out = co.handle({"t": "join", "owner_typed": typed, "plan_hash": plan_hash})
    if out[0]["t"] != "key_request":
        return out
    kr = out[0]["key_request"]
    if sign_with is None:
        req = ap.KeyRequest(*(bytes.fromhex(kr[n]) for n in ("session_id", "nonce", "action_hash", "undo_hash", "summary")),
                            kr["cls"], kr["press_window_s"])
        h = key.request(req)
        clock.t += 2
        key.button.press()
        token = key.sign(h)
    else:
        body = ap.pack_body(ap.key_id_of(sign_with.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)), 1,
                            ap.KeyRequest(*(bytes.fromhex(kr[n]) for n in ("session_id", "nonce", "action_hash", "undo_hash", "summary")),
                                          kr["cls"], kr["press_window_s"]))
        token = body + sign_with.sign(body)
    return co.handle({"t": "token", "token": token.hex()})


def test_coordinator_accepts_only_the_pinned_key(world, tmp_path):
    co, state = _coordinator(world, tmp_path)
    out = _join(co, f"JOIN {FLEET}", world.key, world.clock, sign_with=Ed25519PrivateKey.generate())
    assert out == [{"t": "refused", "reason": "unknown_key"}]
    assert json.loads((state / "roster.json").read_text(encoding="utf-8"))["nodes"] == {}
    co2, state2 = _coordinator(world, tmp_path / "ok")
    assert _join(co2, f"JOIN {FLEET}", world.key, world.clock) == [{"t": "joined"}]
    assert set(json.loads((state2 / "roster.json").read_text(encoding="utf-8"))["nodes"]) == {"vm-b"}


def test_coordinator_checks_the_typed_confirmation_itself(world, tmp_path):
    for typed in ("join lab", f"JOIN {FLEET} ", "JOIN other", "", f"JOIN {FLEET}\n"):
        co, state = _coordinator(world, tmp_path / f"t{abs(hash(typed))}")
        assert _join(co, typed, world.key, world.clock) == [{"t": "refused", "reason": "owner_not_confirmed"}], typed
        assert [r["type"] for r in records(state)][-1] == "fleet.join_refused"


def test_coordinator_refuses_a_changed_plan_and_messages_out_of_order(world, tmp_path):
    co, _ = _coordinator(world, tmp_path)
    assert co.handle({"t": "join", "owner_typed": f"JOIN {FLEET}", "plan_hash": "00" * 32}) == [{"t": "refused", "reason": "out_of_order"}]
    co, _ = _coordinator(world, tmp_path / "b")
    co.handle({"t": "hello", "target": "vm-b"})
    assert co.handle({"t": "join", "owner_typed": f"JOIN {FLEET}", "plan_hash": "00" * 32}) == [{"t": "refused", "reason": "plan_changed"}]
    co, _ = _coordinator(world, tmp_path / "c")
    assert co.handle({"t": "setup_done", "actions": []}) == [{"t": "refused", "reason": "out_of_order"}]
    assert co.handle({"t": "hello", "target": "../../etc"}) == [{"t": "refused", "reason": "malformed"}]


# ---- the node's plan check, in-process -------------------------------------------------------------------------------------------------------

def _plan(files: dict[str, bytes]) -> dict:
    return {"v": 1, "fleet": FLEET, "coordinator": "vm-a",
            "steps": [{"title": "x", "files": {k: base64.b64encode(v).decode() for k, v in files.items()}}]}


def test_the_node_refuses_a_plan_that_writes_outside_its_fleet_folder():
    fp.validate(_plan({".mas/node.json": b"{}"}))
    for bad in ("etc/app.conf", ".mas/../etc/app.conf", "/.mas/x", ".mas//x", ".mas/./x", ".masx/y", ".mas\\x", ".mas/.ssh/id_rsa",
                ".mas/credentials.json", ".mas/x.pem", ".mas/.env", "C:/x", ".mas/x\x00y", ".mas/" + "a" * 101):
        with pytest.raises(fp.PlanRefused):
            fp.validate(_plan({bad: b"x"}))
    for shape in ({}, {"v": 2, "fleet": FLEET, "coordinator": "vm-a", "steps": []}, {**_plan({".mas/a": b"x"}), "extra": 1},
                  {**_plan({".mas/a": b"x"}), "steps": []}, {**_plan({".mas/a": b"x"}), "steps": [{"title": "x", "files": {}}] * 9},
                  {**_plan({".mas/a": b"x"}), "fleet": "Lab!"}):
        with pytest.raises(fp.PlanRefused):
            fp.validate(shape)
    with pytest.raises(fp.PlanRefused):
        fp.validate({**_plan({".mas/a": b"x"}), "steps": [{"title": "x", "files": {".mas/a": "not base64!"}}]})
    with pytest.raises(fp.PlanRefused):
        fp.validate(_plan({".mas/big": b"\0" * (fp.MAX_PLAN_BYTES + 1)}))


# ---- propagation is human-initiated: the code that could do otherwise is absent ----------------------------------------------------------

def test_fleet_code_never_discovers_listens_on_a_node_or_dials_from_the_coordinator():
    pkg = SRC / "mas_fleet"
    node = (pkg / "node.py").read_text(encoding="utf-8")
    coord = (pkg / "coordinator.py").read_text(encoding="utf-8")
    everything = "".join(p.read_text(encoding="utf-8") for p in sorted(pkg.glob("*.py")))
    for marker in ("SO_BROADCAST", "<broadcast>", "255.255.255.255", "IP_ADD_MEMBERSHIP", "envrec", "subprocess.Popen(\"", "shell=True",
                   "connect_ex", "getaddrinfo", "gethostbyname"):
        assert marker not in everything, marker
    assert ".bind(" not in node and ".listen(" not in node and "create_server" not in node
    assert "create_connection" not in coord and ".connect(" not in coord and "subprocess" not in coord
