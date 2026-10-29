"""Ring 0 probes: what is this machine? This machine only, read-only, no scanning.

Every probe returns a ProbeOut: values keyed by fact key (None = could not be determined),
the raw evidence text, and the provider name. Where two providers can answer the same key
(for example psutil and the OS's own command), they are separate probes so the store can
see two independent sources agree. Anything OS-specific that does not apply degrades to
None with a reason.
"""

from __future__ import annotations

import ipaddress
import json
import os
import platform
import shutil
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

GIB = 2**30
OS_NAMES = {"Darwin": "macOS"}
# Version probes only ever run these, with these arguments. Nothing else is executed.
TOOL_VERSION_CMDS: dict[str, list[str]] = {
    "python3": ["python3", "--version"],
    "git": ["git", "--version"],
    "node": ["node", "--version"],
    "docker": ["docker", "--version"],
    "ollama": ["ollama", "--version"],
    "uv": ["uv", "--version"],
    "ssh": ["ssh", "-V"],
}


@dataclass
class ProbeOut:
    values: dict[str, Any]
    raw: str
    provider: str
    limitations: list[str] = field(default_factory=list)
    reasons: dict[str, str] = field(default_factory=dict)  # key -> why it is None


def _run(argv: list[str], timeout: float = 5.0) -> str | None:
    """Run a fixed read-only command. Minimal environment; no shell."""
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False,
            env={"PATH": os.environ.get("PATH", ""), "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = (done.stdout or done.stderr or "").strip() if done.returncode == 0 else ""
    return out or None


def _os_release() -> dict[str, str]:
    path = Path("/etc/os-release")
    if not path.is_file():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(errors="replace").splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k] = v.strip().strip('"')
    return out


def _raw(**parts: Any) -> str:
    return json.dumps(parts, indent=2, sort_keys=True, default=str)


def _fill(values: dict[str, Any], reasons: dict[str, str], why: str) -> None:
    for key, value in values.items():
        if value is None:
            reasons.setdefault(key, why)


# ---- OS --------------------------------------------------------------------------------------

def read_os() -> ProbeOut:
    system = platform.system()
    if system == "Darwin":
        version = platform.mac_ver()[0] or None
    elif system == "Linux":
        version = _os_release().get("VERSION_ID") or None
    else:
        version = platform.release() or None
    values = {
        "os.name": OS_NAMES.get(system, system) or None,
        "os.version": version,
        "os.arch": platform.machine() or None,
        "os.kernel": platform.release() or None,
    }
    reasons: dict[str, str] = {}
    _fill(values, reasons, f"platform module has no value on {system or 'this OS'}")
    return ProbeOut(values, _raw(system=system, values=values), "platform", reasons=reasons)


def read_os_native() -> ProbeOut:
    system = platform.system()
    values: dict[str, Any] = {"os.name": None, "os.version": None, "os.arch": None, "os.kernel": None}
    commands: dict[str, str | None] = {}
    if system == "Darwin":
        commands["sw_vers -productVersion"] = values["os.version"] = _run(["sw_vers", "-productVersion"])
        values["os.name"] = "macOS" if values["os.version"] else None
    if system in ("Darwin", "Linux"):
        commands["uname -m"] = values["os.arch"] = _run(["uname", "-m"])
        commands["uname -r"] = values["os.kernel"] = _run(["uname", "-r"])
    if system == "Linux":
        commands["uname -s"] = uname_s = _run(["uname", "-s"])
        values["os.name"] = OS_NAMES.get(uname_s or "", uname_s)
    reasons: dict[str, str] = {}
    _fill(values, reasons, f"no native command is wired for {system or 'this OS'}")
    return ProbeOut(values, _raw(system=system, commands=commands), "native",
                    limitations=["uses the OS's own commands; only macOS and Linux are wired"],
                    reasons=reasons)


# ---- CPU, RAM, disks ------------------------------------------------------------------------

def read_cpu_ram() -> ProbeOut:
    values: dict[str, Any] = {
        "hw.cpu_cores_physical": psutil.cpu_count(logical=False),
        "hw.cpu_cores_logical": psutil.cpu_count(logical=True),
        "hw.ram_total_gib": round(psutil.virtual_memory().total / GIB, 2),
    }
    reasons: dict[str, str] = {}
    _fill(values, reasons, "psutil could not count them on this OS")
    return ProbeOut(values, _raw(values=values), "psutil", reasons=reasons)


def read_hw_native() -> ProbeOut:
    system = platform.system()
    values: dict[str, Any] = {
        "hw.cpu_model": None, "hw.cpu_cores_physical": None,
        "hw.cpu_cores_logical": None, "hw.ram_total_gib": None,
    }
    commands: dict[str, Any] = {}
    if system == "Darwin":
        values["hw.cpu_model"] = commands["machdep.cpu.brand_string"] = _run(
            ["sysctl", "-n", "machdep.cpu.brand_string"])
        for key, name in (("hw.cpu_cores_physical", "hw.physicalcpu"), ("hw.cpu_cores_logical", "hw.logicalcpu")):
            text = commands[name] = _run(["sysctl", "-n", name])
            values[key] = int(text) if text and text.isdigit() else None
        mem = commands["hw.memsize"] = _run(["sysctl", "-n", "hw.memsize"])
        values["hw.ram_total_gib"] = round(int(mem) / GIB, 2) if mem and mem.isdigit() else None
    elif system == "Linux":
        cpuinfo = Path("/proc/cpuinfo")
        if cpuinfo.is_file():
            for line in cpuinfo.read_text(errors="replace").splitlines():
                if line.lower().startswith(("model name", "hardware", "model\t")):
                    values["hw.cpu_model"] = line.partition(":")[2].strip() or None
                    break
        meminfo = Path("/proc/meminfo")
        if meminfo.is_file():
            for line in meminfo.read_text(errors="replace").splitlines():
                if line.startswith("MemTotal:"):
                    kb = line.split()[1]
                    values["hw.ram_total_gib"] = round(int(kb) * 1024 / GIB, 2) if kb.isdigit() else None
                    break
        commands["files"] = ["/proc/cpuinfo", "/proc/meminfo"]
    reasons: dict[str, str] = {}
    _fill(values, reasons, f"no native source is wired for {system or 'this OS'}")
    return ProbeOut(values, _raw(system=system, commands=commands), "native",
                    limitations=["Linux gives no physical core count from these files"] if system == "Linux" else [],
                    reasons=reasons)


def read_disks() -> ProbeOut:
    usage = shutil.disk_usage(Path.home())
    values = {
        "hw.disk_total_gib": round(usage.total / GIB, 2),
        "hw.disk_free_gib": round(usage.free / GIB, 2),
    }
    return ProbeOut(values, _raw(values=values, path="home volume"), "stdlib",
                    limitations=["home volume only", "free space changes while the run goes"])


# ---- battery, display, virtualization ----------------------------------------------------------

def read_battery() -> ProbeOut:
    if not hasattr(psutil, "sensors_battery"):
        values = {"hw.battery_present": None}
        return ProbeOut(values, _raw(values=values), "psutil",
                        reasons={"hw.battery_present": "psutil has no battery sensor on this OS"})
    battery = psutil.sensors_battery()
    values = {"hw.battery_present": battery is not None}
    return ProbeOut(values, _raw(values=values, percent_hidden=True), "psutil",
                    limitations=["no battery can also mean a desktop or a VM"])


def read_display() -> ProbeOut:
    system = platform.system()
    values: dict[str, Any] = {"hw.display_present": None}
    reasons: dict[str, str] = {}
    raw: dict[str, Any] = {"system": system}
    if system == "Darwin":
        text = _run(["system_profiler", "SPDisplaysDataType", "-json"], timeout=20.0)
        try:
            gpus = json.loads(text)["SPDisplaysDataType"] if text else None
        except (ValueError, KeyError, TypeError):
            gpus = None
        if gpus is None:
            reasons["hw.display_present"] = "system_profiler gave no usable output"
        else:
            count = sum(len(g.get("spdisplays_ndrvs", [])) for g in gpus if isinstance(g, dict))
            values["hw.display_present"] = count > 0
            raw["display_count"] = count
    elif system == "Linux":
        values["hw.display_present"] = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        raw["checked"] = "DISPLAY / WAYLAND_DISPLAY are set (values not stored)"
    else:
        reasons["hw.display_present"] = f"no display check is wired for {system or 'this OS'}"
    return ProbeOut(values, _raw(**raw), "native",
                    limitations=["a remote or headless session can hide a physical display"], reasons=reasons)


def read_virtualization() -> ProbeOut:
    system = platform.system()
    raw: dict[str, Any] = {"system": system}
    value: str | None = None
    reason = ""
    if Path("/.dockerenv").exists():
        value, raw["marker"] = "container", "/.dockerenv"
    elif system == "Linux":
        cgroup = Path("/proc/1/cgroup")
        text = cgroup.read_text(errors="replace") if cgroup.is_file() else ""
        if any(w in text for w in ("docker", "kubepods", "containerd", "lxc")):
            value, raw["marker"] = "container", "/proc/1/cgroup"
        else:
            product = Path("/sys/class/dmi/id/product_name")
            name = product.read_text(errors="replace").strip() if product.is_file() else ""
            raw["dmi_product"] = name
            hint = ("virtualbox", "vmware", "kvm", "qemu", "hyper-v", "virtual machine")
            value = "vm" if any(h in name.lower() for h in hint) else ("none" if name else None)
            reason = "no DMI product name readable" if value is None else ""
    elif system == "Darwin":
        flag = _run(["sysctl", "-n", "kern.hv_vmm_present"])
        raw["kern.hv_vmm_present"] = flag
        value = {"0": "none", "1": "vm"}.get(flag or "")
        reason = "" if value else "sysctl kern.hv_vmm_present unreadable"
    else:
        reason = f"no virtualization check is wired for {system or 'this OS'}"
    values = {"host.virtualization": value}
    return ProbeOut(values, _raw(**raw), "native",
                    limitations=["heuristic markers; a hardened VM can hide them"],
                    reasons={"host.virtualization": reason} if value is None else {})


# ---- tools and network ------------------------------------------------------------------------

def read_tool_versions() -> ProbeOut:
    versions: dict[str, str | None] = {}
    for name, argv in TOOL_VERSION_CMDS.items():
        if shutil.which(argv[0]) is None:
            versions[name] = None  # not installed
            continue
        out = _run(argv, timeout=8.0)
        versions[name] = (out.splitlines()[0][:80] if out else "installed (version unreadable)")
    values = {"host.tools": versions}
    return ProbeOut(values, _raw(versions=versions), "which",
                    limitations=["only a fixed allowlist of tools is asked for its version",
                                 "null means not found on PATH"])


def _usable_ip(entry: Any) -> bool:
    if entry.family not in (socket.AF_INET, socket.AF_INET6):
        return False
    try:
        ip = ipaddress.ip_address(entry.address.split("%")[0])
    except ValueError:
        return False
    return not (ip.is_loopback or ip.is_unspecified or ip.is_link_local)


def read_net_interfaces() -> ProbeOut:
    """This machine's own interfaces that hold an address. Names only: no addresses, no scanning."""
    names = sorted(n for n, entries in psutil.net_if_addrs().items() if any(_usable_ip(e) for e in entries))
    values = {"net.interfaces": names}
    return ProbeOut(values, _raw(values=values), "psutil",
                    limitations=["names only; addresses and other hosts are deliberately not recorded"])


# ---- classification (inference from facts already known) --------------------------------------

DESKTOP_OSES = {"macOS", "Windows", "Linux"}


def classify_host(known: dict[str, Any]) -> ProbeOut:
    """host.kind and host.capabilities from other facts. An inference, so host.kind is a hypothesis."""
    virt = known.get("host.virtualization")
    os_name, arch = known.get("os.name"), str(known.get("os.arch") or "")
    battery, display = known.get("hw.battery_present"), known.get("hw.display_present")
    tools = known.get("host.tools") or {}
    limitations: list[str] = []

    kind = "unknown"
    if virt == "container":
        kind = "container"
    elif virt == "vm":
        kind = "vm"
    elif os_name == "Linux" and Path("/proc/device-tree/model").is_file() and "raspberry" in (
        Path("/proc/device-tree/model").read_text(errors="replace").lower()
    ):
        kind = "sbc"
    elif os_name in DESKTOP_OSES and display is True:
        kind = "laptop"
        if battery is False:
            limitations.append("no battery: this may be a desktop; the vocabulary has no 'desktop'")
    elif os_name == "Linux" and arch.startswith(("arm", "aarch")) and display is False:
        kind = "sbc"
        limitations.append("headless ARM Linux is only a guess for a single-board computer")
    else:
        limitations.append("not enough facts to classify")

    caps = sorted(
        c for c, ok in {
            "battery": battery is True, "display": display is True,
            "network": bool(known.get("net.interfaces")),
            "docker_cli": bool(tools.get("docker")), "ollama": bool(tools.get("ollama")),
            "python": bool(tools.get("python3")), "git": bool(tools.get("git")),
            "node": bool(tools.get("node")), "ssh": bool(tools.get("ssh")),
        }.items() if ok
    )
    values = {"host.kind": kind, "host.capabilities": caps}
    if kind == "unknown":
        values["host.kind"] = None
    reasons = {"host.kind": "not enough facts to classify"} if kind == "unknown" else {}
    return ProbeOut(values, _raw(inputs=known, result=values), "rules",
                    limitations=limitations, reasons=reasons)
