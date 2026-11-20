"""Read-only inspection of the host (MISSION 4.1 rules 2, 9, 10; D-011, D-012, D-037). Criterion P3-READONLY.

    sh <medium>/launch.sh inspect --root / --state <device folder> --target <this host's name>

One INSPECT_SESSION press authorises one read-only session (D-012); nothing is read before it. Every collector reads through one Reader
that accepts only the paths in ALLOWED_FILES / ALLOWED_DIRS (relative to --root, one "*" = one plain path segment): no credential
store, no serial number, no process environment, nothing a user wrote. Outside /proc and /sys a link is refused, so a host cannot point
an allowed name at a secret. The registry is closed and has no network collector (no lan sweep, no port or BLE scan, D-011); this module
makes no network connection and runs no command. The report (and the list of every path read) goes to the device's own folder, never to
the host.

SIMULATED key; EMULATED host (a clean container) in the tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from mas_device import approval as ap
from mas_device import pairing as pr
from mas_device import redact as rd
from mas_device import report as rp
from mas_device.log import AuditLog

ALLOWED_FILES = (
    "etc/os-release", "usr/lib/os-release", "etc/hostname", "proc/version", "proc/cpuinfo", "proc/meminfo", "proc/modules",
    "var/lib/dpkg/status", "lib/apk/db/installed", "usr/lib/apk/db/installed",  # merged-usr: /lib -> usr/lib
    "sys/block/*/size", "sys/block/*/removable", "sys/block/*/device/model",
    "sys/class/net/*/address", "sys/class/net/*/mtu",
    "sys/class/dmi/id/sys_vendor", "sys/class/dmi/id/product_name", "sys/class/dmi/id/product_version",
    "sys/class/dmi/id/bios_vendor", "sys/class/dmi/id/bios_version",
)
ALLOWED_DIRS = ("sys/block", "sys/class/net")
PSEUDO = ("proc/", "sys/")  # kernel pseudo-filesystems: their links are the kernel's own
MAX_READ = 32 << 20
_PLAIN = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


class Forbidden(Exception):
    pass


class Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _match(rel: str, pattern: str) -> bool:
    a, b = rel.split("/"), pattern.split("/")
    return len(a) == len(b) and all(p == "*" and _PLAIN.fullmatch(s) is not None or p == s for s, p in zip(a, b))


def _plain(rel) -> bool:
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel:
        return False
    return all(p not in ("", ".", "..") for p in rel.split("/"))


def allowed(rel) -> bool:
    return _plain(rel) and (any(_match(rel, p) for p in ALLOWED_FILES) or rel in ALLOWED_DIRS)


class Reader:
    """The only way a collector sees the host. `sources` lists every allowed path asked for, in order."""

    def __init__(self, root: Path) -> None:
        self.root = os.path.realpath(root)
        self.sources: list[str] = []
        self.refused: list[str] = []  # allowed names the host made into links to something else: never read, listed in the report

    def _path(self, rel: str) -> str:
        """Outside /proc and /sys a link is followed only to another allowed file under the root (Debian's etc/os-release ->
        ../usr/lib/os-release); a link to anything else (etc/shadow, a home folder, outside the root) is refused."""
        path = os.path.join(self.root, *rel.split("/"))
        if rel.startswith(PSEUDO):
            return path
        real = os.path.realpath(path)
        if real == os.path.normpath(path):
            return path
        try:
            inside = os.path.relpath(real, self.root)
        except ValueError:
            inside = ".."
        target = inside.replace(os.sep, "/")
        if target.startswith("..") or not (_plain(target) and any(_match(target, p) for p in ALLOWED_FILES)):
            raise Forbidden(f"{rel}: a link to something not on the allowlist")
        return real

    def read(self, rel: str) -> bytes | None:
        if not (_plain(rel) and any(_match(rel, p) for p in ALLOWED_FILES)):
            raise Forbidden(f"{rel!r} is not on the inspection allowlist")
        self.sources.append(rel)
        path = os.path.join(self.root, *rel.split("/"))
        if not os.path.lexists(path):
            return None
        try:
            path = self._path(rel)
        except Forbidden:
            self.refused.append(rel)
            return None
        try:
            with open(path, "rb") as f:
                return f.read(MAX_READ)
        except (FileNotFoundError, NotADirectoryError, PermissionError, IsADirectoryError):
            return None
        except OSError:
            return None

    def listdir(self, rel: str) -> list[str] | None:
        if rel not in ALLOWED_DIRS:
            raise Forbidden(f"{rel!r} is not a listable folder")
        self.sources.append(rel)
        try:
            return sorted(n for n in os.listdir(self._path(rel)) if _PLAIN.fullmatch(n))
        except OSError:
            return None


def _cap(s: str | None, n: int) -> str | None:
    """Host text is cut to the report schema's limits, so one absurd value never voids the whole inspection (D-039)."""
    return None if s is None else s[:n]


def _text(b: bytes | None, limit: int = 200) -> str | None:
    if b is None:
        return None
    return b.decode("utf-8", "replace").strip()[:limit]


def _shell_value(v: str) -> str:
    """os-release(5) values follow shell quoting: '...' is literal; inside "..." a backslash escapes $ " \\ and ` (and is kept
    before anything else); an unquoted value is taken as written."""
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1]
    if len(v) >= 2 and v[0] == v[-1] == '"':
        s, out, i = v[1:-1], [], 0
        while i < len(s):
            if s[i] == "\\" and i + 1 < len(s) and s[i + 1] in '$"\\`':
                out.append(s[i + 1])
                i += 2
            else:
                out.append(s[i])
                i += 1
        return "".join(out)
    return v


def _kv(b: bytes | None) -> dict[str, str]:
    out = {}
    for line in (b or b"").decode("utf-8", "replace").splitlines():
        if line.lstrip().startswith("#"):
            continue
        k, sep, v = line.partition("=")
        if sep and re.fullmatch(r"[A-Z][A-Z0-9_]*", k):
            out[k] = _shell_value(v)[:200]
    return out


def c_os(r: Reader) -> dict:
    rel = r.read("etc/os-release")
    if rel is None:
        rel = r.read("usr/lib/os-release")
    kv = _kv(rel)
    m = re.match(r"Linux version (\S+)", _text(r.read("proc/version")) or "")
    return {"id": kv.get("ID"), "version_id": kv.get("VERSION_ID"), "pretty_name": kv.get("PRETTY_NAME"),
            "hostname": _text(r.read("etc/hostname"), 64), "kernel_release": m.group(1)[:100] if m else None}


def c_cpu(r: Reader) -> dict:
    lines = (r.read("proc/cpuinfo") or b"").decode("utf-8", "replace").splitlines()
    models = [ln.split(":", 1)[1].strip() for ln in lines if ln.startswith("model name") and ":" in ln]
    return {"count": sum(1 for ln in lines if re.match(r"processor\s*:", ln)), "model": models[0][:120] if models else None}


def c_memory(r: Reader) -> dict:
    m = re.search(rb"^MemTotal:\s+(\d+) kB\s*$", r.read("proc/meminfo") or b"", re.MULTILINE)
    return {"total_bytes": int(m.group(1)) * 1024 if m else None}


def c_kernel_modules(r: Reader) -> list[str]:
    return sorted({ln.split()[0][:64] for ln in (r.read("proc/modules") or b"").decode("ascii", "replace").splitlines() if ln.split()})


def _stanzas(b: bytes | None) -> list[dict[str, str]]:
    out, cur = [], {}
    for line in (b or b"").decode("utf-8", "replace").splitlines():
        if not line.strip():
            if cur:
                out.append(cur)
            cur = {}
        elif not line[0].isspace() and ":" in line:
            k, _, v = line.partition(":")
            cur[k.strip()] = v.strip()
    if cur:
        out.append(cur)
    return out


def c_packages_dpkg(r: Reader) -> list[dict]:
    pk = [{"name": s["Package"][:200], "version": _cap(s.get("Version"), 200), "arch": _cap(s.get("Architecture"), 64)}
          for s in _stanzas(r.read("var/lib/dpkg/status")) if "Package" in s and s.get("Status", "").endswith(" installed")]
    return sorted(pk, key=lambda p: p["name"])


def c_packages_apk(r: Reader) -> list[dict]:
    pk, cur = [], {}
    for line in (r.read("lib/apk/db/installed") or b"").decode("utf-8", "replace").splitlines() + [""]:
        if not line.strip():
            if "P" in cur:
                pk.append({"name": cur["P"][:200], "version": _cap(cur.get("V"), 200), "arch": _cap(cur.get("A"), 64)})
            cur = {}
        elif len(line) > 2 and line[1] == ":":
            cur[line[0]] = line[2:].strip()
    return sorted(pk, key=lambda p: p["name"])


def c_block_devices(r: Reader) -> list[dict]:
    out = []
    for name in r.listdir("sys/block") or []:
        size = _text(r.read(f"sys/block/{name}/size"))
        if not (size and size.isdigit() and int(size) > 0):
            continue  # an unbound loop device or an empty slot is not storage
        removable = _text(r.read(f"sys/block/{name}/removable"))
        out.append({"name": name, "bytes": int(size) * 512, "removable": removable == "1",
                    "model": _text(r.read(f"sys/block/{name}/device/model"), 64)})
    return out


def c_net_interfaces(r: Reader) -> list[dict]:
    out = []
    for name in r.listdir("sys/class/net") or []:
        mtu = _text(r.read(f"sys/class/net/{name}/mtu"))
        out.append({"name": name, "mac": _text(r.read(f"sys/class/net/{name}/address"), 32), "mtu": int(mtu) if mtu and mtu.isdigit() else None})
    return out


def c_dmi(r: Reader) -> dict:
    out = {}
    for k in ("sys_vendor", "product_name", "product_version", "bios_vendor", "bios_version"):  # never serials or UUIDs
        v = _text(r.read(f"sys/class/dmi/id/{k}"), 80)
        if v:
            out[k] = v
    return out


REGISTRY = {"os": c_os, "cpu": c_cpu, "memory": c_memory, "kernel_modules": c_kernel_modules, "packages_dpkg": c_packages_dpkg,
            "packages_apk": c_packages_apk, "block_devices": c_block_devices, "net_interfaces": c_net_interfaces, "dmi": c_dmi}


def run_collectors(r: Reader) -> dict:
    return {name: fn(r) for name, fn in REGISTRY.items()}


class Session:
    """One INSPECT_SESSION approval, then one read-only pass of every collector, then one report in the device's folder."""

    def __init__(self, *, pairing: Path, state: Path, target: str) -> None:
        if not isinstance(target, str) or not _NAME.fullmatch(target):
            raise ValueError("target is a plain name")
        self.paired = pr.load(pairing)
        self.state, self.target = Path(state), target
        _, session = pr.new_session(self.paired)
        self.approvals = ap.Gate({self.paired.key_id: self.paired.key_pub}, session=session)  # the paired key only
        self.log = AuditLog.open(self.state / "audit.log")
        self.reader: Reader | None = None
        self.action: dict | None = None

    def begin(self, root: Path) -> dict:
        self.reader = Reader(root)  # nothing is read yet
        self.action = {"kind": "inspect_session", "target": self.target, "collectors": sorted(REGISTRY)}
        p = self.approvals.begin(self.action, ap.Class.INSPECT_SESSION)
        self.log.append("approval.requested", {"class": "INSPECT_SESSION", "target": self.target, "action_hash": p.action_hash.hex(),
                                               "summary": p.summary.decode("ascii").rstrip()})
        return {"session_id": p.session.hex(), "nonce": p.nonce.hex(), "action_hash": p.action_hash.hex(), "undo_hash": p.undo_hash.hex(),
                "summary": p.summary.hex(), "cls": int(p.cls), "press_window_s": p.press_window_s}

    def finish(self, token_hex) -> Path:
        try:
            token = bytes.fromhex(token_hex) if isinstance(token_hex, str) else None
        except ValueError:
            token = None
        try:
            if token is None or self.action is None:
                raise ap.Rejected("malformed")
            a = self.approvals.consume(token, self.action, cls=ap.Class.INSPECT_SESSION)
        except ap.Rejected as exc:
            self.log.append("inspect.refused", {"target": self.target, "reason": exc.code})
            raise Refused(exc.code) from None
        collectors = run_collectors(self.reader)
        collectors, _ = rd.redact(collectors)  # D-040: credential-like host text is withheld before anything is stored
        report = {"v": 1, "target": self.target, "key_id": a.key_id.hex(), "counter": a.counter, "collectors": collectors,
                  "sources": list(self.reader.sources), "refused": list(self.reader.refused)}
        problems = rp.validate(report, rp.SCHEMA)
        view = rp.agent_view(report) if not problems else None
        if view is not None:
            problems = rp.validate(view, rp.AGENT_SCHEMA)
        if problems:  # D-039: a report that breaks its schema is never written
            self.log.append("inspect.refused", {"target": self.target, "reason": "report_invalid", "detail": problems[0][:200]})
            raise Refused("report_invalid")
        stem = f"{self.target}-{a.counter}"
        blobs = {f"inspect-{stem}.json": (json.dumps(report, sort_keys=True, indent=1, ensure_ascii=True) + "\n").encode("ascii"),
                 f"inspect-{stem}.md": rp.owner_text(report).encode("utf-8"),
                 f"agentview-{stem}.json": (json.dumps(view, sort_keys=True, indent=1, ensure_ascii=True) + "\n").encode("ascii")}
        for name, data in blobs.items():
            fd = os.open(self.state / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        sha = {name: hashlib.sha256(data).hexdigest() for name, data in blobs.items()}
        self.log.append("inspect.done", {"target": self.target, "report": f"inspect-{stem}.json", "counter": a.counter,
                                         "report_sha256": sha[f"inspect-{stem}.json"], "owner_text_sha256": sha[f"inspect-{stem}.md"],
                                         "agent_view_sha256": sha[f"agentview-{stem}.json"], "sources": report["sources"]})
        return self.state / f"inspect-{stem}.json"


def main(argv: list[str], *, pairing: str) -> int:
    p = argparse.ArgumentParser(prog="mas inspect", description="Read-only inspection of this computer, started by its owner.")
    for name in ("--root", "--state", "--target"):
        p.add_argument(name, required=True)
    a = p.parse_args(argv)
    if not Path(a.state).is_dir():
        p.error("--state is the device's existing folder")
    s = Session(pairing=Path(pairing), state=Path(a.state), target=a.target)
    sys.stderr.write("MAS inspection (read-only): one press approves reading only these system files: "
                     + ", ".join(ALLOWED_FILES) + ". Nothing is written to this computer; the report stays on the device.\n")
    sys.stderr.flush()
    sys.stdout.write(json.dumps({"key_request": s.begin(Path(a.root))}) + "\n")
    sys.stdout.flush()
    line = sys.stdin.buffer.readline()
    try:
        msg = json.loads(line) if line else {}
        token = msg.get("token") if isinstance(msg, dict) else None
        out = s.finish(token)
    except (Refused, ValueError) as exc:
        sys.stderr.write(f"ERROR inspection refused: {exc}\n")
        sys.stdout.write(json.dumps({"status": "refused"}) + "\n")
        return 3
    sys.stdout.write(json.dumps({"status": "done", "report": out.name}) + "\n")
    sys.stdout.flush()
    return 0
