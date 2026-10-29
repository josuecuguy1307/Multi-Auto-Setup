"""Worker entry point: probe, runtime (start/stop/status of the external inference server), uninstall."""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from pathlib import Path

from envrec_worker import __version__



def _arg(argv: list[str], name: str, default: str | None = None) -> str | None:
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default


def probe() -> dict:
    ram = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                ram = round(int(line.split()[1]) * 1024 / 2**30, 2)
    except OSError:
        pass
    limit = None
    for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            t = Path(p).read_text().strip()
            if t.isdigit() and int(t) < 2**50:
                limit = round(int(t) / 2**30, 2)
            break
        except OSError:
            continue
    return {"worker_version": __version__, "arch": platform.machine(), "os": platform.system(),
            "ram_reported_gib": ram, "ram_limit_gib": limit, "disk_free_gb": round(shutil.disk_usage("/").free / 1e9, 2)}


def uninstall(argv: list[str]) -> int:
    root = Path(_arg(argv, "--root", "")).resolve()
    if not str(root).startswith("/opt/envrec") or str(root) in ("/", "/opt"):
        print("refusing: root must be inside /opt/envrec", file=sys.stderr)
        return 2
    pid = root / "server.pid"
    if pid.is_file():
        try:
            os.kill(int(pid.read_text().strip()), 15)
        except (OSError, ValueError):
            pass
    shutil.rmtree(root, ignore_errors=True)
    print("removed")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    if cmd == "probe":
        print(json.dumps(probe()))
        return 0
    if cmd == "runtime":
        from envrec_worker.runtime import cli
        sub = argv[1] if len(argv) > 1 else ""
        fn = {"start": cli.start, "stop": cli.stop, "status": cli.status}.get(sub)
        if fn is None:
            print("usage: runtime start|stop|status --root DIR ...", file=sys.stderr)
            return 2
        return fn(argv[2:])
    if cmd == "uninstall":
        return uninstall(argv)
    print("usage: probe | runtime start|stop|status --root DIR ... | uninstall --root DIR", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
