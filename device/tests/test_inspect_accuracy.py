"""P3-INSPECT: inventory precision and recall against fixtures with known truth meet the thresholds pinned in criteria.yaml (D-038).

Fixture machines are containers of the pinned Debian image (no network), each prepared with traps (packages built and managed with
dpkg in every state that matters, a tricky os-release, a small machine). Right before the inspection, in the same container, the
truth is taken with tools that do NOT read what the inspector reads:

    packages    dpkg-query (dpkg's own parser)              inspector: its own parser of /var/lib/dpkg/status
    kernel      uname -r (a system call)                     inspector: /proc/version
    cpus        getconf _NPROCESSORS_CONF (sysconf)          inspector: /proc/cpuinfo
    memory      getconf _PHYS_PAGES * PAGESIZE (sysinfo)     inspector: /proc/meminfo
    modules     /sys/module/*/initstate                      inspector: /proc/modules
    interfaces  ioctl SIOCGIFHWADDR / SIOCGIFMTU             inspector: /sys/class/net
    disks       sizes from /proc/partitions                  inspector: /sys/block/*/size
    os          the shell sourcing os-release                inspector: its own os-release parser

Not scored (no independent source here): DMI, apk (no Alpine image pulled). Precision = reported facts that are true / reported facts;
recall = true facts reported / true facts. SIMULATED-label criterion; the machines themselves are containers.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

import mas_device
from mas_device import approval as ap
from mas_device import inspect as ins
from mas_device.keymodel import KeyModel

SRC = Path(mas_device.__file__).resolve().parents[1]  # the tree under test
TREE = SRC.parent
sys.path[:0] = [str(TREE / "device" / "medium"), str(Path(__file__).resolve().parent)]
import mas_dockerkit as dk  # noqa: E402
import mas_medium_build as md  # noqa: E402

LOCK = TREE / "device" / "medium" / "medium.lock.json"
CRITERIA = TREE / "device" / "acceptance" / "criteria.yaml"

TRUTH_SH = r"""set -eu
. /usr/lib/os-release
printf 'os\tid\t%s\n' "${ID:-}"
printf 'os\tversion_id\t%s\n' "${VERSION_ID:-}"
printf 'os\tpretty_name\t%s\n' "${PRETTY_NAME:-}"
printf 'os\tkernel_release\t%s\n' "$(uname -r)"
printf 'os\thostname\t%s\n' "$(uname -n)"
printf 'cpu\tcount\t%s\n' "$(getconf _NPROCESSORS_CONF)"
printf 'memory\ttotal_bytes\t%s\n' "$(( $(getconf _PHYS_PAGES) * $(getconf PAGESIZE) ))"
dpkg-query -W -f='${db:Status-Status}\t${Package}\t${Version}\t${Architecture}\n' | awk -F'\t' '$1=="installed"{print "pkg\t"$2"\t"$3"\t"$4}'
for m in /sys/module/*; do [ -f "$m/initstate" ] && printf 'module\t%s\n' "${m##*/}"; done
for d in $(ls /sys/block); do awk -v d="$d" 'NR>2 && $4==d && $3>0 {printf "block\t%s\t%.0f\n", $4, $3*1024}' /proc/partitions; done
/media/mas/runtime/python/bin/python3.13 -I -B /tools/truth_net.py
"""

TRUTH_NET = r"""import fcntl, socket, struct
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for _, name in socket.if_nameindex():
    req = struct.pack("16s16x", name.encode()[:15])
    mac = fcntl.ioctl(s.fileno(), 0x8927, req)[18:24]          # SIOCGIFHWADDR
    mtu = struct.unpack("i", fcntl.ioctl(s.fileno(), 0x8921, req)[16:20])[0]  # SIOCGIFMTU
    print("net\t%s\t%s\t%d" % (name, ":".join("%02x" % b for b in mac), mtu))
"""

MKPKG = r"""mk() { d=/tmp/pkg-$1; mkdir -p $d/DEBIAN $d/etc/masfix
  printf 'Package: %s\nVersion: %s\nArchitecture: %s\nMaintainer: fixture <f@example.invalid>\nDescription: fixture %s\n a second line\n .\n Status: install ok installed\n' "$1" "$2" "$3" "$1" > $d/DEBIAN/control
  echo x > $d/etc/masfix/$1.conf; echo /etc/masfix/$1.conf > $d/DEBIAN/conffiles
  dpkg-deb --build $d /tmp/$1.deb >/dev/null; }
mk mas-fix-all 1.0 all && dpkg -i /tmp/mas-fix-all.deb >/dev/null
mk mas-fix-epoch 2:1.0~rc1-3 amd64 && dpkg -i /tmp/mas-fix-epoch.deb >/dev/null
mk mas-fix-held 0.9 amd64 && dpkg -i /tmp/mas-fix-held.deb >/dev/null && echo 'mas-fix-held hold' | dpkg --set-selections
mk mas-fix-removed 0.1 amd64 && dpkg -i /tmp/mas-fix-removed.deb >/dev/null && dpkg -r mas-fix-removed >/dev/null
mk mas-fix-unpacked 0.2 amd64 && dpkg --unpack /tmp/mas-fix-unpacked.deb >/dev/null
"""

TRICKY_OS = r"""cat > /usr/lib/os-release <<'EOF'
NAME='Fixture Linux'
ID=fixture
ID_LIKE="debian"
VERSION_ID="12.5"
PRETTY_NAME="Fixture \"Quoted\" Linux 12.5 \$HOME \\ end"
# a comment line
HOME_URL="https://example.invalid/"
EOF
"""

MACHINES = {
    "stock": {"setup": "true", "flags": []},
    "packages": {"setup": MKPKG, "flags": []},
    "tricky-os": {"setup": TRICKY_OS, "flags": []},
    "small": {"setup": "true", "flags": ["--hostname", "fixture-small", "--cpus", "1", "--memory", "1g"]},
}


def thresholds() -> dict:
    return yaml.safe_load(CRITERIA.read_text(encoding="utf-8"))["thresholds"]["P3-INSPECT"]


def facts_of_report(c: dict) -> set[tuple]:
    out = set()
    for k in ("id", "version_id", "pretty_name", "kernel_release", "hostname"):
        if c["os"].get(k) is not None:
            out.add(("os", k, c["os"][k]))
    if c["cpu"]["count"] is not None:
        out.add(("cpu", "count", str(c["cpu"]["count"])))
    if c["memory"]["total_bytes"] is not None:
        out.add(("memory", "total_bytes", str(c["memory"]["total_bytes"])))
    out |= {("pkg", p["name"], p["version"], p["arch"]) for p in c["packages_dpkg"]}
    out |= {("module", m) for m in c["kernel_modules"]}
    out |= {("block", b["name"], str(b["bytes"])) for b in c["block_devices"]}
    out |= {("net", n["name"], n["mac"], str(n["mtu"])) for n in c["net_interfaces"]}
    return out


def facts_of_truth(text: str) -> set[tuple]:
    out = set()
    for line in text.splitlines():
        parts = tuple(line.split("\t"))
        if parts[0] == "os" and parts[2] == "":
            continue  # a field the os-release does not have
        out.add(parts)
    return out


def score(reported: set, truth: set) -> tuple[float, float]:
    hit = len(reported & truth)
    return (hit / len(reported) if reported else 0.0), (hit / len(truth) if truth else 0.0)


class Tick:
    def __init__(self, t: float = 100.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def press(key: KeyModel, clock: Tick, kr: dict) -> str:
    req = ap.KeyRequest(*(bytes.fromhex(kr[n]) for n in ("session_id", "nonce", "action_hash", "undo_hash", "summary")),
                        kr["cls"], kr["press_window_s"])
    h = key.request(req)
    clock.t += 2
    key.button.press()
    return key.sign(h).hex()


# ---- pure: the scoring itself and the pinned thresholds ----------------------------------------------------------------------------------

def test_the_thresholds_are_the_pinned_ones():
    assert thresholds() == {"precision_min": 0.95, "recall_min": 0.90}


def test_scoring_counts_what_it_says():
    t = {("pkg", "a", "1", "all"), ("pkg", "b", "2", "amd64"), ("os", "id", "debian"), ("cpu", "count", "2")}
    assert score(t, t) == (1.0, 1.0)
    assert score({("pkg", "a", "1", "all")}, t) == (1.0, 0.25)
    assert score(t | {("pkg", "ghost", "0", "all")}, t) == (0.8, 1.0)
    assert score(set(), t) == (0.0, 0.0)
    assert facts_of_truth("os\tid\tdebian\nos\tversion_id\t\npkg\ta\t1\tall\n") == {("os", "id", "debian"), ("pkg", "a", "1", "all")}


def test_the_os_release_parser_follows_the_shell_rules(tmp_path):
    """os-release(5): optional single or double quotes; inside double quotes, backslash escapes $ " \\ and `."""
    root = tmp_path / "h"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "os-release").write_text(TRICKY_OS.split("<<'EOF'\n", 1)[1].rsplit("EOF\n", 1)[0], encoding="ascii")
    c = ins.c_os(ins.Reader(root))
    assert c["id"] == "fixture" and c["version_id"] == "12.5"
    assert c["pretty_name"] == 'Fixture "Quoted" Linux 12.5 $HOME \\ end'


def test_the_truth_never_reads_what_the_inspector_reads():
    for src in ("/proc/cpuinfo", "/proc/meminfo", "dpkg/status", "/proc/modules", "/sys/class/net", "/proc/version", "/sys/block/"):
        assert src not in TRUTH_SH and src not in TRUTH_NET, src


# ---- the fixture machines ------------------------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def runs():
    dk.docker_ok()
    root = TREE / "runs" / "inspect-accuracy-tests"
    root.mkdir(parents=True, exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="run-", dir=root))
    clock = Tick()
    key = KeyModel.create(base / "key", clock=clock)
    medium = md.build(base / "medium", cache=dk.medium_cache(TREE), key_pub=key.public_key, tree=TREE)
    lab = dk.Lab(json.loads(LOCK.read_text(encoding="utf-8"))["base_image"], medium)
    tools = base / "tools"
    tools.mkdir()
    (tools / "truth.sh").write_bytes(TRUTH_SH.encode("ascii"))
    (tools / "truth_net.py").write_bytes(TRUTH_NET.encode("ascii"))
    results = {}
    try:
        for name, m in MACHINES.items():
            state = base / f"state-{name}"
            state.mkdir()
            (tools / f"setup-{name}.sh").write_bytes(m["setup"].encode("ascii"))
            cmd = (f"sh /tools/setup-{name}.sh && sh /tools/truth.sh > /state/truth.tsv && "
                   f"exec sh {dk.MOUNT}/launch.sh inspect --root / --state /state --target fix-{name}")
            mach = dk.Machine(lab, f"acc-{name}", ["sh", "-c", cmd], rm=True, flags=m["flags"],
                              mounts=["-v", f"{state}:/state", "-v", f"{tools}:/tools:ro"])
            while (msg := mach.line()) is not None:
                if "key_request" in msg:
                    mach.send({"token": press(key, clock, msg["key_request"])})
            rc, err, _ = mach.finish()
            assert rc == 0, (name, err[-3000:])
            report = json.loads(next(state.glob("inspect-*.json")).read_bytes())
            truth = facts_of_truth((state / "truth.tsv").read_text(encoding="utf-8"))
            reported = facts_of_report(report["collectors"])
            results[name] = {"reported": reported, "truth": truth, "report": report}
        out = TREE / "runs" / "eval"
        out.mkdir(parents=True, exist_ok=True)
        (out / "inspect-accuracy.json").write_text(json.dumps(
            {n: {"precision": score(r["reported"], r["truth"])[0], "recall": score(r["reported"], r["truth"])[1],
                 "reported": len(r["reported"]), "truth": len(r["truth"]),
                 "false_positives": sorted(map(list, r["reported"] - r["truth"])), "misses": sorted(map(list, r["truth"] - r["reported"]))}
             for n, r in results.items()}, indent=1), encoding="utf-8")
        yield results
    finally:
        lab.close()
        shutil.rmtree(base, ignore_errors=True)


def test_precision_and_recall_meet_the_thresholds_on_every_machine(runs):
    th = thresholds()
    for name, r in runs.items():
        p, rc = score(r["reported"], r["truth"])
        assert len(r["truth"]) > 50, (name, len(r["truth"]))  # a real inventory, not a handful of facts
        assert p >= th["precision_min"] and rc >= th["recall_min"], (name, p, rc, sorted(r["reported"] - r["truth"])[:10],
                                                                    sorted(r["truth"] - r["reported"])[:10])


def test_every_kind_of_fact_is_scored(runs):
    kinds = {f[0] for r in runs.values() for f in r["truth"]}
    # loadable kernel modules exist only if the (shared) kernel has any; every other kind must be present and scored
    assert {"os", "cpu", "memory", "pkg", "block", "net"} <= kinds <= {"os", "cpu", "memory", "pkg", "module", "block", "net"}, kinds


def test_package_states_are_reported_exactly(runs):
    r = runs["packages"]
    mine = {f for f in r["reported"] if f[0] == "pkg" and f[1].startswith("mas-fix-")}
    true = {f for f in r["truth"] if f[0] == "pkg" and f[1].startswith("mas-fix-")}
    assert true == {("pkg", "mas-fix-all", "1.0", "all"), ("pkg", "mas-fix-epoch", "2:1.0~rc1-3", "amd64"),
                    ("pkg", "mas-fix-held", "0.9", "amd64")}  # removed (rc) and unpacked-only are not installed
    assert mine == true
    assert {f for f in r["reported"] if f[0] == "pkg"} == {f for f in r["truth"] if f[0] == "pkg"}


def test_os_kernel_cpu_memory_and_disks_are_exact(runs):
    for name, r in runs.items():
        for kind in ("os", "cpu", "memory", "block", "net", "module"):
            mine = {f for f in r["reported"] if f[0] == kind}
            true = {f for f in r["truth"] if f[0] == kind}
            assert mine == true, (name, kind, sorted(mine ^ true))
    assert ("os", "pretty_name", 'Fixture "Quoted" Linux 12.5 $HOME \\ end') in runs["tricky-os"]["reported"]
    assert ("os", "hostname", "fixture-small") in runs["small"]["reported"]
