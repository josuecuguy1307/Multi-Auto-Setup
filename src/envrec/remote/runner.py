"""Remote runners: SSH and docker exec. Template-only, config-checked before any process starts.

Both refuse (raise) before any network or process call when the target is not an entry in targets.yaml or
the transport is not one of that entry's allowed transports. SSH key paths come from an environment
variable named in the entry; the path is never logged, never put in a Fact, and never in a result.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from dataclasses import dataclass, field

from envrec.mission.models import TargetConfig, TargetEntry, Transport
from envrec.remote.templates import Template, TemplateError, get_template


class TargetNotAllowed(PermissionError):
    """The target (or transport) is not in targets.yaml. Raised before any process or network call."""


class MissingKey(RuntimeError):
    """The environment variable that should hold the key path is not set."""


@dataclass
class RemoteResult:
    exit_code: int
    stdout: str
    stderr: str
    transport: str
    target_id: str
    template: str
    argv_shown: list[str] = field(default_factory=list)  # with secrets redacted; safe to log

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


Exec = Callable[[list[str], "bytes | Path | None", float], tuple[int, bytes, bytes]]


def _real_exec(argv: list[str], stdin: bytes | None, timeout: float) -> tuple[int, bytes, bytes]:
    try:
        # A child must never read the human's terminal (approval answers are typed there): /dev/null unless data is passed.
        if isinstance(stdin, Path):  # stream a (possibly large) file instead of holding it in memory
            with stdin.open("rb") as fh:
                done = subprocess.run(argv, stdin=fh, capture_output=True, timeout=timeout, check=False)
        else:
            kw = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
            done = subprocess.run(argv, capture_output=True, timeout=timeout, check=False, **kw)
        return done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired:
        return 124, b"", b"timed out"
    except OSError as exc:
        return 127, b"", f"cannot start {argv[0]}: {exc.strerror}".encode()


SerialFn = Callable[[str, int, dict, float], tuple[int, bytes]]
TcpFn = Callable[[str, int, bytes | None, float], tuple[int, bytes]]


class RemoteRunner:
    def __init__(self, config: TargetConfig, *, exec_fn: Exec = _real_exec, host_key_policy: str = "yes",
                 known_hosts: str | None = None, connect_timeout: int = 5, serial_fn: SerialFn | None = None, tcp_fn: TcpFn | None = None) -> None:
        self.serial_fn, self.tcp_fn = serial_fn or _real_serial, tcp_fn or _real_tcp
        self.config, self.exec_fn = config, exec_fn
        self.host_key_policy, self.known_hosts, self.connect_timeout = host_key_policy, known_hosts, connect_timeout
        self.attempts: list[tuple[str, str]] = []  # (target id, transport) for every process actually started

    def entry_for(self, target_id: str, transport: Transport) -> TargetEntry:
        entry = self.config.get(target_id)
        if entry is None:
            raise TargetNotAllowed(f"{target_id!r} is not an entry in targets.yaml")
        if transport not in entry.allowed_transports:
            raise TargetNotAllowed(f"{transport.value!r} is not an allowed transport for {target_id!r}")
        return entry

    def run(self, target_id: str, transport: Transport, template: str | Template, values: dict[str, object] | None = None,
            *, stdin: bytes | Path | None = None, timeout: float = 60.0) -> RemoteResult:
        entry = self.entry_for(target_id, transport)  # raises before anything else happens
        tpl = get_template(template) if isinstance(template, str) else template
        if stdin is not None and not tpl.sends_stdin:
            raise TemplateError(f"template {tpl.name!r} does not take input on stdin")
        if transport == Transport.ssh:
            argv, shown = self._ssh_argv(entry, tpl, values)
        elif transport == Transport.docker_exec:
            argv = ["docker", "exec", "-i", entry.docker_exec.container, *tpl.render(values)]
            shown = list(argv)
        elif transport == Transport.adb_usb:
            # adb gives the device shell ONE string: the same quoted command a template renders for ssh. Only a fixed template can reach it.
            argv = ["adb", "-s", entry.adb_usb.serial, "shell", tpl.remote_command(values)]
            shown = list(argv)
        else:
            raise TargetNotAllowed(f"no runner is built for transport {transport.value!r} in this phase")
        self.attempts.append((target_id, transport.value))
        code, out, err = self.exec_fn(argv, stdin, timeout)
        return RemoteResult(code, out.decode(errors="replace"), err.decode(errors="replace"), transport.value,
                            target_id, tpl.name, shown)

    def run_argv(self, target_id: str, argv: list[str], shell: str, *, stdin: bytes | Path | None = None, timeout: float = 60.0) -> RemoteResult:
        """Run ONE argv that came from an owner-approved recipe (the executor is the only caller) on an enrolled computer over SSH, quoted for the shell the recipe names.
        Not reachable from the model: tools never pass an argv of their own here."""
        from envrec.recipes.shell import quote
        entry = self.entry_for(target_id, Transport.ssh)
        full, shown = self._ssh_argv_remote(entry, quote(argv, shell))
        self.attempts.append((target_id, "ssh"))
        code, out, err = self.exec_fn(full, stdin, timeout)
        return RemoteResult(code, out.decode(errors="replace"), err.decode(errors="replace"), "ssh", target_id, "recipe", shown)

    def _ssh_argv(self, entry: TargetEntry, tpl: Template, values: dict[str, object] | None) -> tuple[list[str], list[str]]:
        return self._ssh_argv_remote(entry, tpl.remote_command(values))

    def _ssh_argv_remote(self, entry: TargetEntry, remote: str) -> tuple[list[str], list[str]]:
        key_path = os.environ.get(entry.ssh.key_env)
        if not key_path:
            raise MissingKey(f"environment variable {entry.ssh.key_env} is not set")
        opts = ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={self.connect_timeout}",
                "-o", f"StrictHostKeyChecking={self.host_key_policy}", "-o", "IdentitiesOnly=yes"]
        if self.known_hosts:
            opts += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
        tail = ["-p", str(entry.ssh.port), f"{entry.ssh.user}@{entry.host}", remote]
        return ["ssh", "-i", key_path, *opts, *tail], ["ssh", "-i", "<key path from $" + entry.ssh.key_env + ">", *opts, *tail]


    # ---- routes with no shell: a serial port and a TCP endpoint ---------------------------------------------------------------------------------
    # They have no templates (there is no command line to put a value into). What they may do is fixed by the entry: listen, and, only when the entry says
    # `allow_send: true`, send the short fixed identification table. Both check targets.yaml before anything opens.

    def serial_listen(self, target_id: str, *, timeout: float = 10.0) -> RemoteResult:
        entry = self.entry_for(target_id, Transport.serial)
        from envrec.tools.builtin.hardware import serial_plan
        plan = dict(serial_plan())
        plan["bauds"] = [entry.serial.baud]
        if not entry.serial.allow_send:
            plan["probes"] = []  # listen-only: the entry did not allow anything to be sent
        self.attempts.append((target_id, "serial"))
        code, data = self.serial_fn(entry.serial.path, entry.serial.baud, plan, timeout)
        return RemoteResult(code, data.decode(errors="replace"), "", "serial", target_id, "serial_listen" if not entry.serial.allow_send else "serial_identify",
                            ["serial", entry.serial.path, str(entry.serial.baud), "send=" + ("fixed table" if entry.serial.allow_send else "nothing")])

    def ios_attached(self, target_id: str, *, timeout: float = 10.0) -> bool:
        """Is the phone this entry names on the cable? Lists devices only; it does not open a session, so no Trust prompt is raised."""
        from envrec.remote import ios
        entry = self.entry_for(target_id, Transport.ios_usb)
        self.attempts.append((target_id, "ios_usb"))
        code, out, _ = self.exec_fn(["idevice_id", "-l"], None, timeout)
        return code == 0 and any(ios.udid_hash(u) == entry.ios_usb.udid_sha256 for u in ios.parse_udids(out.decode(errors="replace")))

    def ios_read(self, target_id: str, aspect: str, *, timeout: float = 20.0) -> RemoteResult:
        """Read one fixed aspect (identity, battery, storage, name) of the phone the entry names, over the cable. Read-only; see envrec.remote.ios for the table and the rules."""
        from envrec.remote import ios
        entry = self.entry_for(target_id, Transport.ios_usb)
        if aspect not in ios.ASPECTS:
            raise TemplateError(f"no iOS aspect named {aspect!r}; the table has {sorted(ios.ASPECTS)}")
        self.attempts.append((target_id, "ios_usb"))
        shown = ["idevice_id", "-l"]
        code, out, err = self.exec_fn(shown, None, timeout)
        if code != 0:
            return RemoteResult(code, "", err.decode(errors="replace"), "ios_usb", target_id, "ios_list", shown)
        match = [u for u in ios.parse_udids(out.decode(errors="replace")) if ios.udid_hash(u) == entry.ios_usb.udid_sha256]
        if not match:
            return RemoteResult(1, "", "no device found: the phone this entry names is not attached", "ios_usb", target_id, "ios_list", shown)
        lines, shown_all = [], [shown]
        for _fact, domain, key in ios.ASPECTS[aspect]:
            argv = ["ideviceinfo", "-u", match[0], *(["-q", domain] if domain else []), "-k", key]
            show = [a if a != match[0] else "<udid>" for a in argv]
            shown_all.append(show)
            code, out, err = self.exec_fn(argv, None, timeout)
            if code != 0 or not out.strip():
                return RemoteResult(code or 1, "\n".join(lines), err.decode(errors="replace") or out.decode(errors="replace"), "ios_usb", target_id, f"ios_{aspect}", [" ".join(s) for s in shown_all])
            value = out.decode(errors="replace").strip().splitlines()[0]
            lines.append(f"{key}={value}")
        return RemoteResult(0, "\n".join(lines), "", "ios_usb", target_id, f"ios_{aspect}", [" ".join(s) for s in shown_all])

    def tcp_read(self, target_id: str, *, timeout: float = 5.0) -> RemoteResult:
        entry = self.entry_for(target_id, Transport.tcp)
        send = b"*IDN?\r\n" if entry.tcp.allow_send else None
        self.attempts.append((target_id, "tcp"))
        code, data = self.tcp_fn(entry.tcp.host, entry.tcp.port, send, timeout)
        return RemoteResult(code, data.decode(errors="replace"), "", "tcp", target_id, "tcp_identify" if send else "tcp_listen",
                            ["tcp", f"{entry.tcp.host}:{entry.tcp.port}", "send=" + ("*IDN?" if send else "nothing")])


def _real_serial(path: str, baud: int, plan: dict, timeout: float) -> tuple[int, bytes]:
    from envrec.discovery import LocalCollector
    c = LocalCollector().serial_exchange("probe", path, plan)
    return (0 if c.ok else 1), (c.data if c.ok else (c.detail or "").encode())


def _real_tcp(host: str, port: int, send: bytes | None, timeout: float) -> tuple[int, bytes]:
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            got = b""
            try:
                got = s.recv(512)
            except OSError:
                pass
            if not got and send:
                s.sendall(send)
                try:
                    got = s.recv(512)
                except OSError:
                    pass
            return 0, got[:512]
    except OSError as exc:
        return 1, type(exc).__name__.encode()
