"""The command policy for discovery: fixed argv lists, a program allowlist, the destructive-word denylist, no shell."""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml

# Read-only inspection programs only. Anything else is refused before it runs. Programs are listed by basename.
ALLOWED_PROGRAMS = frozenset({
    "system_profiler", "ioreg", "sw_vers", "uname", "sysctl", "ifconfig", "netstat", "route", "scutil", "networksetup",
    "arp", "ndp", "dns-sd", "lsof", "ps", "pmset", "diskutil", "df", "mount", "uptime", "locale", "hostname", "id",
    "docker", "podman", "prlctl", "VBoxManage", "utmctl", "ollama", "bluetoothctl", "lsusb", "lspci", "lsblk",
    "avahi-browse", "ip", "ss", "lscpu", "free", "cat", "ls", "dpkg", "rpm", "pacman", "flatpak", "snap", "brew", "getprop",
    "which", "readlink", "iwconfig", "iw", "nmcli", "resolvectl", "sensors", "ping",
    "launchctl", "defaults",
})
# Programs that run ONLY with exactly these argument lists (prefix match for the last entry marked with a trailing "*"). Nothing a device could be changed with.
RESTRICTED: dict[str, tuple[tuple[str, ...], ...]] = {
    "getprop": tuple((k,) for k in ("ro.product.model", "ro.product.manufacturer", "ro.product.name", "ro.build.version.release", "ro.build.version.sdk",
                                    "ro.product.cpu.abi", "ro.hardware", "ro.build.type")),
    "pm": (("list", "packages"), ("list", "packages", "-3")),
    "dumpsys": (("battery",), ("sensorservice",), ("wifi",)),
    "cmd": (("wifi", "status"),),
    "i2cdetect": (("-l",), ("-y", "0"), ("-y", "1"), ("-y", "2")),
    "vcgencmd": (("measure_temp",), ("get_throttled",), ("version",), ("measure_clock", "arm"), ("get_mem", "arm")),
    "gpioinfo": ((),),
}
# `cat` and `ls` read only these places: no file contents outside system facts, no directory listing outside the known bin/app/device dirs.
READ_PATHS = ("/etc/os-release", "/etc/hostname", "/etc/resolv.conf", "/proc/uptime", "/proc/cpuinfo", "/proc/meminfo", "/proc/loadavg", "/proc/net/",
              "/proc/device-tree/model", "/sys/class/hwmon/", "/sys/class/thermal/", "/sys/class/power_supply/", "/sys/bus/iio/", "/sys/bus/usb/devices/",
              "/sys/class/net/", "/sys/bus/i2c/", "/sys/class/gpio/", "/sys/firmware/devicetree/")
LIST_PATHS = ("/Applications", "/System/Applications", "/dev", "/usr/local/bin", "/opt/homebrew/bin", "/sys/", "/dev/serial/", "/dev/disk/")
# The same words the policy denylist uses, plus verbs no read-only discovery command needs.
DENY_WORDS = frozenset({"delete", "format", "flash", "elevate", "erase", "rm", "kill", "killall", "sudo", "su", "chmod", "chown",
                        "mkfs", "dd", "reboot", "shutdown", "install", "uninstall", "remove", "purge", "write"})
_SPLIT = re.compile(r"[^a-z0-9]+")


class CommandRefused(ValueError):
    pass


def _agents_yaml() -> dict:
    f = data_dir() / "agents.yaml"
    return (yaml.safe_load(f.read_text(encoding="utf-8")) or {}) if f.is_file() else {}


def agent_paths() -> tuple[str, ...]:
    """Config directories from data/agents.yaml (existence check only: `ls -d`) and MCP config files (read by the adapter itself)."""
    y = _agents_yaml()
    dirs = [str(Path(d).expanduser()) for a in y.get("agents", []) for d in a.get("config_dirs", [])]
    return tuple(dict.fromkeys(dirs))


def mcp_config_paths() -> tuple[str, ...]:
    return tuple(str(Path(m["path"]).expanduser()) for m in _agents_yaml().get("mcp_configs", []))


def user_bin_dirs() -> tuple[str, ...]:
    h = str(Path.home())
    return (f"{h}/.local/bin", f"{h}/bin", f"{h}/.cargo/bin", f"{h}/go/bin", f"{h}/Applications")


def data_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "data"


_VERSION_PROBES: dict[str, set[tuple[str, ...]]] | None = None


def version_probes() -> dict[str, set[tuple[str, ...]]]:
    """Programs from data/tools.yaml, each allowed ONLY with its own fixed version arguments."""
    global _VERSION_PROBES
    if _VERSION_PROBES is None:
        _VERSION_PROBES = {}
        f = data_dir() / "tools.yaml"
        if f.is_file():
            for t in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).get("tools", []):
                _VERSION_PROBES.setdefault(t["bin"], set()).add(tuple(t.get("args") or []))
    return _VERSION_PROBES


def validate_argv(argv: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """Raise CommandRefused unless this is a plain argv list that is allowed. Returns it as a tuple."""
    if not argv or not all(isinstance(a, str) and a and "\x00" not in a for a in argv):
        raise CommandRefused("a command must be a non-empty list of non-empty strings")
    prog = os.path.basename(argv[0])
    probes = version_probes()
    if prog in RESTRICTED:
        if tuple(argv[1:]) not in RESTRICTED[prog]:
            raise CommandRefused(f"{prog!r} may only run with its fixed read-only arguments")
        return tuple(argv)
    if prog in probes and prog not in ALLOWED_PROGRAMS:
        if tuple(argv[1:]) not in probes[prog]:
            raise CommandRefused(f"{prog!r} may only run with its fixed version arguments")
        return tuple(argv)
    if prog not in ALLOWED_PROGRAMS:
        raise CommandRefused(f"{prog!r} is not an allowed discovery program")
    if prog in ("cat", "ls", "readlink"):
        paths = [a for a in argv[1:] if not a.startswith("-")]
        allowed = READ_PATHS if prog in ("cat", "readlink") else LIST_PATHS + user_bin_dirs() + agent_paths()
        if not paths or not all(any(p == a.rstrip("/") or p.startswith(a if a.endswith("/") else a + "/") or p == a for a in allowed) for p in paths):
            raise CommandRefused(f"{prog} may only read fixed system and inventory paths, not {paths}")
    for token in argv:
        words = set(_SPLIT.split(token.lower()))
        hit = words & DENY_WORDS
        if hit and not token.startswith(("/", "-")):  # options like --format name a style, they are not verbs
            raise CommandRefused(f"argument {token!r} contains the denied word {sorted(hit)[0]!r}")
    return tuple(argv)
