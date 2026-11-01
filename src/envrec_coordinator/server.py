"""The coordinator's server. The ONE LAN-facing listening socket of the whole system is created here, by `asyncio.start_server` below, and nowhere else (conformance N2). With `web=True` the
coordinator also serves the owner's screen on ONE more socket, bound to loopback only (`web.py`); it is off by default.

A client without a certificate may only try to pair. A client with a certificate must carry one pinned at pairing (the TLS handshake fails otherwise); its messages are signed
envelopes, and anything unsigned, badly signed, replayed, stale, from an unknown sender or of an unknown type is logged and ends the connection."""

from __future__ import annotations

import asyncio
import ipaddress
import time
import uuid
from pathlib import Path

import envrec_os
from envrec_coordinator.control import ControlWatcher, write_json
from envrec_coordinator.discovery import DiscoveryResponder, open_responder
from envrec_coordinator.harness import HarnessCore
from envrec_coordinator.owner import OwnerDesk
from envrec_coordinator.pairing import HELLO_TIMEOUT_S, Decision, PairingBusy, PairingDesk
from envrec_coordinator.registry import Registry
from envrec_coordinator.services import Services
from envrec_coordinator.runs import OWNER_GESTURES, AgentRuns
from envrec_coordinator.tasks import ALERT_SKEW_S, TaskDesk, evaluate_report
from envrec_coordinator.waiting import WaitingDesk
from envrec_coordinator.web import WEB_HOST, WebScreen
from envrec_net import envrun as ER
from envrec_net import identity as ident
from envrec_net import task as T
from envrec_net import pake, tls
from envrec_net.envelope import EnvelopeRejected
from envrec_net.session import PLAIN, PROTOCOL, Sealer, SessionVerifier, new_challenge
from envrec_net.log import EventLog
from envrec_net.wire import FrameError, read_frame, write_frame

HEARTBEAT_INTERVAL_S = 10.0
IDLE_TIMEOUT_S = 3 * HEARTBEAT_INTERVAL_S
SUMMARY_EVERY_S = 300.0  # a heartbeat is not an event: the log gets connect, disconnect, a changed card and one summary this often
CAPS = {"hostname": 80, "os": 20, "arch": 20, "os_release": 40, "agent": 40}


class BindRefused(ValueError):
    pass


class WaitingGone(Exception):
    pass


def check_bind(host: str) -> str:
    """The coordinator listens on one address the owner chose: loopback, a private LAN address or a link-local one. Never every interface, never a public address."""
    try:
        a = ipaddress.ip_address(host)
    except ValueError:
        raise BindRefused(f"{host!r} is not an IP address: name the interface address to listen on") from None
    if a.is_unspecified or a.is_multicast or a.is_global:
        raise BindRefused(f"{host} is not allowed: choose a loopback, private or link-local address of this computer")
    return str(a)


def _clip(v, cap: int) -> str:
    return "".join(c for c in str(v) if c.isprintable())[:cap]


class LocalStep:
    """The record of a step run on this computer, with what the setup adapter reads from a node task's record."""

    def __init__(self) -> None:
        self.status, self.reason, self.result, self.duration_s = "sent", "", {}, 0.0
        self.done = asyncio.Event()


class Coordinator:
    def __init__(self, state_dir: Path, host: str, port: int, *, clock=time.time, idle_timeout_s: float = IDLE_TIMEOUT_S, owner_ttl_s: float = 120.0, summary_every_s: float = SUMMARY_EVERY_S,
                 web: bool = False, web_port: int = 0, discovery_port: int | None = None, name: str | None = None, include_virtual: bool = False,
                 skew_alert_s: float = ALERT_SKEW_S, report_every_s: float = 300.0,
                 model_profile: str | None = None, models_file=None, board_escalation_limit: int = 2) -> None:
        self.board_escalation_limit = board_escalation_limit  # settings key board_escalation_limit; Services clamps it to 0..the router's maximum
        self.state, self.host, self.port, self.clock, self.idle_timeout_s = Path(state_dir) / "coordinator", check_bind(host), port, clock, idle_timeout_s
        self.layer = envrec_os.current()
        self.owner_ttl_s = owner_ttl_s
        self._server: asyncio.AbstractServer | None = None
        self._tasks: list[asyncio.Task] = []
        self._conns: set[asyncio.StreamWriter] = set()
        self.summary_every_s, self.skew_alert_s, self.report_every_s = summary_every_s, skew_alert_s, report_every_s
        self._sessions: dict[str, NodeSession] = {}
        self._model_profile, self._models_file = model_profile, models_file
        self._discovery_port, self.include_virtual = discovery_port, include_virtual
        self.display_name = (name or envrec_os.card()['hostname'])[:60]
        self.responder: DiscoveryResponder | None = None
        self._udp = None
        self._last_disc_summary = 0.0
        self._online: dict[str, int] = {}  # node id -> open authenticated sessions
        self._web_wanted, self._web_port = web, web_port
        self.web: WebScreen | None = None
        self._native_confirm = None  # tests put a fake dialog here; None means the operating system's own

    async def start(self) -> None:
        self.state.mkdir(parents=True, exist_ok=True)
        self.layer.protect_private(self.state)
        self.identity = ident.load_or_create(self.state / "identity", self.layer)
        self.registry = Registry(self.state, self.layer)
        self.log = EventLog(self.state / "log")  # opened on this thread: the store belongs to it
        self.ctx = tls.server_context(self.state / "identity", self.registry.trusted_certs())
        self.watcher = ControlWatcher(self.state, self.layer, self._on_cli_offer, self._on_cli_task, self._on_cli_unpair)
        self.tasks = TaskDesk(clock=self.clock, log=lambda t, p, owner=False: self.log.add(t, p, owner=owner), sessions=lambda nid: self._sessions.get(nid), approvals=self._owner_approval, origins=self._owner_origin, credential_approvals=self._owner_credential_approval)
        self.tasks.on_result = self._on_task_result
        self.tasks.on_change = self._publish_tasks
        self.runs = AgentRuns(self, model_profile=self._model_profile, models_file=self._models_file)
        self.harness = HarnessCore(self, native_confirm=self._native_confirm, clock=self.clock)
        self.services = Services(self)  # the router, the admission desk and the access flows, wired to this coordinator; they decide nothing the owner or the written rule decides
        self._connect_env = None  # what the connection of a chat needs to know about this computer; a test gives it a temporary home and fake commands
        self.waiting = WaitingDesk(clock=self.clock, log=lambda t, p: self.log.add(t, p), on_change=self._publish_waiting)
        self.owner = OwnerDesk(self.clock)
        self.desk = PairingDesk(identity=self.identity, registry=self.registry, log=self.log, ask_owner=self._ask_owner, on_paired=lambda der: tls.add_trusted(self.ctx, der),
                                clock=self.clock, owner_ttl_s=self.owner_ttl_s, on_outcome=self._on_outcome, on_stage=lambda i, stage: self.owner.exchanging(i))
        self._server = await asyncio.start_server(self._on_connect, self.host, self.port, ssl=self.ctx)  # THE listening socket
        self.port = self._server.sockets[0].getsockname()[1]
        self._tasks.append(asyncio.create_task(self.watcher.run()))
        if self._discovery_port is not None:
            self.responder = DiscoveryResponder(self.identity, name=self.display_name, tcp_port=self.port, clock=self.clock, include_virtual=self.include_virtual,
                                                log=lambda t, p: self.log.add(t, p))
            self._udp = await open_responder(self.responder, self._discovery_port)  # the ONE discovery socket (optional)
            self._discovery_port = self._udp.get_extra_info("sockname")[1]
            self.log.add("discovery_started", {"udp_port": self._discovery_port})
        self._publish_waiting()
        self._tasks.append(asyncio.create_task(self._tick()))
        self.log.add("coordinator_started", {"host": self.host, "port": self.port, "fingerprint": self.identity.fingerprint, "paired_nodes": len(self.registry.all())})
        if self._web_wanted:
            try:
                self.web = WebScreen(self, port=self._web_port, clock=self.clock)
                self.web.start(asyncio.get_running_loop())
            except OSError:
                self.web = None
                await self.stop()
                raise
            self.harness.write_endpoint(self.web.port)
            self.log.add("web_started", {"host": WEB_HOST, "port": self.web.port})  # the link carries the one-time secret: it is never logged

    # ---- the owner's front ends: the terminal (control files) and the web screen. Both call these; neither holds protocol logic -------------------

    def _on_cli_offer(self, offer_id: str, code: str, node: str | None = None) -> None:
        try:
            self._on_offer(offer_id, code, "cli", bound_to=node)
        except (WaitingGone, PairingBusy) as exc:
            self.watcher.report(offer_id, "rejected", "that machine is not waiting any more" if isinstance(exc, WaitingGone) else "another pairing is in progress")

    def _on_offer(self, offer_id: str, code: str, via: str = "cli", bound_to: str | None = None) -> None:
        target = None
        if bound_to is not None:
            w = self.waiting.get(bound_to)
            if w is None:
                raise WaitingGone(bound_to)
            if self.desk.busy:
                raise PairingBusy()
            target = w.name
        if via == "agent":  # the OWNER's pick came first (an OWNER event in the run); making the code is code's act, and is logged as code's
            self.log.add("pairing_offer_for_picked", {"offer": offer_id, "via": via, "waiting": bound_to, "machine": target})
        elif via.startswith("chat:"):  # a chat's tool call is not the owner: the code is only SHOWN, and nothing is approved until the owner confirms with a gesture
            self.log.add("pairing_offer_requested_by_chat", {"offer": offer_id, "via": via, **({"waiting": bound_to, "machine": target} if bound_to else {})})
        else:
            self.log.add("owner_requested_pairing", {"offer": offer_id, "via": via, **({"waiting": bound_to, "machine": target} if bound_to else {})}, owner=True)
        offer = self.desk.open_offer(offer_id, code, bound_to)
        self.owner.opened(offer_id, code, offer.expires, via, target=target)
        if bound_to is not None:
            self.waiting.pick(bound_to)  # only now may that machine's own connection continue to the key exchange

    def offer_for_waiting(self, wait_id: str, via: str = "web") -> dict:
        """The owner picked a waiting machine: a code is made for that machine only."""
        self.desk.expire_due()
        if self.desk.busy:
            raise PairingBusy()
        self._on_offer(uuid.uuid4().hex[:12], pake.new_code(), via, bound_to=wait_id)
        return self.snapshot()

    def _publish_waiting(self) -> None:
        """The terminal's `mas waiting` reads this file (the coordinator opens no socket for it)."""
        try:
            write_json(self.layer, self.watcher.dir / "waiting.json", {"waiting": [w.view() for w in self.waiting.list()], "at": self.clock()})
        except OSError:
            pass

    def begin_pairing(self, via: str = "web") -> dict:
        """The owner asks for a new code (same effect as `mas pair` on this computer)."""
        self.desk.expire_due()
        if self.desk.busy:
            raise PairingBusy()
        self._on_offer(uuid.uuid4().hex[:12], pake.new_code(), via)
        return self.snapshot()

    def decide_pairing(self, offer_id: str, accept: bool, via: str) -> bool:
        return self.owner.decide(offer_id, accept, via)

    async def _ask_owner(self, summary: dict) -> Decision:
        """Ask the owner in every front end at once; the first answer wins and the other question is withdrawn."""
        cli = asyncio.ensure_future(self.watcher.ask_owner(summary))
        web = asyncio.ensure_future(self.owner.ask(summary))
        try:
            done, _ = await asyncio.wait({cli, web}, return_when=asyncio.FIRST_COMPLETED)
            return web.result() if web in done else Decision(cli.result(), "cli")
        finally:
            for t in (cli, web):
                t.cancel()
            await asyncio.gather(cli, web, return_exceptions=True)

    def _on_outcome(self, offer_id: str, out) -> None:
        self.watcher.report(offer_id, out.status, out.reason)
        self.owner.outcome(offer_id, out.status, out.reason)

    async def _tick(self) -> None:
        while True:
            self.desk.expire_due()  # a code nobody used ends as expired for every front end, visitor or not
            if self.responder is not None and self.clock() - self._last_disc_summary >= self.summary_every_s:
                self._last_disc_summary = self.clock()
                summary = self.responder.summary()
                if any(summary.values()):
                    self.log.add("discovery_summary", summary)
            await asyncio.sleep(1.0)

    def snapshot(self) -> dict:
        """What the owner's screen shows: the paired machines and the pairing in progress."""
        self.desk.expire_due()
        machines = [{"id": n.node_id, "name": n.name, "os": n.os, "arch": n.arch, "status": "online" if self._online.get(n.node_id) else "offline",
                     "last_heartbeat": n.last_heartbeat, "fingerprint": n.fingerprint, "paired_at": n.paired_at, "skew_s": n.skew_s, "report": n.report, "report_at": n.report_at,
                     "alerts": self._alerts_of(n), "analysis_at": n.analysis_at, "agent_analysis_at": (self.runs.stored_analysis(n.node_id) or {}).get("stored_at")} for n in self.registry.all()]
        return {"now": self.clock(), "machines": machines, "pairing": self.owner.view(), "waiting": [w.view() for w in self.waiting.list()], "tasks": [t.view() for t in self.tasks.list()[-30:]], "runs": self.runs.listing(), "model": self.runs.model_status(), "confirmations": self.harness.pending_confirmations(), "activity": self.harness.activity_tail(), "openwebui_admin": (self.connect_env.home / "mas-openwebui" / "admin.secret").exists()}

    def _alerts_of(self, n) -> list:
        """The stored alerts of the last report, plus a clock warning measured at the last connect (never a refusal)."""
        out = list(n.alerts or [])
        if n.skew_s is not None and abs(n.skew_s) > self.skew_alert_s and not any(a.get("kind") == "clock_skew" for a in out):
            out.append({"kind": "clock_skew", "skew_s": round(n.skew_s), "text": f"The clock of this node differs by {round(abs(n.skew_s) / 60)} min (the connection still works)."})
        return out

    def _on_task_result(self, rec) -> None:
        """A task ended. The self-report is stored with its alerts (GUARD); an analysis is stored whole."""
        if rec.status != "done":
            return
        node = self.registry.get(rec.node_id)
        if rec.task_type == "node_report":
            alerts = evaluate_report(rec.result, node.skew_s if node else None, alert_skew_s=self.skew_alert_s)
            self.registry.set_report(rec.node_id, rec.result, alerts, self.clock())
            for a in alerts:
                self.log.add("node_alert", {"node": rec.node_id, **{k: v for k, v in a.items() if k != "text"}})
        elif rec.task_type == "machine_analysis":
            self.registry.save_analysis(rec.node_id, rec.result, self.clock())

    def _on_cli_task(self, req_id: str, node_id: str, task_type: str) -> None:
        rec = self.tasks.start(node_id, task_type, None, by="OWNER")

        async def finish() -> None:
            await rec.done.wait()
            write_json(self.layer, self.watcher.dir / f"task-res-{req_id}.json", {**rec.view(), "result": rec.result})

        asyncio.ensure_future(finish())

    def _on_cli_unpair(self, req_id: str, node_ref: str) -> None:
        async def finish() -> None:
            res = await self.unpair_node(node_ref)
            write_json(self.layer, self.watcher.dir / f"unpair-res-{req_id}.json", res)

        asyncio.ensure_future(finish())

    async def unpair_node(self, node_ref: str, *, via: str = "cli") -> dict:
        """The owner unpairs a node (contract 2.6). The pin leaves the registry FIRST, so from this moment `_node_session` refuses the node at the channel; then, if it is connected, it
        is told on its signed session (`revoked`) so it forgets this coordinator too, and the connection is closed. The certificate stays in the live TLS context until the next start,
        which is why the refusal does not rest on the handshake alone."""
        hits = [n for n in self.registry.all() if n.node_id == node_ref or n.name == node_ref or n.node_id.startswith(node_ref)]
        if len(hits) != 1:
            return {"status": "not_found" if not hits else "ambiguous", "node": node_ref}
        rec = hits[0]
        self.registry.remove(rec.node_id)
        sess = self._sessions.pop(rec.node_id, None)
        notified = False
        if sess is not None:
            try:
                await sess.send("reject", {"reason": "revoked", "message": PLAIN["revoked"]})
                notified = True
            except Exception:
                pass
            try:
                sess.writer.close()
            except Exception:
                pass
        self.log.add("node_unpaired", {"node": rec.node_id, "name": rec.name, "notified": notified, "via": via}, owner=True)
        return {"status": "unpaired", "node": rec.node_id, "name": rec.name, "notified": notified}

    def _publish_tasks(self) -> None:
        try:
            write_json(self.layer, self.watcher.dir / "tasks.json", {"tasks": [t.view() for t in self.tasks.list()[-50:]], "at": self.clock()})
        except OSError:
            pass

    def analysis_of(self, node_id: str) -> dict | None:
        return self.registry.load_analysis(node_id) if self.registry.get(node_id) else None

    # ---- harness runs (the model drives; see runs.py) ----

    def start_analysis(self, node_id: str, via: str = "web", **kw) -> dict:
        """The owner pressed "Analyze this machine": an agent run starts (an OWNER event)."""
        return self.runs.view(self.runs.start_analysis(node_id, via, **kw).id, events=False)

    def start_options_run(self, node_id: str, via: str = "web", **kw) -> dict:
        return self.runs.view(self.runs.start_options(node_id, via, **kw).id, events=False)

    def start_draft_run(self, via: str = "web", correction: str | None = None, **kw) -> dict:
        return self.runs.view(self.runs.start_draft(via, correction=correction, **kw).id, events=False)

    def approve_draft(self, run_id: str, by: str = "owner") -> dict | None:
        return self.runs.approve_draft(run_id, by)

    def profile_view(self) -> dict:
        return self.runs.profile_view()

    def profile_reset(self) -> int:
        return self.runs.profile_reset()

    def start_lan_run(self, via: str = "web", **kw) -> dict:
        return self.runs.view(self.runs.start_lan(via, **kw).id, events=False)

    def run_view(self, run_id: str) -> dict | None:
        return self.runs.view(run_id)

    def pick_of(self, node_id: str) -> dict | None:
        """The option the owner recorded for a machine, as a summary a screen can show (the whole option stays in the coordinator): None when nothing was picked."""
        doc = self.runs.stored_pick(node_id)
        if doc is None:
            return None
        o = doc["option"]
        return {"node": node_id, "picked_at": doc.get("picked_at"), "option": o["id"], "title": o.get("title"), "model": o.get("model"), "build": o.get("build"), "memory_needed_gib": o.get("memory_needed_gib"),
                "disk_needed_gib": o.get("disk_needed_gib"), "labels": o.get("labels", []), "license": o.get("license"), "license_basis": o.get("license_basis")}

    def start_install_run(self, node_id: str, via: str = "web", **kw) -> dict:
        return self.runs.view(self.runs.start_install(node_id, via, **kw).id, events=False)

    def start_uninstall_run(self, node_id: str, via: str = "web", **kw) -> dict:
        return self.runs.view(self.runs.start_uninstall(node_id, via, **kw).id, events=False)

    def setup_of(self, node_id: str) -> dict | None:
        """What a setup left on a machine, as a summary a screen can show (the plan stays in the coordinator): None when nothing is left to undo."""
        rec = self.runs.stored_setup(node_id)
        if rec is None:
            return None
        left = sorted(int(n) for n, st in rec["status"].items() if st in ("done", "failed"))
        return {"node": node_id, "run": rec["run"], "recorded_at": rec.get("recorded_at"), "plan_hash": rec["plan"]["plan_hash"], "option": rec["plan"]["option_id"], "option_title": rec.get("option_title"), "steps_left": left}

    @property
    def connect_env(self):
        from envrec_coordinator import connect as C
        if self._connect_env is None:
            self._connect_env = C.Env.system(self.state.parent)  # the entry says where the state directory is: the folder that holds `coordinator/`, as for every adapter
        return self._connect_env

    @connect_env.setter
    def connect_env(self, env) -> None:
        self._connect_env = env

    def openwebui_admin(self) -> dict | None:
        from envrec_coordinator import connect as C
        return C.read_admin_account(self.connect_env)

    def start_connect_run(self, host: str | None = None, via: str = "web", **kw) -> dict:
        return self.runs.view(self.runs.start_connect(host, via, **kw).id, events=False)

    def start_disconnect_run(self, host: str, via: str = "web", **kw) -> dict:
        return self.runs.view(self.runs.start_uninstall(f"connect-{host}", via, **kw).id, events=False)

    def connect_step(self, plan: dict, n: int, action: str = "run"):
        """Run one step of a connection plan on THIS computer, by the coordinator's own code, and only when the log holds an OWNER approval of this exact plan hash, step and action (the same rule the task desk
        applies before it sends a step to a node). A plan whose hash is not its own, or one nobody approved, ends `rejected` at once and nothing is touched."""
        from envrec_coordinator import connect as C
        rec = LocalStep()
        asyncio.ensure_future(self._connect_run(rec, plan, n, action, C))
        return rec

    async def _connect_run(self, rec, plan: dict, n: int, action: str, C) -> None:
        t0 = time.monotonic()
        try:
            digest = C.plan_hash(plan)
            if plan.get("plan_hash") != digest:
                rec.status, rec.reason = "rejected", "plan_altered"
            elif not self._owner_approval(digest, n, action):
                rec.status, rec.reason = "rejected", "no_owner_approval"
            else:
                rec.status = "running"
                self.log.add("connect_step_started", {"host": plan.get("option_id"), "step": n, "action": action, "plan_hash": digest})
                rec.result = await asyncio.to_thread(C.run_step, plan, n, action, self.connect_env)
                rec.status = "done"
        except C.StepFailed as exc:
            rec.status, rec.reason = "failed", f"StepFailed: {exc.code}: {exc.text}"
        except Exception as exc:  # a step never takes the coordinator down
            rec.status, rec.reason = "failed", f"StepFailed: {type(exc).__name__}"
        finally:
            rec.duration_s = round(time.monotonic() - t0, 2)
            self.log.add("connect_step_ended", {"host": plan.get("option_id"), "step": n, "action": action, "status": rec.status, "reason": rec.reason[:120]})
            rec.done.set()

    def answer_run(self, run_id: str, qid: str, option: str, by: str = "owner", channel: str | None = None, gesture: str | None = None) -> bool:
        return self.runs.answer(run_id, qid, option, by, channel, gesture)

    def pick_option(self, run_id: str, option_id: str, channel: str | None = None, by: str | None = None) -> bool:
        return self.runs.pick_option(run_id, option_id, channel, by)

    def run_task(self, node_id: str, task_type: str, via: str = "web", params: dict | None = None) -> dict:
        """The owner asks for a task on one node (the web screen and the CLI call this). Returns the task's view; it ends on its own."""
        rec = self.tasks.start(node_id, task_type, params, by="OWNER")
        return rec.view()

    def web_event(self, type: str, payload: dict) -> None:
        self.log.add(type, payload)

    def _owner_approval(self, plan_hash: str, step: int, action: str) -> str | None:
        """The id of the OWNER event in the log that approved exactly this plan, step and action, or None. Only an event tagged OWNER counts: the operator's (CLAUDE-CODE) and the code's never do."""
        for e in reversed(self.log.events("setup_step_approved")):
            p = e.payload
            if e.effective_tag == "OWNER" and p.get("plan_hash") == plan_hash and p.get("step") == step and p.get("action") == action:
                return e.id
        return None

    def _owner_credential_approval(self, plan_hash: str, name: str, action: str) -> str | None:
        """The id of the OWNER confirmation in the log that approved exactly this credential change, or None. The plan (node, access, name, action) is the GUARD's own record and must hash to `plan_hash`; the
        confirmation must be tagged OWNER, approved, bound to that hash and given with a human gesture. A model's tool call, the operator and the code never count."""
        plan = next((e for e in reversed(self.log.events("credential_plan")) if e.payload.get("plan_hash") == plan_hash), None)
        if plan is None or plan.payload.get("name") != name or plan.payload.get("action") != action:
            return None
        p = plan.payload
        if T.credential_plan_hash(str(p.get("node")), str(p.get("access")), name, action) != plan_hash:
            return None
        for e in reversed(self.log.events("owner_confirmation")):
            q = e.payload
            if e.effective_tag == "OWNER" and q.get("plan_hash") == plan_hash and q.get("approved") is True and q.get("mechanism") in OWNER_GESTURES:
                return e.id
        return None

    def _owner_origin(self, task_id: str, task_type: str | None = None, sensitivity: str | None = None) -> str | None:
        """The id of the OWNER event in the log that created or approved exactly this task, or None. Only an event tagged OWNER counts: the operator's (CLAUDE-CODE) and the code's never do. A subtask of an
        owner-approved decomposition counts through the OWNER decision that approved it (`_decomposition_origin`)."""
        if task_id.startswith("adm-"):  # a task of an admission exam: its origin is the owner's approval of that exam (`adm-<id>:<suite task>`)
            aid = task_id.split(":", 1)[0]
            return next((e.id for e in reversed(self.log.events("admission_approved")) if e.effective_tag == "OWNER" and e.payload.get("admission") == aid), None)
        for e in reversed(self.log.events("environment_task_origin")):
            if e.effective_tag == "OWNER" and e.payload.get("task") == task_id:
                return e.id
        return self._decomposition_origin(task_id, task_type, sensitivity)

    def _owner_decision(self, event_id: str, parent: str, subtasks: list[dict]):
        """The OWNER event `event_id` if it is a human decision (a gesture of the OS dialog, the chat host's dialog or the panel) for exactly this decomposition, else None. The authority is the recorded event:
        its tag must be OWNER (never the operator's or the code's), its gesture one of HUMAN_GESTURES, and the decomposition it records must be, character for character, the one being approved."""
        e = next((x for x in self.log.events("decomposition_decision") if x.id == event_id), None)
        if e is None or e.effective_tag != "OWNER" or e.payload.get("gesture") not in ER.HUMAN_GESTURES:
            return None
        try:
            same = ER.decomposition_canon(e.payload.get("parent", ""), e.payload.get("subtasks", [])) == ER.decomposition_canon(parent, subtasks)
        except (KeyError, TypeError):
            return None
        return e if same else None

    def _decomposition_origin(self, task_id: str, task_type: str | None = None, sensitivity: str | None = None) -> str | None:
        """For a subtask: the id of the OWNER decision that approved it, or None. The subtask must have been registered against a recorded decomposition (`decomposition_approved`), by its subtask id from the
        owner's list; the OWNER decision is checked again from the store; the id may be claimed by one task only (the first); and the task actually being sent must be of the listed type and at least the listed level."""
        regs = [e for e in self.log.events("environment_task_origin") if e.payload.get("kind") == "subtask"]
        mine = next((e for e in reversed(regs) if e.payload.get("task") == task_id), None)
        if mine is None:
            return None
        rec = next((e for e in self.log.events("decomposition_approved") if e.payload.get("id") == mine.payload.get("decomposition")), None)
        if rec is None:
            return None
        decided = self._owner_decision(rec.payload.get("owner_event", ""), rec.payload.get("parent", ""), rec.payload.get("subtasks", []))
        if decided is None:
            return None
        listed = next((x for x in rec.payload["subtasks"] if x.get("id") == mine.payload.get("subtask_id")), None)
        if listed is None:
            return None
        first = next(e for e in regs if e.payload.get("decomposition") == rec.payload["id"] and e.payload.get("subtask_id") == listed["id"])
        if first.payload.get("task") != task_id:
            return None  # that subtask id was already claimed by another task
        if task_type is not None and task_type != listed["type"]:
            return None
        if sensitivity is not None and ER.SENSITIVITIES.index(sensitivity) < ER.SENSITIVITIES.index(listed["sensitivity"]):
            return None  # a task routed under a LOWER level than the one the owner listed would be more permissive than approved
        return decided.id

    def own_task(self, task_id: str, *, by: str = "owner", kind: str = "created") -> str:
        """Record that the owner created (`created`) or accepted a planner's suggestion for (`accepted`) this task. Called by the owner's own screens; the operator passes `by="claude-code"` and the event
        is then not the owner's. Returns the event id."""
        return self.log.add("environment_task_origin", {"task": task_id, "kind": kind, **({"by": "claude-code"} if by != "owner" else {})}, owner=True).id

    def record_owner_decision(self, parent_task_id: str, subtasks: list[dict], *, gesture: str, by: str = "owner") -> str:
        """Called by the layer that shows the dialog (the OS dialog, the chat host's, the panel) AFTER a human gesture: records the owner's decision on one exact decomposition (subtasks with their ids, types
        and levels). Returns the event id, which `approve_decomposition` then verifies. A call made by the operator is recorded as not the owner's."""
        listed = [{"id": str(x["id"]), "type": str(x["type"]), "sensitivity": str(x["sensitivity"])} for x in subtasks]
        return self.log.add("decomposition_decision", {"parent": parent_task_id, "subtasks": listed, "gesture": gesture, **({"by": "claude-code"} if by != "owner" else {})}, owner=True).id

    def approve_decomposition(self, parent_task_id: str, subtasks: list[dict], owner_event_id: str):
        """Accept a decomposition on the strength of a recorded OWNER decision. `owner_event_id` must name, in this coordinator's store, an OWNER event from a human gesture for exactly this decomposition; the
        caller's say-so counts for nothing. Returns the decomposition id, or a Refusal (`no_owner_origin`)."""
        from envrec_coordinator.router import strings as RS
        from envrec_coordinator.router.model import Refusal
        ids = [str(x.get("id", "")) for x in subtasks if isinstance(x, dict)]
        decided = self._owner_decision(owner_event_id, parent_task_id, subtasks) if len(ids) == len(subtasks) and len(set(ids)) == len(ids) and all(ids) else None
        if decided is None:
            return Refusal(task_id=parent_task_id, code="no_owner_origin", reason=RS.refusal_text("no_owner_origin"), details={"owner_event": owner_event_id})
        listed = [{"id": str(x["id"]), "type": str(x["type"]), "sensitivity": str(x["sensitivity"])} for x in subtasks]
        did = ER.decomposition_id(parent_task_id, listed, owner_event_id)
        if not any(e.payload.get("id") == did for e in self.log.events("decomposition_approved")):
            self.log.add("decomposition_approved", {"id": did, "parent": parent_task_id, "subtasks": listed, "count": len(listed), "owner_event": owner_event_id, "gesture": decided.payload["gesture"]})
        return did

    def own_subtask(self, task_id: str, decomposition_id: str, subtask_id: str) -> str | None:
        """Register a task as the subtask `subtask_id` of an approved decomposition. The type and level are the owner's listed ones, not the caller's. Returns the OWNER decision it rests on, or None when the
        decomposition is unknown, the id is not on the owner's list, or another task already holds that id (then it needs a new OWNER decision)."""
        rec = next((e for e in self.log.events("decomposition_approved") if e.payload.get("id") == decomposition_id), None)
        listed = next((x for x in rec.payload["subtasks"] if x["id"] == subtask_id), None) if rec is not None else None
        if listed is None:
            return None
        self.log.add("environment_task_origin", {"task": task_id, "kind": "subtask", "decomposition": decomposition_id, "subtask_id": subtask_id, "task_type": listed["type"], "sensitivity": listed["sensitivity"]})
        return self._decomposition_origin(task_id)

    async def stop(self) -> None:
        if self._udp is not None:
            self._udp.close()
        await self.runs.shutdown()
        if self.web is not None:
            await asyncio.to_thread(self.web.stop)  # first: its handlers call back into this loop
        for t in self._tasks:
            t.cancel()
        if self._server is not None:
            self._server.close()
            for w in list(self._conns):  # an open node connection would keep `wait_closed` waiting forever
                w.close()
            await self._server.wait_closed()
        self.log.add("coordinator_stopped", {})
        self.log.close()

    # ---- connections ---------------------------------------------------------------------------------------------------------------------------

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        source = (writer.get_extra_info("peername") or ("?",))[0]
        self._conns.add(writer)
        try:
            der = tls.peer_cert_der(writer)
            if der is None:  # no certificate: a computer that is not paired. The first message says whether it wants to wait for the owner or to try a code.
                try:
                    first = await read_frame(reader, timeout=HELLO_TIMEOUT_S)
                except FrameError:
                    first = {}  # a connection that says nothing valid is refused (and rate-limited) by the pairing desk
                if first.get("type") == "wait_hello":
                    await self.waiting.attend(reader, writer, source, first, pair=self.desk.handle, cert_der=self.identity.cert_der, read_frame=read_frame)
                else:
                    await self.desk.handle(reader, writer, source, self.identity.cert_der, first_frame=first)
            else:
                await self._node_session(reader, writer, der, source)
        except Exception as exc:  # one bad connection never takes the server down
            self.log.add("connection_error", {"source": source, "error": type(exc).__name__})
        finally:
            self._conns.discard(writer)
            writer.close()

    async def _reject(self, sess: "NodeSession | None", node_id: str, reason: str, source: str, writer=None) -> str:
        """Tell the node WHY (a typed reason, signed on the pinned channel) before the connection is closed, and log it."""
        self.log.add("message_rejected", {"node": node_id, "reason": reason, "source": source})
        try:
            if sess is not None:
                await sess.send("reject", {"reason": reason, "message": PLAIN.get(reason, reason)})
            elif writer is not None:
                await write_frame(writer, {"type": "reject", "reason": reason, "message": PLAIN.get(reason, reason)})
        except Exception:
            pass
        return f"message rejected: {reason}"

    async def _node_session(self, reader, writer, cert_der: bytes, source: str) -> None:
        rec = self.registry.by_cert(cert_der)
        if rec is None:  # the handshake should have refused it; if it did not, so does this
            self.log.add("connection_refused", {"source": source, "reason": "unpaired_certificate"})
            await self._reject(None, "?", "unknown_node", source, writer)
            return
        sid = new_challenge()
        sess = NodeSession(self, rec.node_id, writer, sid, rec.public_raw)
        await sess.send("challenge", {"nonce": sid, "server_time": self.clock(), "proto": PROTOCOL})  # freshness without anybody's clock: the node must answer THIS nonce
        try:
            first = await read_frame(reader, timeout=HELLO_TIMEOUT_S)
        except FrameError:
            self.log.add("connection_refused", {"node": rec.node_id, "source": source, "reason": "no_hello"})
            return
        if "sid" not in first:  # an older node that sends a heartbeat first
            await self._reject(sess, rec.node_id, "protocol_version", source)
            return
        try:
            hello = sess.verifier.open(first)
        except EnvelopeRejected as exc:
            await self._reject(sess, rec.node_id, exc.reason, source)
            return
        if hello.type != "hello" or hello.payload.get("nonce") != sid:
            await self._reject(sess, rec.node_id, "challenge_failed", source)
            return
        if hello.payload.get("proto") != PROTOCOL:
            await self._reject(sess, rec.node_id, "protocol_version", source)
            return
        local = hello.payload.get("local_time")
        skew = float(local) - self.clock() if isinstance(local, (int, float)) and not isinstance(local, bool) else None
        sess.skew_s = skew
        self.log.add("node_connected", {"node": rec.node_id, "source": source, **({"skew_s": round(skew, 1)} if skew is not None else {})})
        if skew is not None:
            self.registry.set_skew(rec.node_id, skew)
            if abs(skew) > self.skew_alert_s:  # only a warning: nothing is ever refused for the wall clock
                self.log.add("clock_skew", {"node": rec.node_id, "skew_s": round(skew), "alert_after_s": self.skew_alert_s})
        self._online[rec.node_id] = self._online.get(rec.node_id, 0) + 1
        self._sessions[rec.node_id] = sess
        await sess.send("welcome", {"interval_s": HEARTBEAT_INTERVAL_S, "skew_s": skew})
        self.tasks.start(rec.node_id, "node_report", by="GUARD")  # the node describes itself every time it connects
        reporter = asyncio.ensure_future(self._periodic_report(rec.node_id))
        reason = "closed by the node"
        beats, last_summary, last_card = 0, self.clock(), None
        try:
            while True:
                try:
                    raw = await read_frame(reader, timeout=self.idle_timeout_s)
                except FrameError as exc:
                    reason = str(exc)[:60]
                    break
                try:
                    env = sess.verifier.open(raw)
                except EnvelopeRejected as exc:
                    reason = await self._reject(sess, rec.node_id, exc.reason, source)
                    break
                if env.type == "heartbeat":
                    card = {k: _clip(env.payload.get(k, ""), cap) for k, cap in CAPS.items()}
                    self.registry.heartbeat(rec.node_id, self.clock())
                    beats += 1
                    if card != last_card:
                        last_card = card
                        self.log.add("node_card", {"node": rec.node_id, **card})
                    if self.clock() - last_summary >= self.summary_every_s:
                        self.log.add("heartbeat_summary", {"node": rec.node_id, "beats": beats, "seconds": round(self.clock() - last_summary)})
                        beats, last_summary = 0, self.clock()
                    await sess.send("ack", {"nonce": env.nonce, "interval_s": HEARTBEAT_INTERVAL_S})
                elif env.type == "task_update":
                    self.tasks.on_update(rec.node_id, env.payload)
                elif env.type == "result":
                    self.tasks.on_node_result(rec.node_id, env.payload)
                else:
                    self.log.add("message_rejected", {"node": rec.node_id, "reason": "unknown_type", "type": _clip(env.type, 30), "source": source})
                    await self._reject(sess, rec.node_id, "unknown_type", source)
                    reason = "message rejected: unknown_type"
                    break
        finally:
            reporter.cancel()
            if self._sessions.get(rec.node_id) is sess:
                del self._sessions[rec.node_id]
            self.tasks.node_gone(rec.node_id)
            if beats:
                self.log.add("heartbeat_summary", {"node": rec.node_id, "beats": beats, "seconds": round(self.clock() - last_summary)})
            self._online[rec.node_id] -= 1
            if not self._online[rec.node_id]:
                del self._online[rec.node_id]
            self.log.add("node_disconnected", {"node": rec.node_id, "reason": reason})

    async def _periodic_report(self, node_id: str) -> None:
        while True:
            await asyncio.sleep(self.report_every_s)
            self.tasks.start(node_id, "node_report", by="GUARD")


class NodeSession:
    """One connected node: its session id, the counters of both directions, and a lock so two tasks never interleave their frames."""

    def __init__(self, coord, node_id: str, writer, sid: str, node_public: bytes) -> None:
        self.identity, self.node_id, self.writer, self.sid = coord.identity, node_id, writer, sid
        self.sealer = Sealer(coord.identity, sid)
        self.verifier = SessionVerifier(node_id, node_public, sid)
        self.skew_s: float | None = None
        self._lock = asyncio.Lock()

    async def send(self, type: str, payload: dict) -> None:
        async with self._lock:
            await write_frame(self.writer, self.sealer.seal(type, payload))
