"""Three ways to obtain a Capture for the same Cmd, so one parser serves them all.

LocalCollector runs the command on this machine, RemoteCollector through the existing template runner on an enrolled
target, ReplayCollector reads a recorded output. Every collector enforces the timeout, validates the argv first, and
returns a Capture with a typed error instead of raising.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Protocol

from envrec.contracts import Tier
from envrec.discovery.commands import CommandRefused, validate_argv
from envrec.discovery.types import MAX_CAPTURE_BYTES, Capture, Cmd, Reason, this_platform
from envrec.cmdtemplate import Template

_PERMISSION = re.compile(r"not authorized|operation not permitted|permission denied|not permitted|access denied|privacy", re.I)


def classify_failure(exit_code: int | None, stderr: str) -> Reason:
    if _PERMISSION.search(stderr or ""):
        return Reason.permission
    return Reason.unavailable


class Collector(Protocol):
    origin: str
    platform: str

    def run(self, cmd: Cmd) -> Capture: ...
    def read_file(self, name: str, path: str) -> Capture: ...
    def http_get(self, name: str, url: str, timeout_s: float = 3.0) -> Capture: ...
    def serial_exchange(self, name: str, port: str, plan: dict) -> Capture: ...


def _refused(cmd: Cmd, origin: str, platform: str, why: str) -> Capture:
    return Capture(cmd.name, " ".join(cmd.argv), b"", None, origin, platform, Reason.denied, why)


class LocalCollector:
    origin = "local"

    def __init__(self, platform: str | None = None) -> None:
        self.platform = platform or this_platform()

    def run(self, cmd: Cmd) -> Capture:
        src = " ".join(cmd.argv)
        try:
            argv = validate_argv(cmd.argv)
        except CommandRefused as exc:
            return _refused(cmd, self.origin, self.platform, str(exc))
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"), "LC_ALL": "C", "LANG": "C", "HOME": os.environ.get("HOME", "/")}
        err = subprocess.STDOUT if cmd.merge_stderr else subprocess.PIPE
        if cmd.window_s is not None:
            return self._windowed(cmd, argv, env, src)
        try:
            done = subprocess.run(list(argv), stdout=subprocess.PIPE, stderr=err, timeout=cmd.timeout_s, env=env, stdin=subprocess.DEVNULL, check=False)
        except subprocess.TimeoutExpired:
            return Capture(cmd.name, src, b"", None, self.origin, self.platform, Reason.timeout, f"no answer in {cmd.timeout_s:g} s")
        except FileNotFoundError:
            return Capture(cmd.name, src, b"", 127, self.origin, self.platform, Reason.not_applicable if cmd.optional else Reason.unavailable,
                           f"{argv[0]} is not installed")
        except OSError as exc:
            return Capture(cmd.name, src, b"", None, self.origin, self.platform, classify_failure(None, str(exc)), type(exc).__name__)
        if cmd.merge_stderr:
            done.stderr = b""
        data = done.stdout[:MAX_CAPTURE_BYTES]
        if done.returncode != 0:
            why = classify_failure(done.returncode, done.stderr.decode("utf-8", "replace"))
            return Capture(cmd.name, src, data, done.returncode, self.origin, self.platform, why, done.stderr.decode("utf-8", "replace")[:160])
        return Capture(cmd.name, src, data, 0, self.origin, self.platform)

    def http_get(self, name: str, url: str, timeout_s: float = 3.0) -> Capture:
        """One GET. A server on this computer (loopback) is read as before; an official documentation host from config/doc_sources.yaml is read over https with a size cap and a time cap.
        Anything else is refused. Redirects are followed only to hosts on the same list. The body is untrusted data: it is stored as a capture whose source is the URL."""
        import urllib.error
        import urllib.request
        from urllib.parse import urlparse
        from envrec.discovery.docsources import load_sources
        u = urlparse(url)
        host = u.hostname
        if host in ("127.0.0.1", "localhost", "::1"):
            limit, timeout, allowed = MAX_CAPTURE_BYTES, timeout_s, None
        else:
            src = load_sources()
            if u.scheme != "https" or host not in src.hosts:
                return Capture(name, url, b"", None, self.origin, self.platform, Reason.denied,
                               f"only loopback and the official documentation hosts in config/doc_sources.yaml may be read ({', '.join(sorted(src.hosts))}), over https")
            limit, timeout, allowed = src.max_bytes, src.timeout_s, src.hosts

        class Guard(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                h = urlparse(newurl).hostname
                if allowed is not None and (urlparse(newurl).scheme != "https" or h not in allowed):
                    raise urllib.error.URLError(f"redirect to {h} is not an official documentation host")
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        try:
            opener = urllib.request.build_opener(Guard)
            with opener.open(urllib.request.Request(url, headers={"User-Agent": "envrec-research", "Accept": "text/*, application/json"}, method="GET"), timeout=timeout) as r:  # noqa: S310
                body = r.read(limit + 1)
                if len(body) > limit:
                    return Capture(name, f"GET {url}", body[:limit], 0, self.origin, self.platform)  # the first part is kept; the rest is not read
                return Capture(name, f"GET {url}", body, 0, self.origin, self.platform)
        except TimeoutError:
            return Capture(name, f"GET {url}", b"", None, self.origin, self.platform, Reason.timeout, f"HANG: {host} did not answer in {timeout:g} s")
        except urllib.error.HTTPError as exc:
            return Capture(name, f"GET {url}", b"", None, self.origin, self.platform, Reason.unavailable, f"HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            reason = Reason.timeout if "timed out" in str(exc).lower() else Reason.unavailable
            return Capture(name, f"GET {url}", b"", None, self.origin, self.platform, reason, ("HANG: " if reason == Reason.timeout else "") + type(exc).__name__)

    def serial_exchange(self, name: str, port: str, plan: dict) -> Capture:
        """Open one serial port exclusively, listen first, then send ONLY the entries of `plan["probes"]`, stopping at the first reply."""
        import json
        import re as _re
        src = f"serial:{port}"
        if not _re.fullmatch(r"/dev/(cu\.[\w.\-]+|tty(USB|ACM|AMA|GS)\d+|rfcomm\d+|pts/\d+|ttys\d+)", port):
            return Capture(name, src, b"", None, self.origin, self.platform, Reason.denied, "that path is not a serial device path discovery may open")
        try:
            import fcntl
            import select
            import termios
        except ImportError:
            return Capture(name, src, b"", None, self.origin, self.platform, Reason.unavailable, "serial access needs a POSIX system (or pyserial)")
        speeds = {4800: termios.B4800, 9600: termios.B9600, 19200: termios.B19200, 38400: termios.B38400, 57600: termios.B57600, 115200: termios.B115200}
        try:
            fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except PermissionError:
            return Capture(name, src, b"", None, self.origin, self.platform, Reason.permission, "permission denied opening the port")
        except OSError as exc:
            return Capture(name, src, b"", None, self.origin, self.platform, Reason.unavailable, type(exc).__name__)
        log: dict = {"port": port, "passive": "", "sent": [], "writes": [], "reply": "", "baud": None}
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.ioctl(fd, termios.TIOCEXCL)
            except OSError:
                return Capture(name, src, b"", None, self.origin, self.platform, Reason.denied, "the port is in use by another program")

            def listen(seconds: float) -> bytes:
                import time
                end, got = time.monotonic() + seconds, b""
                while time.monotonic() < end:
                    r, _, _ = select.select([fd], [], [], 0.1)
                    if r:
                        try:
                            chunk = os.read(fd, 1024)
                        except OSError:
                            break
                        got += chunk
                return got

            for baud in plan.get("bauds", [9600]):
                if baud not in speeds:
                    continue
                attrs = termios.tcgetattr(fd)
                attrs[0] = attrs[1] = 0
                attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
                attrs[3] = 0
                attrs[4] = attrs[5] = speeds[baud]
                attrs[6][termios.VMIN], attrs[6][termios.VTIME] = 0, 0
                termios.tcsetattr(fd, termios.TCSANOW, attrs)
                termios.tcflush(fd, termios.TCIOFLUSH)
                log["baud"] = baud
                passive = listen(float(plan.get("listen_s", 1.5)))
                if passive:
                    log["passive"] = passive.decode("latin-1")
                    break
                answered = False
                for pr in plan.get("probes", []):
                    data = pr["send"].encode("latin-1")  # exactly the table's bytes, nothing else
                    os.write(fd, data)
                    log["sent"].append(pr["name"])
                    log["writes"].append(data.hex())
                    reply = listen(float(plan.get("reply_wait_s", 1.0)))
                    if reply:
                        log["reply"], answered = reply.decode("latin-1"), True
                        break
                if answered:
                    break
            return Capture(name, src, json.dumps(log).encode(), 0, self.origin, self.platform)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)

    def _windowed(self, cmd: Cmd, argv: tuple[str, ...], env: dict, src: str) -> Capture:
        import time
        try:
            proc = subprocess.Popen(list(argv), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            return Capture(cmd.name, src, b"", 127, self.origin, self.platform, Reason.not_applicable if cmd.optional else Reason.unavailable, f"{argv[0]} is not installed")
        except OSError as exc:
            return Capture(cmd.name, src, b"", None, self.origin, self.platform, classify_failure(None, str(exc)), type(exc).__name__)
        time.sleep(cmd.window_s)
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        return Capture(cmd.name, src, (out or b"")[:MAX_CAPTURE_BYTES], 0, self.origin, self.platform)

    def read_file(self, name: str, path: str) -> Capture:
        try:
            return Capture(name, f"file:{path}", Path(path).read_bytes()[:MAX_CAPTURE_BYTES], 0, self.origin, self.platform)
        except FileNotFoundError:
            return Capture(name, f"file:{path}", b"", None, self.origin, self.platform, Reason.not_applicable, "no such file")
        except PermissionError:
            return Capture(name, f"file:{path}", b"", None, self.origin, self.platform, Reason.permission, "permission denied")
        except OSError as exc:
            return Capture(name, f"file:{path}", b"", None, self.origin, self.platform, Reason.unavailable, type(exc).__name__)


class ReplayCollector:
    """Reads recorded outputs: `<dir>/<cmd.name>.out`. An optional `<name>.exit` holds a non-zero exit code."""

    origin = "replay"

    def __init__(self, directory: Path | str, platform: str) -> None:
        self.dir, self.platform = Path(directory), platform

    def run(self, cmd: Cmd) -> Capture:
        f = self.dir / f"{cmd.name}.out"
        src = " ".join(cmd.argv)
        if not f.is_file():
            return Capture(cmd.name, src, b"", None, self.origin, self.platform, Reason.unavailable, "no recording")
        code = int((self.dir / f"{cmd.name}.exit").read_text()) if (self.dir / f"{cmd.name}.exit").is_file() else 0
        if code != 0:
            return Capture(cmd.name, src, f.read_bytes(), code, self.origin, self.platform, classify_failure(code, f.read_text(errors="replace")), "recorded failure")
        return Capture(cmd.name, src, f.read_bytes(), 0, self.origin, self.platform)

    def http_get(self, name: str, url: str, timeout_s: float = 3.0) -> Capture:
        f = self.dir / f"{name}.out"
        if not f.is_file():
            return Capture(name, f"GET {url}", b"", None, self.origin, self.platform, Reason.unavailable, "no recording")
        return Capture(name, f"GET {url}", f.read_bytes(), 0, self.origin, self.platform)

    def serial_exchange(self, name: str, port: str, plan: dict) -> Capture:
        f = self.dir / f"{name}.out"
        if not f.is_file():
            return Capture(name, f"serial:{port}", b"", None, self.origin, self.platform, Reason.unavailable, "no recording")
        return Capture(name, f"serial:{port}", f.read_bytes(), 0, self.origin, self.platform)

    def read_file(self, name: str, path: str) -> Capture:
        f = self.dir / f"{name}.out"
        if not f.is_file():
            return Capture(name, f"file:{path}", b"", None, self.origin, self.platform, Reason.unavailable, "no recording")
        return Capture(name, f"file:{path}", f.read_bytes(), 0, self.origin, self.platform)


class RemoteCollector:
    """Runs a Cmd on an enrolled target through the existing template runner. Only argv lists that an adapter declared for the
    target's platform are accepted: the registry is built from the adapters' own command lists, never from free text."""

    origin = "remote"

    def __init__(self, runner, target_id: str, transport, platform: str, allowed: set[tuple[str, ...]], timeout_s: float = 30.0) -> None:
        self.runner, self.target_id, self.transport, self.platform = runner, target_id, transport, platform
        self.allowed, self.timeout_s = allowed, timeout_s

    def run(self, cmd: Cmd) -> Capture:
        src = " ".join(cmd.argv)
        try:
            argv = validate_argv(cmd.argv)
        except CommandRefused as exc:
            return _refused(cmd, self.origin, self.platform, str(exc))
        if argv not in self.allowed:
            return _refused(cmd, self.origin, self.platform, "not one of the fixed read-only templates for this platform")
        tpl = Template(f"disc_{cmd.name}", argv, Tier.observe, {}, "discovery read-only command")
        try:
            r = self.runner.run(self.target_id, self.transport, tpl, timeout=min(cmd.timeout_s, self.timeout_s))
        except Exception as exc:  # TargetNotAllowed, TemplateError, missing key ...
            return Capture(cmd.name, src, b"", None, self.origin, self.platform, Reason.denied, type(exc).__name__ + ": " + str(exc)[:120])
        data = r.stdout.encode("utf-8", "replace")[:MAX_CAPTURE_BYTES]
        if r.exit_code == 124:
            return Capture(cmd.name, src, data, 124, self.origin, self.platform, Reason.timeout, "timed out")
        if r.exit_code != 0:
            return Capture(cmd.name, src, data, r.exit_code, self.origin, self.platform, classify_failure(r.exit_code, r.stderr), r.stderr[:160])
        return Capture(cmd.name, src, data, 0, self.origin, self.platform)

    def read_file(self, name: str, path: str) -> Capture:
        return self.run(Cmd(name, ("cat", path)))

    def http_get(self, name: str, url: str, timeout_s: float = 3.0) -> Capture:
        return Capture(name, f"GET {url}", b"", None, self.origin, self.platform, Reason.not_applicable, "no local HTTP identify on remote targets")

    def serial_exchange(self, name: str, port: str, plan: dict) -> Capture:
        return Capture(name, f"serial:{port}", b"", None, self.origin, self.platform, Reason.not_applicable, "serial ports of enrolled targets use the serial runner")
