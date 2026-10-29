"""Remote runners: SSH and docker exec. Template-only, config-checked before any process starts.

Both refuse (raise) before any network or process call when the target is not an entry in targets.yaml or
the transport is not one of that entry's allowed transports. SSH key paths come from an environment
variable named in the entry; the path is never logged, never put in a Fact, and never in a result.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
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


Exec = Callable[[list[str], bytes | None, float], tuple[int, bytes, bytes]]


def _real_exec(argv: list[str], stdin: bytes | None, timeout: float) -> tuple[int, bytes, bytes]:
    try:
        # A child must never read the human's terminal (approval answers are typed there): /dev/null unless data is passed.
        kw = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        done = subprocess.run(argv, capture_output=True, timeout=timeout, check=False, **kw)
        return done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired:
        return 124, b"", b"timed out"
    except OSError as exc:
        return 127, b"", f"cannot start {argv[0]}: {exc.strerror}".encode()


class RemoteRunner:
    def __init__(self, config: TargetConfig, *, exec_fn: Exec = _real_exec, host_key_policy: str = "yes",
                 known_hosts: str | None = None, connect_timeout: int = 5) -> None:
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
            *, stdin: bytes | None = None, timeout: float = 60.0) -> RemoteResult:
        entry = self.entry_for(target_id, transport)  # raises before anything else happens
        tpl = get_template(template) if isinstance(template, str) else template
        if stdin is not None and not tpl.sends_stdin:
            raise TemplateError(f"template {tpl.name!r} does not take input on stdin")
        if transport == Transport.ssh:
            argv, shown = self._ssh_argv(entry, tpl, values)
        elif transport == Transport.docker_exec:
            argv = ["docker", "exec", "-i", entry.docker_exec.container, *tpl.render(values)]
            shown = list(argv)
        else:
            raise TargetNotAllowed(f"no runner is built for transport {transport.value!r} in this phase")
        self.attempts.append((target_id, transport.value))
        code, out, err = self.exec_fn(argv, stdin, timeout)
        return RemoteResult(code, out.decode(errors="replace"), err.decode(errors="replace"), transport.value,
                            target_id, tpl.name, shown)

    def _ssh_argv(self, entry: TargetEntry, tpl: Template, values: dict[str, object] | None) -> tuple[list[str], list[str]]:
        key_path = os.environ.get(entry.ssh.key_env)
        if not key_path:
            raise MissingKey(f"environment variable {entry.ssh.key_env} is not set")
        remote = tpl.remote_command(values)
        opts = ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={self.connect_timeout}",
                "-o", f"StrictHostKeyChecking={self.host_key_policy}", "-o", "IdentitiesOnly=yes"]
        if self.known_hosts:
            opts += ["-o", f"UserKnownHostsFile={self.known_hosts}"]
        tail = ["-p", str(entry.ssh.port), f"{entry.ssh.user}@{entry.host}", remote]
        return ["ssh", "-i", key_path, *opts, *tail], ["ssh", "-i", "<key path from $" + entry.ssh.key_env + ">", *opts, *tail]
