"""Start, watch and stop the server child process.

The supervisor is the only code in the worker that starts a process, and it starts exactly one thing: the
argv built from a validated RuntimeSpec (never through a shell). It waits for the health endpoint, detects a
crash, and stops the server on its own if memory or free disk crosses the configured thresholds.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from envrec_worker.runtime.spec import RuntimeSpec, build_command


class RuntimeFailed(RuntimeError):
    """The server did not come up, or died. The message is the honest reason."""


@dataclass
class Status:
    state: str  # stopped | starting | running | crashed | stopped_by_watchdog
    pid: int | None = None
    exit_code: int | None = None
    reason: str = ""
    rss_mib: float | None = None
    free_disk_mb: float | None = None


def rss_mib(pid: int) -> float | None:
    """Resident memory of a process in MiB, from /proc (Linux) or ps (elsewhere). None if unreadable."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
        return round(int(out) / 1024, 1) if out else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


class Supervisor:
    def __init__(self, spec: RuntimeSpec, log_path: str | None = None) -> None:
        self.spec, self.log_path = spec, log_path
        self.proc: subprocess.Popen | None = None
        self._stopped_reason = ""

    # ---- start -----------------------------------------------------------------------------------------

    def start(self) -> Status:
        argv = build_command(self.spec)  # validates; raises SpecError for anything unsafe
        log = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
        try:
            self.proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                         start_new_session=True)
        except OSError as exc:
            raise RuntimeFailed(f"cannot start the server: {exc.strerror or type(exc).__name__}") from None
        finally:
            if self.log_path:
                log.close()
        deadline = time.monotonic() + self.spec.startup_timeout_s
        while time.monotonic() < deadline:
            code = self.proc.poll()
            if code is not None:
                raise RuntimeFailed(f"the server exited during startup with code {code}{self._log_tail()}")
            if self.healthy():
                return self.status()
            time.sleep(0.2)
        self.stop()
        raise RuntimeFailed(f"the server did not become healthy within {self.spec.startup_timeout_s:.0f}s{self._log_tail()}")

    def _log_tail(self) -> str:
        if not self.log_path:
            return ""
        try:
            tail = Path(self.log_path).read_text(errors="replace").strip().splitlines()[-3:]
            return ": " + " | ".join(tail) if tail else ""
        except OSError:
            return ""

    # ---- observe ----------------------------------------------------------------------------------------

    def healthy(self) -> bool:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.spec.port}/health", timeout=2) as r:
                return r.status == 200 and json.loads(r.read() or b"{}").get("status", "ok") in ("ok", "no slot available")
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def status(self) -> Status:
        if self.proc is None:
            return Status("stopped")
        code = self.proc.poll()
        if code is not None:
            if self._stopped_reason:
                return Status("stopped_by_watchdog", self.proc.pid, code, self._stopped_reason)
            if self._clean_stop:  # only a stop WE asked for counts as stopped; any other exit is a crash
                return Status("stopped", self.proc.pid, code, "stopped on request")
            return Status("crashed", self.proc.pid, code, f"exited with code {code}")
        free = shutil.disk_usage(Path(self.spec.model).parent).free / 1e6
        return Status("running" if self.healthy() else "starting", self.proc.pid, None, "", rss_mib(self.proc.pid), round(free, 1))

    _clean_stop = False

    # ---- watch ------------------------------------------------------------------------------------------

    def check_caps(self) -> str:
        """One watchdog pass. Returns the reason the server was stopped, or '' if all is within caps."""
        if self.proc is None or self.proc.poll() is not None:
            return ""
        rss = rss_mib(self.proc.pid)
        if self.spec.max_rss_mib is not None and rss is not None and rss > self.spec.max_rss_mib:
            return self._trip(f"memory {rss} MiB crossed the {self.spec.max_rss_mib} MiB cap")
        free = shutil.disk_usage(Path(self.spec.model).parent).free / 1e6
        if self.spec.min_free_disk_mb is not None and free < self.spec.min_free_disk_mb:
            return self._trip(f"free disk {free:.0f} MB fell below {self.spec.min_free_disk_mb} MB")
        return ""

    def _trip(self, reason: str) -> str:
        self._stopped_reason = reason
        self._terminate()
        return reason

    def watch(self, interval_s: float = 2.0) -> Status:
        """Block until the server stops for any reason; enforce the caps while it runs."""
        while self.proc is not None and self.proc.poll() is None:
            if self.check_caps():
                break
            time.sleep(interval_s)
        return self.status()

    # ---- stop -------------------------------------------------------------------------------------------

    def _terminate(self) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        try:
            os.killpg(self.proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                self.proc.kill()
            self.proc.wait(timeout=10)

    def stop(self) -> Status:
        self._clean_stop = True
        self._terminate()
        return self.status()
