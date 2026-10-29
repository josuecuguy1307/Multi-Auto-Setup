"""`runtime start | stop | status`: what the controller's fixed command templates call on the device."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

from envrec_worker.runtime.spec import RuntimeSpec, SpecError
from envrec_worker.runtime.supervisor import RuntimeFailed, Supervisor


def _arg(argv: list[str], name: str, default: str | None = None) -> str | None:
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default


def _write_status(path: Path, **fields) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"ts": time.time(), **fields}))
    os.replace(tmp, path)


def _pid_alive(pid: int) -> bool:
    """Alive means running, not a zombie: a killed process nobody reaped (PID 1 may be a plain `sleep`) is dead."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        return state not in ("Z", "X")
    except (OSError, IndexError):
        return True  # no /proc (not Linux): fall back to the signal check above


def start(argv: list[str]) -> int:
    root = Path(_arg(argv, "--root", "")).resolve()
    status_file, pidfile = root / "runtime.status.json", root / "server.pid"
    try:
        spec = RuntimeSpec(
            kind=_arg(argv, "--kind", ""), binary=_arg(argv, "--binary", ""), model=_arg(argv, "--model", ""),
            port=int(_arg(argv, "--port", "0")), threads=int(_arg(argv, "--threads", "0")),
            context=int(_arg(argv, "--context", "0")), host=_arg(argv, "--host", "127.0.0.1"),
            max_rss_mib=int(v) if (v := _arg(argv, "--max-rss-mib")) else None,
            min_free_disk_mb=int(v) if (v := _arg(argv, "--min-free-disk-mb")) else None,
            startup_timeout_s=float(_arg(argv, "--startup-timeout", "120")))
        spec.validate()
    except (SpecError, ValueError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    sup = Supervisor(spec, log_path=str(root / "server.log"))
    pidfile.write_text(str(os.getpid()))
    _write_status(status_file, state="starting", pid=os.getpid())

    def on_term(signum, frame):
        st = sup.stop()
        _write_status(status_file, state="stopped", reason="stopped on request", exit_code=st.exit_code)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, on_term)
    try:
        st = sup.start()
    except RuntimeFailed as exc:
        _write_status(status_file, state="failed", reason=str(exc))
        print(f"failed: {exc}", file=sys.stderr)
        return 1
    _write_status(status_file, state="running", pid=st.pid, server_pid=st.pid, rss_mib=st.rss_mib)
    final = sup.watch(interval_s=1.0)
    _write_status(status_file, state=final.state, reason=final.reason, exit_code=final.exit_code)
    return 0 if final.state == "stopped" else 1


def stop(argv: list[str]) -> int:
    root = Path(_arg(argv, "--root", "")).resolve()
    pidfile = root / "server.pid"
    if not pidfile.is_file():
        print("not running (no pidfile)")
        return 0
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, signal.SIGTERM)
    except (ValueError, ProcessLookupError, PermissionError):
        pass
    for _ in range(100):
        if not _pid_alive(int(pidfile.read_text().strip() or 0)) if pidfile.exists() else True:
            break
        time.sleep(0.1)
    pidfile.unlink(missing_ok=True)
    print("stopped")
    return 0


def status(argv: list[str]) -> int:
    root = Path(_arg(argv, "--root", "")).resolve()
    sf, pf = root / "runtime.status.json", root / "server.pid"
    if not sf.is_file():
        print(json.dumps({"state": "absent"}))
        return 0
    data = json.loads(sf.read_text())
    if data.get("state") in ("running", "starting") and pf.is_file() and not _pid_alive(int(pf.read_text().strip() or 0)):
        data = {"state": "crashed", "reason": "the supervisor process is gone but never reported a stop"}
    print(json.dumps(data))
    return 0
