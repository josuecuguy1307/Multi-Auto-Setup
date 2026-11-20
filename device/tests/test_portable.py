"""P2-PORTABLE: one-command launch from a read-only medium in a clean container; the profiler picks the compute mode (D-003, D-034).

EMULATED: the "host" is a Debian container with no Python, no network, a read-only root filesystem and no capabilities; the medium is a
folder bind-mounted read-only. The medium carries its own interpreter (python-build-standalone), its wheels and the device code; nothing
is installed in the container. Inputs are pinned in device/medium/medium.lock.json and cached under runs/medium-cache (git-ignored).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import mas_device
from mas_device import approval as ap
from mas_device import medium as md
from mas_device import pairing as pr
from mas_device import profiler as pf
from mas_device.keymodel import KeyModel

SRC = Path(mas_device.__file__).resolve().parents[1]  # the tree under test: the oracle points PYTHONPATH at its own src
TREE = SRC.parent
LOCK = TREE / "device" / "medium" / "medium.lock.json"
TIMEOUT = 300
MOUNT = "/media/mas"


def _cache() -> Path:
    """runs/medium-cache of this tree, or of the main checkout when the tree is a worktree or a copy under runs/."""
    cands = [TREE / "runs" / "medium-cache"]
    try:
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=TREE, capture_output=True,
                                text=True, timeout=30).stdout.strip()
        if common:
            cands.append(Path(common).parent / "runs" / "medium-cache")
    except OSError:
        pass
    for c in cands:
        if c.is_dir():
            return c
    pytest.fail("ERROR medium cache missing: run `python -m mas_device.medium fetch` (downloads pinned in device/medium/medium.lock.json)")


def _docker_ok() -> None:
    try:
        r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        pytest.fail(f"ERROR docker unavailable: {exc}")
    if r.returncode != 0 or not r.stdout.strip():
        pytest.fail("ERROR the Docker engine is not running (start Docker Desktop from the Start menu, HANDOFF §2 step 0)")


class Tick:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    _docker_ok()
    base = tmp_path_factory.mktemp("portable")
    clock = Tick()
    key = KeyModel.create(base / "key", clock=clock)
    out = md.build(base / "medium", cache=_cache(), key_pub=key.public_key, tree=TREE)
    w = type("Built", (), {})()
    w.medium, w.key, w.clock, w.base, w.lock = out, key, clock, base, json.loads(LOCK.read_text(encoding="utf-8"))
    return w


def manifest_of(medium: Path) -> dict[str, str]:
    out = {}
    for line in (medium / md.MANIFEST).read_text(encoding="ascii").splitlines():
        digest, rel = line.split("  ", 1)
        out[rel] = digest
    return out


def tree_hashes(medium: Path) -> dict[str, str]:
    out = {}
    for dirpath, dirnames, filenames in os.walk(medium):
        for n in filenames:
            p = Path(dirpath, n)
            rel = p.relative_to(medium).as_posix()
            if rel != md.MANIFEST:
                out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


class Session:
    """`docker run -i <clean image> sh /media/mas/launch.sh ...`: the model's side holds its stdin/stdout, the owner sees stderr."""

    def __init__(self, built, host: Path, state: Path, *, medium: Path | None = None, ro: bool = True, extra: list[str] = (),
                 args: list[str] | None = None) -> None:
        medium = medium or built.medium
        if args is None:
            args = ["--root", "/host", "--state", "/state", "--target", "vm-a"]
        cmd = ["docker", "run", "--rm", "-i", "--network", "none", "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m",
               "--cap-drop", "ALL", "--security-opt", "no-new-privileges", *extra,
               "-v", f"{medium}:{MOUNT}" + (":ro" if ro else ""), "-v", f"{host}:/host", "-v", f"{state}:/state",
               built.lock["base_image"], "sh", f"{MOUNT}/launch.sh", *args]
        self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.n = 0

    def call(self, method: str, **params) -> dict:
        self.n += 1
        self.p.stdin.write(json.dumps({"id": self.n, "method": method, "params": params}).encode() + b"\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        assert line, "the session died: " + self.p.stderr.read().decode("utf-8", "replace")[-3000:]
        return json.loads(line)

    def close(self) -> tuple[int, str]:
        try:
            self.p.stdin.close()
        except OSError:
            pass
        rc = self.p.wait(timeout=TIMEOUT)
        err = self.p.stderr.read().decode("utf-8", "replace")
        self.p.stdout.close()
        self.p.stderr.close()
        return rc, err


def dirs(built, name: str) -> tuple[Path, Path]:
    host = built.base / f"host-{name}"
    (host / "etc").mkdir(parents=True)
    (host / "etc" / "app.conf").write_bytes(b"port=1\n")
    state = built.base / f"state-{name}"
    state.mkdir()
    return host, state


def records(state: Path) -> list[dict]:
    p = state / "audit.log"
    return [json.loads(x) for x in p.read_bytes().splitlines() if x] if p.exists() else []


def approve(built, kr: dict) -> str:
    req = ap.KeyRequest(*(bytes.fromhex(kr[n]) for n in ("session_id", "nonce", "action_hash", "undo_hash", "summary")),
                        kr["cls"], kr["press_window_s"])
    h = built.key.request(req)
    assert built.key.display_text()
    built.clock.t += 2
    built.key.button.press()
    return built.key.sign(h).hex()


def write_session(built, name: str, **kw) -> tuple[int, str, Path, Path]:
    host, state = dirs(built, name)
    s = Session(built, host, state, **kw)
    r = s.call("plan_write", files={"etc/app.conf": base64.b64encode(b"port=2\n").decode()})
    assert r["ok"], r
    action = r["result"]["action"]
    r = s.call("begin", action=action)
    assert r["ok"], r
    r = s.call("execute", token=approve(built, r["result"]["key_request"]), action=action)
    assert r["ok"], r
    rc, err = s.close()
    return rc, err, host, state


def started(state: Path) -> dict:
    s = [x["payload"] for x in records(state) if x["type"] == "gate.started"]
    assert len(s) == 1, records(state)
    return s[0]


# ---- the pinned inputs and the medium ---------------------------------------------------------------------------------------------------

def test_lock_pins_every_input_and_the_cache_matches(built):
    lock = built.lock
    assert lock["base_image"].startswith("debian:") and "@sha256:" in lock["base_image"]
    py = lock["python"]
    assert py["file"].startswith("cpython-3.13.") and py["file"].endswith("-x86_64-unknown-linux-gnu-install_only.tar.gz")
    names = {w["file"].split("-")[0] for w in lock["wheels"]}
    assert names == {"cryptography", "cffi", "pycparser"}
    cache = _cache()
    for item in [py, *lock["wheels"]]:
        assert len(item["sha256"]) == 64 and item["url"].startswith("https://")
        data = (cache / ("" if item is py else "wheels") / item["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == item["sha256"], item["file"]


def test_medium_layout_manifest_and_device_code(built):
    m = built.medium
    man = manifest_of(m)
    assert man == tree_hashes(m)  # the manifest names every file on the medium, and nothing else
    for rel in ("launch.sh", "runtime/launch.py", pr.RECORD_NAME, "runtime/python/bin/python3.13"):
        assert rel in man, rel
    assert any(r.startswith("runtime/site/cryptography/") for r in man)
    # the device code on the medium is the tree's own, byte for byte, sources only
    own = {f.name for f in (SRC / "mas_device").glob("*.py")}
    on_medium = {r.split("/")[-1] for r in man if r.startswith("runtime/mas_device/")}
    assert on_medium == own
    for name in own:
        assert (m / "runtime" / "mas_device" / name).read_bytes() == (SRC / "mas_device" / name).read_bytes()
    assert not any("__pycache__" in r or r.endswith(".pyc") for r in man if not r.startswith("runtime/python/"))
    # nothing a FAT/exFAT stick could not hold, and nothing we do not need
    for dirpath, dirnames, filenames in os.walk(m):
        for n in dirnames + filenames:
            assert not os.path.islink(os.path.join(dirpath, n)), os.path.join(dirpath, n)
    for pruned in ("runtime/python/share/", "runtime/python/include/", "runtime/python/lib/python3.13/test/",
                   "runtime/python/lib/python3.13/tkinter/", "runtime/python/lib/python3.13/idlelib/"):
        assert not any(r.startswith(pruned) for r in man), pruned
    assert pr.load(m / pr.RECORD_NAME).key_pub == built.key.public_key
    sh = (m / "launch.sh").read_text(encoding="ascii")
    assert sh.startswith("#!/bin/sh\n") and " -I -B " in sh and "\r" not in sh


def test_medium_build_is_reproducible(built, tmp_path):
    again = md.build(tmp_path / "again", cache=_cache(), key_pub=built.key.public_key, tree=TREE)
    a, b = manifest_of(built.medium), manifest_of(again)
    a.pop(pr.RECORD_NAME)
    b.pop(pr.RECORD_NAME)  # the medium id is random per medium; everything else is identical
    assert a == b
    with pytest.raises(FileExistsError):
        md.build(tmp_path / "again", cache=_cache(), key_pub=built.key.public_key, tree=TREE)  # never builds over a medium


# ---- the clean host ---------------------------------------------------------------------------------------------------------------------

def test_the_clean_container_has_no_python_and_no_network(built):
    probe = "for p in python3 python python3.13 pip pip3; do command -v $p && exit 7; done; " \
            "cat /proc/net/route | tail -n +2 | grep -q . && exit 8; echo clean"
    r = subprocess.run(["docker", "run", "--rm", "--network", "none", built.lock["base_image"], "sh", "-c", probe],
                       capture_output=True, text=True, timeout=TIMEOUT)
    assert r.returncode == 0 and r.stdout.strip() == "clean", (r.returncode, r.stdout, r.stderr)


# ---- one command, from the read-only medium -----------------------------------------------------------------------------------------------

def test_one_command_session_from_the_read_only_medium(built):
    before = tree_hashes(built.medium)
    rc, err, host, state = write_session(built, "main")
    assert rc == 0, err[-3000:]
    assert (host / "etc" / "app.conf").read_bytes() == b"port=2\n"  # the approved write happened on the host
    assert err.startswith(pr.TERMS), err[:400]  # the owner saw the terms first (D-021)
    assert "MAS compute mode: host" in err
    s = started(state)
    assert s["compute_mode"] == "host" and s["profile"]["cpus"] >= 2 and s["profile"]["mem_bytes"] >= pf.MIN_HOST_MEM
    assert s["key_id"] == ap.key_id_of(built.key.public_key).hex()
    assert sorted(os.listdir(state)) == ["audit.log", "audit.log.lock"] or sorted(os.listdir(state)) == ["audit.log"]
    assert tree_hashes(built.medium) == before  # the medium is unchanged


def test_the_launcher_never_writes_to_the_medium_even_when_it_could(built, tmp_path):
    rw = tmp_path / "rw-medium"
    shutil.copytree(built.medium, rw)
    before = tree_hashes(rw)
    rc, err, _, _ = write_session(built, "rw", medium=rw, ro=False)
    assert rc == 0, err[-3000:]
    assert tree_hashes(rw) == before  # no bytecode, no state, no log: same files, same bytes


def test_the_launcher_needs_the_owners_arguments(built):
    for args in ([], ["--root", "/host"], ["--root", "/host", "--state", "/state"]):
        host, state = dirs(built, f"noargs-{len(args)}")
        rc, _ = Session(built, host, state, args=args).close()
        assert rc != 0, args
        assert os.listdir(state) == [], args


def test_the_launcher_refuses_a_medium_that_does_not_match_its_manifest(built, tmp_path):
    bad = tmp_path / "bad-medium"
    shutil.copytree(built.medium, bad)
    target = bad / "runtime" / "mas_device" / "gate.py"
    original = target.read_bytes()
    for name, tamper, undo in (
            ("edited", lambda: target.write_bytes(original + b"\n# tampered\n"), lambda: target.write_bytes(original)),
            ("added", lambda: (bad / "runtime" / "mas_device" / "evil.py").write_bytes(b"x = 1\n"),
             lambda: (bad / "runtime" / "mas_device" / "evil.py").unlink()),
            ("removed", lambda: target.unlink(), lambda: target.write_bytes(original))):
        tamper()
        try:
            host, state = dirs(built, f"tampered-{name}")
            s = Session(built, host, state, medium=bad)
            rc, err = s.close()
            assert rc != 0, name
            assert "ERROR" in err and "manifest" in err.lower(), (name, err[-600:])
            assert os.listdir(state) == [], name  # refused before the log was opened
        finally:
            undo()


# ---- the profiler -------------------------------------------------------------------------------------------------------------------------

def test_profiler_picks_device_mode_on_a_small_host_and_host_mode_on_a_big_one(built):
    rc, err, _, state = write_session(built, "small", extra=["--memory", "1g", "--cpus", "1"])
    assert rc == 0, err[-3000:]
    s = started(state)
    assert s["compute_mode"] == "device", s
    assert s["profile"]["mem_bytes"] <= 1 << 30 and s["profile"]["cpus"] == 1
    assert "MAS compute mode: device" in err
    rc, err, _, state = write_session(built, "big", extra=["--memory", "6g", "--cpus", "2"])
    assert rc == 0, err[-3000:]
    s = started(state)
    assert s["compute_mode"] == "host", s
    assert 5 << 30 <= s["profile"]["mem_bytes"] <= 6 << 30 and s["profile"]["cpus"] == 2


def _fake(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="ascii")
    return root


GIB = 1 << 30


def test_profiler_rules_on_fixed_machines(tmp_path):
    assert pf.MIN_HOST_MEM == 4 * GIB and pf.MIN_HOST_CPUS == 2
    meminfo = "MemTotal:       16384000 kB\nMemFree:         1000 kB\n"
    cases = {
        # name: (files, cpus seen by the OS, expected mem, expected cpus, expected mode)
        "big-unlimited-v2": ({"proc/meminfo": meminfo, "sys/fs/cgroup/memory.max": "max\n", "sys/fs/cgroup/cpu.max": "max 100000\n"},
                             8, 16384000 * 1024, 8, "host"),
        "v2-memory-limit": ({"proc/meminfo": meminfo, "sys/fs/cgroup/memory.max": str(2 * GIB) + "\n",
                             "sys/fs/cgroup/cpu.max": "max 100000\n"}, 8, 2 * GIB, 8, "device"),
        "v2-cpu-quota": ({"proc/meminfo": meminfo, "sys/fs/cgroup/memory.max": "max\n", "sys/fs/cgroup/cpu.max": "150000 100000\n"},
                         8, 16384000 * 1024, 2, "host"),
        "v2-one-cpu": ({"proc/meminfo": meminfo, "sys/fs/cgroup/memory.max": "max\n", "sys/fs/cgroup/cpu.max": "100000 100000\n"},
                       8, 16384000 * 1024, 1, "device"),
        "v1-memory-limit": ({"proc/meminfo": meminfo, "sys/fs/cgroup/memory/memory.limit_in_bytes": str(3 * GIB) + "\n"},
                            4, 3 * GIB, 4, "device"),
        "v1-no-limit": ({"proc/meminfo": meminfo, "sys/fs/cgroup/memory/memory.limit_in_bytes": "9223372036854771712\n"},
                        4, 16384000 * 1024, 4, "host"),
        "small-machine": ({"proc/meminfo": "MemTotal:        2000000 kB\n"}, 4, 2000000 * 1024, 4, "device"),
        "unknown-memory": ({}, 8, None, 8, "device"),
        "garbage": ({"proc/meminfo": "MemTotal: lots\n", "sys/fs/cgroup/memory.max": "-1\n", "sys/fs/cgroup/cpu.max": "x y\n"},
                    8, None, 8, "device"),
    }
    for name, (files, os_cpus, mem, cpus, mode) in cases.items():
        prof = pf.probe(_fake(tmp_path / name, files), os_cpus=os_cpus)
        assert prof["mem_bytes"] == mem, name
        assert prof["cpus"] == cpus, name
        got, reasons = pf.choose(prof)
        assert got == mode, (name, reasons)
        assert reasons and all(isinstance(r, str) for r in reasons), name
    gpu = pf.probe(_fake(tmp_path / "gpu", {"proc/meminfo": meminfo, "dev/nvidia0": "", "dev/dri/renderD128": ""}), os_cpus=8)
    assert gpu["gpu"] == ["nvidia", "dri"]
    assert pf.probe(_fake(tmp_path / "nogpu", {"proc/meminfo": meminfo}), os_cpus=8)["gpu"] == []
