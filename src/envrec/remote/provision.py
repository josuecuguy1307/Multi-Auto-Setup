"""Provisioning: transfer a pinned worker and a model file to ONE enrolled device and start a localhost-only server.

Every write goes through the approval gate (a fresh prompt per action) and, before it runs, an undo entry is
recorded. Only the pinned, checksummed release artifact is ever sent: never code a model wrote and never the
controller's own source. A checksum mismatch aborts before the first approval. Nothing here is reachable from a
worker: the worker build does not contain this module.
"""

from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


from envrec.contracts import Origin, Target, TargetKind, Tier
from envrec.mission.models import EntryLimits, SshBlock, TargetConfig, TargetEntry, Transport
from envrec.policy.config_gate import decide_for_fleet
from envrec.remote.approval import ApprovalGate, PendingAction, UndoLog
from envrec.remote.preflight import Preflight, PreflightError, preflight_facts, run_preflight
from envrec.remote.recipes import RECIPES
from envrec.remote.runner import RemoteRunner, TargetNotAllowed
from envrec.remote.sizing import ModelChoice, choose_model
from envrec.remote.templates import INSTALL_ROOT, get_template
from envrec.store import Store

ROOT = f"{INSTALL_ROOT}/worker"
SERVER_PORT = 8089
SPECS = {s.name: s for s in RECIPES}


class ChecksumMismatch(RuntimeError):
    """A local artifact does not match the pinned release.json. Raised before anything is sent."""


class PolicyDenied(PermissionError):
    pass


@dataclass
class Release:
    version: str
    filename: str
    sha256: str
    models: dict[str, dict]
    dir: Path

    @classmethod
    def load(cls, release_json: Path | str, artifacts_dir: Path | str) -> Release:
        d = json.loads(Path(release_json).read_text())
        return cls(d["version"], d["filename"], d["sha256"], d["models"], Path(artifacts_dir))

    def verified_worker_bytes(self) -> bytes:
        data = (self.dir / self.filename).read_bytes()
        if hashlib.sha256(data).hexdigest() != self.sha256:
            raise ChecksumMismatch(f"{self.filename} does not match the pinned sha256 {self.sha256[:12]}...")
        return data

    def verified_model_bytes(self, name: str) -> bytes:
        data = (self.dir / "models" / name).read_bytes()
        if hashlib.sha256(data).hexdigest() != self.models[name]["sha256"]:
            raise ChecksumMismatch(f"model {name} does not match the pinned sha256")
        return data


@dataclass
class Step:
    recipe: str
    status: str  # ok | denied | failed
    detail: str = ""


@dataclass
class DeployResult:
    target_id: str
    ok: bool = False
    steps: list[Step] = field(default_factory=list)
    model: ModelChoice | None = None
    model_name: str | None = None
    failure: str = ""
    needs_undo: bool = False
    preflight: Preflight | None = None


def worker_targets_yaml(entry: TargetEntry) -> bytes:
    """The worker's own targets.yaml: exactly one entry, itself. Generated here from the enrolled entry."""
    me = TargetEntry(id=entry.id, name=entry.name, host="127.0.0.1", os=entry.os, allowed_transports=[Transport.ssh],
                     ssh=SshBlock(user=entry.ssh.user if entry.ssh else "worker", key_env="ENVREC_SELF_KEY"),
                     limits=entry.limits, owner_confirmed=True)
    # JSON is valid YAML; emitting it this way keeps every YAML writer out of this module (only enroll writes YAML)
    return (json.dumps(TargetConfig(targets=[me]).model_dump(mode="json"), indent=2) + "\n").encode()


def sync_targets(store: Store, config: TargetConfig) -> dict[str, Target]:
    """Register every targets.yaml entry as an authorized Target. This is the only way a remote Target gets
    authorized=True, and the flag is accepted by the store only when it comes from config."""
    out = {}
    for e in config.targets:
        existing = next((t for t in store.list_targets() if t.handle == e.id), None)
        t = existing or Target(kind=TargetKind.net_host, ring=2, handle=e.id, authorized=True)
        out[e.id] = store.put_target(t.model_copy(update={"authorized": True}), from_config=True)
    return out


class Tunnel:
    def __init__(self, proc: subprocess.Popen, local_port: int, argv_shown: list[str]) -> None:
        self.proc, self.local_port, self.argv_shown = proc, local_port, argv_shown

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class Provisioner:
    def __init__(self, runner: RemoteRunner, gate: ApprovalGate, store: Store, config: TargetConfig, release: Release | None) -> None:
        self.runner, self.gate, self.store, self.config, self.release = runner, gate, store, config, release
        self.targets = sync_targets(store, config)

    # ---- the one write path: policy, then fresh approval, then undo entry, then run ----------------------

    def _write(self, tid: str, transport: Transport, recipe: str, template: str, values: dict, *, stdin: bytes | None = None,
               checksum: str | None = None, artifact: str | None = None, undo: tuple[str, dict] | None = None):
        decision = decide_for_fleet(SPECS[recipe], self.targets[tid], self.config)
        if decision.decision.value == "deny":
            raise PolicyDenied(decision.reason)
        tpl = get_template(template)
        pending = PendingAction(recipe, tpl.remote_command(values), tid, Origin.builtin, checksum, artifact)
        if not self.gate.request(pending):
            return None  # denied by the human: nothing recorded, nothing run
        if undo:
            UndoLog(self.store, tid).record(recipe, undo[0], undo[1])
        return self.runner.run(tid, transport, template, values, stdin=stdin)

    def _fail(self, res: DeployResult, recipe: str, why: str, *, undo: bool = True) -> DeployResult:
        res.steps.append(Step(recipe, "failed", why))
        res.failure, res.needs_undo = why, undo
        return res

    # ---- the recipes -------------------------------------------------------------------------------------------

    def check(self, tid: str, transport: Transport) -> Preflight:
        pf = run_preflight(self.runner, tid, transport)
        preflight_facts(pf, self.targets[tid], self.store, transport)
        return pf

    def select_model(self, tid: str, pf: Preflight) -> ModelChoice:
        e = self.config.get(tid)
        return choose_model(pf.ram_effective_gib, pf.disk_free_gb, e.max_disk_gb, e.max_model_params_b)

    def _send(self, tid, transport, recipe, dest: str, data: bytes, sha: str, label: str) -> str | None:
        """Transfer one file and verify it on the device. Returns None when ok, else a reason."""
        r = self._write(tid, transport, recipe, "receive_file", {"dest": dest}, stdin=data, checksum=sha, artifact=label)
        if r is None:
            return "denied"
        if not r.ok:
            return f"transfer failed: {r.stderr.strip()[:160]}"
        c = self.runner.run(tid, transport, "sha256_file", {"path": dest})
        remote = c.stdout.split()[0] if c.ok and c.stdout.split() else ""
        return None if remote == sha else f"checksum mismatch on the device (got {remote[:12] or 'nothing'}, expected {sha[:12]})"

    def deploy(self, tid: str, transport: Transport) -> DeployResult:
        res = DeployResult(tid)
        entry = self.config.get(tid)
        if entry is None:
            raise TargetNotAllowed(f"{tid!r} is not an entry in targets.yaml")
        worker = self.release.verified_worker_bytes()  # a mismatch aborts here, before any approval or install
        try:
            pf = self.check(tid, transport)
        except PreflightError as exc:
            return self._fail(res, "check", str(exc), undo=False)
        res.preflight = pf
        res.steps.append(Step("check", "ok", f"{pf.arch} {pf.os_name}, RAM {pf.ram_effective_gib} GiB, disk {pf.disk_free_gb} GB"))

        py = self.runner.run(tid, transport, "python_version")
        if not py.ok:
            hint = ("python3 is missing and this harness installs no system packages; "
                    f"install it yourself ({'package manager: ' + pf.pkg_manager if pf.pkg_manager != 'none' else 'no package manager found'})")
            return self._fail(res, "install_runtime", hint, undo=False)
        res.steps.append(Step("install_runtime", "ok", py.stdout.strip() or py.stderr.strip()))

        choice = self.select_model(tid, pf)
        res.model = choice
        if choice.params_b == 0.0:
            return self._fail(res, "select_model", f"no model fits this device (limited by {choice.limited_by})", undo=False)
        res.model_name = f"stub-{choice.params_b:g}b"
        model = self.release.verified_model_bytes(res.model_name)
        res.steps.append(Step("select_model", "ok", f"{res.model_name} ({choice.params_b:g}B, limited by {choice.limited_by})"))

        models_dir = f"{ROOT}/models"
        for d in (ROOT, models_dir):
            r = self._write(tid, transport, "install_runtime", "mkdir_install", {"dir": d}, undo=("remove_tree", {"dir": ROOT}))
            if r is None:
                res.steps.append(Step("install_runtime", "denied", f"mkdir {d}"))
                res.failure = "denied by the human"
                return res
            if not r.ok:
                return self._fail(res, "install_runtime", f"cannot create {d}: {r.stderr.strip()[:160]}")
        res.steps.append(Step("install_runtime", "ok", f"created {ROOT}"))

        runtime = f"{ROOT}/{self.release.filename}"
        steps = [(runtime, worker, self.release.sha256, f"worker {self.release.version}"),
                 (f"{ROOT}/targets.yaml", (ty := worker_targets_yaml(entry)), hashlib.sha256(ty).hexdigest(), "worker targets.yaml (itself only)"),
                 (f"{models_dir}/{res.model_name}", model, self.release.models[res.model_name]["sha256"], res.model_name)]
        for dest, data, sha, label in steps:
            why = self._send(tid, transport, "transfer_model", dest, data, sha, label)
            if why == "denied":
                res.steps.append(Step("transfer_model", "denied", label))
                res.failure, res.needs_undo = "denied by the human", True
                return res
            if why:
                return self._fail(res, "transfer_model", f"{label}: {why}")
            res.steps.append(Step("transfer_model", "ok", label))

        pidfile = f"{ROOT}/server.pid"
        r = self._write(tid, transport, "start_server", "start_server",
                        {"runtime": runtime, "port": SERVER_PORT, "model": res.model_name, "log": f"{ROOT}/server.log", "pidfile": pidfile},
                        undo=("stop_server", {"pidfile": pidfile}))
        if r is None:
            res.steps.append(Step("start_server", "denied"))
            res.failure, res.needs_undo = "denied by the human", True
            return res
        if not r.ok:
            return self._fail(res, "start_server", r.stderr.strip()[:160])
        time.sleep(0.5)
        h = self.health(tid, transport)
        if not h.get("status") == "ok" or h.get("model_sha256") != self.release.models[res.model_name]["sha256"]:
            return self._fail(res, "health_check", f"server did not report healthy with the transferred model: {h}")
        res.steps += [Step("start_server", "ok", f"127.0.0.1:{SERVER_PORT} on the device"), Step("health_check", "ok", "model checksum matches")]
        res.ok, res.needs_undo = True, True
        return res

    def health(self, tid: str, transport: Transport) -> dict:
        for _ in range(10):  # the server needs a moment to bind
            r = self.runner.run(tid, transport, "health", {"port": SERVER_PORT})
            if r.ok:
                try:
                    return json.loads(r.stdout)
                except ValueError:
                    return {"error": "health reply was not JSON"}
            time.sleep(0.3)
        return {"error": "no answer on 127.0.0.1 inside the device"}

    # ---- undo: ONE approved command -------------------------------------------------------------------------------

    def undo(self, tid: str, transport: Transport) -> tuple[bool, str]:
        """Stop the server, remove everything installed, then confirm nothing remains."""
        log = UndoLog(self.store, tid)
        if not log.pending():
            return True, "nothing to undo"
        pidfile = f"{ROOT}/server.pid"
        cmds = [get_template("stop_server").remote_command({"pidfile": pidfile}),
                get_template("remove_tree").remote_command({"dir": ROOT}),
                get_template("file_exists").remote_command({"path": ROOT})]
        pending = PendingAction("uninstall", " ; ".join(cmds), tid, Origin.builtin)
        if not self.gate.request(pending):
            return False, "undo denied by the human; the device is unchanged"
        self.runner.run(tid, transport, "stop_server", {"pidfile": pidfile})
        r = self.runner.run(tid, transport, "remove_tree", {"dir": ROOT})
        gone = self.runner.run(tid, transport, "file_exists", {"path": ROOT})
        if not r.ok or gone.stdout.strip() != "absent":
            return False, f"undo ran but {ROOT} may remain: {r.stderr.strip()[:120] or gone.stdout.strip()}"
        log.mark_undone()
        return True, f"removed {ROOT}; confirmed absent"

    # ---- the approved tunnel to the device's localhost -----------------------------------------------------------------

    def open_tunnel(self, tid: str) -> Tunnel:
        entry = self.runner.entry_for(tid, Transport.ssh)
        import os
        key = os.environ.get(entry.ssh.key_env)
        if not key:
            raise PermissionError(f"environment variable {entry.ssh.key_env} is not set")
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        local = s.getsockname()[1]
        s.close()
        opts = ["-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", f"StrictHostKeyChecking={self.runner.host_key_policy}",
                "-o", "IdentitiesOnly=yes"]
        if self.runner.known_hosts:
            opts += ["-o", f"UserKnownHostsFile={self.runner.known_hosts}"]
        tail = ["-L", f"127.0.0.1:{local}:127.0.0.1:{SERVER_PORT}", "-p", str(entry.ssh.port), f"{entry.ssh.user}@{entry.host}"]
        shown = ["ssh", "-i", f"<key from ${entry.ssh.key_env}>", *opts, *tail]
        pending = PendingAction("open_tunnel", " ".join(shown), tid, Origin.builtin)
        if not self.gate.request(pending):
            raise PermissionError("tunnel denied by the human")
        proc = subprocess.Popen(["ssh", "-i", key, *opts, *tail], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                socket.create_connection(("127.0.0.1", local), timeout=0.2).close()
                return Tunnel(proc, local, shown)
            except OSError:
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
        proc.kill()
        raise ConnectionError("the tunnel did not come up")
