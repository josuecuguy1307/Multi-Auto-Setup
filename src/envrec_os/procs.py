"""Processes, the same way on every operating system (psutil has one answer for macOS, Linux and Windows): is this the process we started, stop it, which addresses is it listening on.

A process id alone is not an identity (ids are reused): a process is the one we started when its id AND its creation time match what was written down when it started."""

from __future__ import annotations

import time
from typing import Any

import psutil

LOOPBACK = ("127.0.0.1", "::1")


def created_at(pid: int) -> float | None:
    try:
        return psutil.Process(pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def is_ours(pid: int, created: float) -> bool:
    """The process with this id exists, is alive (not a zombie) and was created when we said."""
    try:
        p = psutil.Process(pid)
        return abs(p.create_time() - created) < 1.5 and p.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def resident_gib(pid: int) -> float | None:
    try:
        return round(psutil.Process(pid).memory_info().rss / 2 ** 30, 3)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def listening(pid: int) -> list[tuple[str, int]] | None:
    """The (address, port) pairs this process and its children listen on, or None when the system does not let us look (then nothing may be claimed)."""
    try:
        procs = [psutil.Process(pid), *psutil.Process(pid).children(recursive=True)]
        out = []
        for p in procs:
            conns = p.net_connections(kind="inet") if hasattr(p, "net_connections") else p.connections(kind="inet")
            out += [(c.laddr.ip, c.laddr.port) for c in conns if c.status == psutil.CONN_LISTEN]
        return out
    except psutil.NoSuchProcess:
        return []
    except (psutil.AccessDenied, OSError):
        return None


def stop(pid: int, created: float, grace_s: float = 8.0) -> dict[str, Any]:
    """Stop the process we started (and its children): ask, wait, then force. Does nothing to a process that is not ours. Returns {was_running, stopped}."""
    if not is_ours(pid, created):
        return {"was_running": False, "stopped": True}
    try:
        p = psutil.Process(pid)
        tree = [p, *p.children(recursive=True)]
        for q in tree:
            try:
                q.terminate()
            except psutil.NoSuchProcess:
                pass
        _gone, alive = psutil.wait_procs(tree, timeout=grace_s)
        for q in alive:
            try:
                q.kill()
            except psutil.NoSuchProcess:
                pass
        _gone, alive = psutil.wait_procs(alive, timeout=grace_s)
    except psutil.NoSuchProcess:
        return {"was_running": True, "stopped": True}
    t0 = time.monotonic()
    while is_ours(pid, created) and time.monotonic() - t0 < 2:
        time.sleep(0.05)
    return {"was_running": True, "stopped": not is_ours(pid, created)}


# ---- running a program for an environment run (docs/ROUTER_ENVIRONMENTS.md): no shell, bounded time and output, a minimal environment --------------------------------------------

def find_exe(name: str) -> str | None:
    """The full path of a program on this computer's PATH (the operating system's own lookup, including Windows' executable extensions), or None."""
    import shutil
    return shutil.which(name)


def run_capture(argv: list[str], cwd, env: dict[str, str], *, timeout_s: float, stdin_text: str | None = None, max_bytes: int = 2_000_000) -> dict[str, Any]:
    """Run `argv` (the whole command; no shell) in `cwd` with exactly `env` and wait. Returns {exit, stdout, stderr, timed_out, duration_s, truncated}. A program that outlives `timeout_s` is stopped
    with its children. The output is cut at `max_bytes` each; nothing here reads or logs a secret: the environment is whatever the caller built."""
    import subprocess
    t0 = time.monotonic()
    proc = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    timed_out = False
    try:
        out, err = proc.communicate(stdin_text.encode() if stdin_text is not None else None, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            parent = psutil.Process(proc.pid)
            for child in parent.children(recursive=True):
                child.kill()
        except psutil.Error:
            pass
        proc.kill()
        out, err = proc.communicate()
    return {"exit": proc.returncode, "stdout": out[:max_bytes].decode("utf-8", "replace"), "stderr": err[:max_bytes].decode("utf-8", "replace"), "timed_out": timed_out,
            "duration_s": round(time.monotonic() - t0, 2), "truncated": len(out) > max_bytes or len(err) > max_bytes}


def sandbox_backends() -> list[str]:
    """The sandbox backends ready on this computer, by the contract's list (docs/NETWORK_CONTRACT.md 8.1). Today only Docker exists as a backend: ready means its daemon answers."""
    import subprocess
    found = []
    if find_exe("docker"):
        try:
            if subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0:
                found.append("docker")
        except (OSError, subprocess.TimeoutExpired):
            pass
    return found
