"""Provisioning: transfer a pinned worker and a model file to ONE enrolled device and start a localhost-only server.

Every write goes through the approval gate (a fresh prompt per action) and, before it runs, an undo entry is
recorded. Only the pinned, checksummed release artifact is ever sent: never code a model wrote and never the
controller's own source. A checksum mismatch aborts before the first approval. Nothing here is reachable from a
worker: the worker build does not contain this module.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


from envrec.contracts import CreatedBy, Evidence, Fact, FactStatus, Origin, Target, TargetKind, Tier
from envrec.mission.models import EntryLimits, SshBlock, TargetConfig, TargetEntry, Transport
from envrec.policy.config_gate import decide_for_fleet
from envrec.remote.approval import ApprovalGate, PendingAction, UndoLog
from envrec.remote.preflight import Preflight, PreflightError, parse_df, preflight_facts, run_preflight
from envrec.remote.recipes import RECIPES
from envrec.remote.runner import RemoteRunner, TargetNotAllowed
from envrec.remote.sizing import ModelChoice, choose_model
from envrec.runtime.fetch import ChecksumMismatch
from envrec.runtime.manifest import Artifact
from envrec.remote.templates import INSTALL_ROOT, get_template
from envrec.store import Store

ROOT = f"{INSTALL_ROOT}/worker"
SERVER_PORT = 8089
SPECS = {s.name: s for s in RECIPES}


class PolicyDenied(PermissionError):
    pass


@dataclass
class Release:
    """The pinned worker archive. Runtimes and models are NOT here: they come from the approved manifest."""

    version: str
    filename: str
    sha256: str
    dir: Path

    @classmethod
    def load(cls, release_json: Path | str, artifacts_dir: Path | str) -> Release:
        d = json.loads(Path(release_json).read_text())
        return cls(d["version"], d["filename"], d["sha256"], Path(artifacts_dir))

    def verified_worker_path(self) -> Path:
        p = self.dir / self.filename
        if _file_sha(p) != self.sha256:
            raise ChecksumMismatch(f"{self.filename} does not match the pinned sha256 {self.sha256[:12]}...")
        return p


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class DeployPlan:
    """Exactly what will be put on one device. Built from verified, cached manifest artifacts; never from model text."""

    runtime: Artifact  # the pinned runtime archive
    runtime_path: Path  # in the controller's cache
    model: Artifact
    model_path: Path
    kind: str  # runtime project id from the manifest; the worker must have a verified flag set for it
    binary_name: str  # the server binary inside the archive, from the manifest
    threads: int = 2
    context: int = 2048
    max_rss_mib: int = 1024
    min_free_disk_mb: int = 500
    port: int = 8089


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
    plan: DeployPlan | None = None
    facts: dict = field(default_factory=dict)


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
        if Transport.local in e.allowed_transports:  # this computer: its own media session, or the door to a computer that is not enrolled yet
            kind = TargetKind.door if e.local.scope == "door" else TargetKind.media
        else:
            kind = TargetKind.remote  # a computer reached over a transport
        t = existing or Target(kind=kind, ring=1 if kind in (TargetKind.media, TargetKind.door) else 2, handle=e.id, authorized=True)
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

    def _write(self, tid: str, transport: Transport, recipe: str, template: str, values: dict, *, stdin: bytes | Path | None = None,
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
        return self.runner.run(tid, transport, template, values, stdin=stdin, timeout=900 if stdin is not None else 120)

    def _fail(self, res: DeployResult, recipe: str, why: str, *, undo: bool = True) -> DeployResult:
        res.steps.append(Step(recipe, "failed", why))
        res.failure, res.needs_undo = why, undo
        return res

    # ---- read-only ------------------------------------------------------------------------------------------

    def check(self, tid: str, transport: Transport) -> Preflight:
        pf = run_preflight(self.runner, tid, transport)
        preflight_facts(pf, self.targets[tid], self.store, transport)
        return pf

    def select_model(self, tid: str, pf: Preflight) -> ModelChoice:
        e = self.config.get(tid)
        return choose_model(pf.ram_effective_gib, pf.disk_free_gb, e.max_disk_gb, e.max_model_params_b)

    def health(self, tid: str, transport: Transport, port: int = SERVER_PORT, tries: int = 40) -> dict:
        for _ in range(tries):  # the server needs a moment to bind
            r = self.runner.run(tid, transport, "health", {"port": port})
            if r.ok:
                try:
                    return json.loads(r.stdout)
                except ValueError:
                    return {"status": "ok", "raw": r.stdout.strip()[:80]}
            time.sleep(0.5)
        return {"error": "no answer on 127.0.0.1 inside the device"}

    # ---- transfer one verified file ---------------------------------------------------------------------------

    def _send(self, tid, transport, recipe, dest: str, data: bytes | Path, sha: str, label: str) -> str | None:
        """Transfer one file and verify it on the device. Returns None when ok, else a reason."""
        size = len(data) if isinstance(data, bytes) else data.stat().st_size
        r = self._write(tid, transport, recipe, "receive_file", {"dest": dest}, stdin=data, checksum=sha,
                        artifact=f"{label}, {size / 1e6:.1f} MB")
        if r is None:
            return "denied"
        if not r.ok:
            return f"transfer failed: {r.stderr.strip()[:160]}"
        c = self.runner.run(tid, transport, "sha256_file", {"path": dest}, timeout=300)
        remote = c.stdout.split()[0] if c.ok and c.stdout.split() else ""
        return None if remote == sha else f"checksum mismatch on the device (got {remote[:12] or 'nothing'}, expected {sha[:12]})"

    def caps_for(self, pf: Preflight, entry: TargetEntry, context: int, port: int = SERVER_PORT) -> dict:
        """Resource caps from facts and limits: stricter of RAM and the target's own disk ceiling."""
        threads = max(1, min(4, pf.cpus))
        return {"threads": threads, "context": context, "port": port,
                "max_rss_mib": max(64, int(pf.ram_effective_gib * 1024 * 0.85)),
                "min_free_disk_mb": 300}

    # ---- deploy ---------------------------------------------------------------------------------------------------

    def deploy(self, tid: str, transport: Transport, plan: DeployPlan) -> DeployResult:
        res = DeployResult(tid, plan=plan)
        entry = self.config.get(tid)
        if entry is None:
            raise TargetNotAllowed(f"{tid!r} is not an entry in targets.yaml")
        worker = self.release.verified_worker_path()  # a mismatch on ANY local file aborts here, before any approval
        for art, path in ((plan.runtime, plan.runtime_path), (plan.model, plan.model_path)):
            if _file_sha(path) != art.sha256:
                raise ChecksumMismatch(f"cached {art.file} does not match the approved sha256")
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

        need_gb = (plan.runtime.bytes * 2 + plan.model.bytes) / 1e9 + 0.1
        budget = min(pf.disk_free_gb, entry.max_disk_gb)
        if need_gb > budget:
            return self._fail(res, "select_model", f"not enough disk: needs about {need_gb:.2f} GB, budget is {budget:.2f} GB", undo=False)
        res.model_name = plan.model.id
        res.steps.append(Step("select_model", "ok", f"{plan.model.id} with {plan.runtime.id}"))

        dirs = [ROOT, f"{ROOT}/models", f"{ROOT}/runtime"]
        for d in dirs:
            r = self._write(tid, transport, "install_runtime", "mkdir_install", {"dir": d}, undo=("remove_tree", {"dir": ROOT}))
            if r is None:
                res.steps.append(Step("install_runtime", "denied", f"mkdir {d}"))
                res.failure = "denied by the human"
                return res
            if not r.ok:
                return self._fail(res, "install_runtime", f"cannot create {d}: {r.stderr.strip()[:160]}")
        res.steps.append(Step("install_runtime", "ok", f"created {ROOT}"))

        worker_dest, archive_dest = f"{ROOT}/{self.release.filename}", f"{ROOT}/runtime/{plan.runtime.file}"
        model_dest = f"{ROOT}/models/{plan.model.file}"
        ty = worker_targets_yaml(entry)
        for dest, data, sha, label in [
            (worker_dest, worker, self.release.sha256, f"worker {self.release.version}"),
            (f"{ROOT}/targets.yaml", ty, hashlib.sha256(ty).hexdigest(), "worker targets.yaml (itself only)"),
            (archive_dest, plan.runtime_path, plan.runtime.sha256, plan.runtime.file),
        ]:
            why = self._send(tid, transport, "transfer_model", dest, data, sha, label)
            if why == "denied":
                res.steps.append(Step("transfer_model", "denied", label))
                res.failure, res.needs_undo = "denied by the human", True
                return res
            if why:
                return self._fail(res, "transfer_model", f"{label}: {why}")
            res.steps.append(Step("transfer_model", "ok", label))

        rt_dir = f"{ROOT}/runtime/{plan.kind}"
        for tpl, vals in (("mkdir_install", {"dir": rt_dir}), ("extract_tar", {"archive": archive_dest, "dest": rt_dir})):
            r = self._write(tid, transport, "install_runtime", tpl, vals)
            if r is None:
                res.steps.append(Step("install_runtime", "denied", tpl))
                res.failure, res.needs_undo = "denied by the human", True
                return res
            if not r.ok:
                return self._fail(res, "install_runtime", f"{tpl} failed: {r.stderr.strip()[:200]}")
        found = self.runner.run(tid, transport, "find_file", {"dir": rt_dir, "name": plan.binary_name})
        binary = found.stdout.split()[0] if found.ok and found.stdout.split() else ""
        if not binary:
            return self._fail(res, "install_runtime", f"{plan.binary_name} was not found in the unpacked runtime archive")
        res.steps.append(Step("install_runtime", "ok", f"runtime unpacked, server at {binary}"))

        why = self._send(tid, transport, "transfer_model", model_dest, plan.model_path, plan.model.sha256, plan.model.file)
        if why == "denied":
            res.steps.append(Step("transfer_model", "denied", plan.model.file))
            res.failure, res.needs_undo = "denied by the human", True
            return res
        if why:
            return self._fail(res, "transfer_model", f"{plan.model.file}: {why}")
        res.steps.append(Step("transfer_model", "ok", plan.model.file))

        t_start = time.monotonic()
        r = self._write(tid, transport, "start_server", "runtime_start", {
            "worker": worker_dest, "root": ROOT, "kind": plan.kind, "binary": binary, "model": model_dest, "port": plan.port,
            "threads": plan.threads, "context": plan.context, "max_rss_mib": plan.max_rss_mib,
            "min_free_disk_mb": plan.min_free_disk_mb}, undo=("runtime_stop", {"worker": worker_dest, "root": ROOT}))
        if r is None:
            res.steps.append(Step("start_server", "denied"))
            res.failure, res.needs_undo = "denied by the human", True
            return res
        if not r.ok:
            return self._fail(res, "start_server", r.stderr.strip()[:160])
        h = self.health(tid, transport, plan.port)
        res.facts = {"binary": binary, "model_path": model_dest, "worker": worker_dest, "health": h,
                     "startup_s": round(time.monotonic() - t_start, 2), "port": plan.port}
        if h.get("error") or str(h.get("status", "ok")) not in ("ok", "no slot available"):
            tail = self.runner.run(tid, transport, "tail_file", {"path": f"{ROOT}/server.log"})
            why = f"the server did not become healthy: {h}; log: {' | '.join(tail.stdout.strip().splitlines()[-2:])[:200]}"
            lib = re.search(r"error while loading shared libraries: (\S+?):", tail.stdout)
            if lib:  # the runtime needs a system library this device lacks; this harness installs no system packages
                why += (f"; the device is missing the system library {lib.group(1)}: install it yourself"
                        f"{' (package manager: ' + pf.pkg_manager + ')' if pf.pkg_manager != 'none' else ''}, then deploy again")
            return self._fail(res, "health_check", why)
        res.steps += [Step("start_server", "ok", f"127.0.0.1:{plan.port} on the device"), Step("health_check", "ok", "server answered /health")]
        res.ok, res.needs_undo = True, True
        # the deploy CLAIMS the server is running (a hypothesis made by the loop, from the health answer); a verifier then checks it. A verifier never creates the claim it verifies.
        self.store.write_fact(Fact(target_id=self.targets[tid].id, key="host.model_server", value="running", status=FactStatus.hypothesis, created_by=CreatedBy.loop,
                                   evidence=[Evidence(provider="deploy", probe="start_server+health", confidence=0.6, channel="ssh", limitations=["the device answered /health once; not yet verified"])]))
        return res

    def verify(self, tid: str, transport: Transport, res: DeployResult, expected, client=None):
        """Run the real-model verifier against a finished deploy. `client` defaults to a request made from inside the device
        (no tunnel needed); pass a TunnelClient to prove the same thing over the SSH tunnel."""
        from envrec.verify import ExecClient
        from envrec.verify.model_check import RemoteProbes, verify_model
        port = res.facts.get("port", SERVER_PORT)
        client = client or ExecClient(self.runner, tid, transport, port)
        probes = RemoteProbes(self.runner, tid, transport, port, res.facts["binary"])
        return verify_model(client, probes, self.store, self.targets[tid], expected, startup_s=res.facts.get("startup_s"))

    # ---- undo: ONE approved command -------------------------------------------------------------------------------

    def undo(self, tid: str, transport: Transport, port: int = SERVER_PORT) -> tuple[bool, str]:
        """Stop the server, delete everything installed (runtime and model files too), confirm the port is closed and
        the files are gone."""
        log = UndoLog(self.store, tid)
        if not log.pending():
            return True, "nothing to undo"
        worker = f"{ROOT}/{self.release.filename}" if self.release else f"{ROOT}/worker.pyz"
        have_worker = self.runner.run(tid, transport, "file_exists", {"path": worker}).stdout.strip() == "present"
        pidfile = f"{ROOT}/server.pid"
        stop_tpl, stop_vals = ("runtime_stop", {"worker": worker, "root": ROOT}) if have_worker else ("stop_server", {"pidfile": pidfile})
        cmds = [get_template(stop_tpl).remote_command(stop_vals), get_template("remove_tree").remote_command({"dir": ROOT}),
                get_template("file_exists").remote_command({"path": ROOT}), get_template("port_check").remote_command({"port": port})]
        pending = PendingAction("uninstall", " ; ".join(cmds), tid, Origin.builtin)
        if not self.gate.request(pending):
            return False, "undo denied by the human; the device is unchanged"
        before = self.runner.run(tid, transport, "df_free", {"path": "/opt"})
        self.runner.run(tid, transport, stop_tpl, stop_vals)
        r = self.runner.run(tid, transport, "remove_tree", {"dir": ROOT}, timeout=300)
        gone = self.runner.run(tid, transport, "file_exists", {"path": ROOT})
        closed = self.runner.run(tid, transport, "port_check", {"port": port})
        after = self.runner.run(tid, transport, "df_free", {"path": "/opt"})
        if not r.ok or gone.stdout.strip() != "absent":
            return False, f"undo ran but {ROOT} may remain: {r.stderr.strip()[:120] or gone.stdout.strip()}"
        if closed.stdout.strip() != "closed":
            return False, f"the files are gone but port {port} still answers on the device"
        log.mark_undone()
        try:
            freed = (parse_df(after.stdout) - parse_df(before.stdout)) * 1000
        except Exception:
            freed = 0.0
        return True, f"removed {ROOT}; confirmed absent; port {port} closed; free disk changed by {freed:+.0f} MB"

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
