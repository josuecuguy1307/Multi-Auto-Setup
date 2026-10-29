"""Mission 1 end to end with FAKES: an invented operating system ("zorg") that no skill knows, a scripted model as the actor, a fake owner who answers prompts, and a fake model server.

It proves the machinery, not a real computer: the door (key, one-time link, pairing digits, host-key pin), the unknown ladder (read, research, propose a recipe skill, sandbox, baseline,
verifier, the owner's one approval of the exact code by hash), the deploy with a prompt and an undo for every write, verification by code, a task through the tunnel, the undo with proof
that nothing is left, and a SECOND run on the same operating system that does none of the ladder again. Label: FIXTURE / SIMULATED."""
from __future__ import annotations

import asyncio
import base64
import collections
import hashlib
import ipaddress
import json
import re
import sys
import urllib.request
from pathlib import Path

import pytest

from envrec.discovery import LocalCollector, Salts
from envrec.discovery.types import this_platform
from envrec.doors.pair_code import pairing_code
from envrec.loop import Loop, RunConfig
from envrec.mission import EntryLimits, MissionCard, SshBlock, TargetConfig, TargetEntry, Transport
from envrec.model.scripted import ScriptedModel, scripted_profile
from envrec.protocols import ProtocolSet
from envrec.recipes.executor import Artifact, UndoJournal
from envrec.remote.provision import sync_targets
from envrec.remote.runner import RemoteRunner
from envrec.skills.lib import Library
from envrec.skills.verify import ScriptedBaseline, ScriptedVerifier, VerifierVerdict
from envrec.skills.workbench import Shelf, Workbench
from envrec.store import Store
from envrec.tools.bridge import RegistryAdapter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "fakes"))
from zorg import FakeModelServer, Zorg  # noqa: E402
HOST_PUB = "ssh-ed25519 " + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(32))).decode()
GOAL = "Set up a working small-model workspace on the enrolled laptop, verify it works, and report."

SCRIPT = '''"""Facts about a zorg computer from what its read-only commands printed."""
import re

from envrec_sbx import Caps, Out


def parse(inp):
    caps = Caps(inp)
    out = Out(caps)
    ver = caps.get("zver")
    m = re.match(r"^ZORG\\s+\\S+\\s+(\\S+)", ver.text.strip()) if ver is not None and ver.ok else None
    if not m:
        return out.result()
    out.fact("os.family", "zorg", src="zver", quote=ver.text.strip().splitlines()[0][:12])
    out.fact("os.arch", m.group(1).lower(), src="zver", quote=m.group(1))
    for name, key, rx in (("zdisk", "hw.disk_free_gib", r"free\\s+(\\d+)"), ("zram", "hw.ram_total_gib", r"ram\\s+(\\d+)")):
        c = caps.get(name)
        mm = re.search(rx, c.text) if c is not None and c.ok else None
        if mm:
            out.fact(key, int(mm.group(1)), src=name, quote=mm.group(0))
    return out.result()
'''

RECIPE = {
    "shell": "cmd", "root": "Z:\\envrec\\ws", "port": 8089,
    "steps": [
        {"id": "mkdir", "write": True, "run": ["zmk", "{root}"], "undo": ["zrm", "{root}"]},
        {"id": "send_runtime", "write": True, "put": {"artifact": "runtime", "dest": "{root}\\rt.bin", "receive": ["ztee", "{dest}"]}, "undo": ["zrm", "{root}\\rt.bin"]},
        {"id": "check_runtime", "run": ["zsum", "{root}\\rt.bin"], "expect_sha256": "runtime"},
        {"id": "send_model", "write": True, "put": {"artifact": "model", "dest": "{root}\\toy-model.gguf", "receive": ["ztee", "{dest}"]}, "undo": ["zrm", "{root}\\toy-model.gguf"]},
        {"id": "check_model", "run": ["zsum", "{root}\\toy-model.gguf"], "expect_sha256": "model"},
        {"id": "start", "write": True, "run": ["zrun", "{root}\\rt.bin", "--port", "{port}", "--host", "127.0.0.1"], "undo": ["zkill", "{root}\\rt.bin"]},
    ],
    "health": ["zhealth", "{port}"], "absent": ["zls", "{root}"], "bind_check": ["znet", "{port}"], "rss": ["zmem", "{root}\\rt.bin"],
}


def draft():
    return {
        "name": "workspace-zorg", "description": "Use when the enrolled computer runs zorg and needs a small-model workspace.", "body": "Zorg computers answer zver, zdisk and zram. The recipe puts the workspace under Z:\\envrec\\ws.",
        "script": SCRIPT, "tier": "read", "protocol": "connect", "ring": 2,
        "steps": [{"id": "ver", "tool": "remote_read", "args": {"shell": "cmd", "argv": ["zver"], "name": "zver"}},
                  {"id": "disk", "tool": "remote_read", "args": {"shell": "cmd", "argv": ["zdisk"], "name": "zdisk"}},
                  {"id": "ram", "tool": "remote_read", "args": {"shell": "cmd", "argv": ["zram"], "name": "zram"}}],
        "produces": ["os.family", "os.arch", "hw.disk_free_gib", "hw.ram_total_gib"],
        "signature": {"facts": [{"key": "os.family", "regex": "^zorg$"}]},
        "recipe": RECIPE,
        "tests": [{"name": "zorg-box", "platform": "linux", "captures": {"zver": "@capture:zver", "zdisk": "@capture:zdisk", "zram": "@capture:zram"},
                   "facts": {"os.family": "zorg", "os.arch": "x64", "hw.disk_free_gib": 40, "hw.ram_total_gib": 16}},
                  {"name": "not-zorg", "platform": "linux", "match": False, "captures": {"zver": "'zver' is not recognized as an internal or external command.", "zdisk": "x", "zram": "y"}}],
    }


def j(**kw):
    return json.dumps(kw)


def call(tool, **args):
    return j(action="call_tool", tool=tool, args=args, why="next step")


def adv(to):
    return j(action="advance", to=to)


class World:
    """Everything one run needs, built over a temp directory; `owner` is the fake owner (every prompt is answered yes and recorded)."""

    def __init__(self, tmp: Path, computer: Zorg | None = None) -> None:
        import shutil
        self.tmp = tmp
        shutil.copytree(ROOT / "skills", tmp / "skills", ignore=shutil.ignore_patterns("generated", "state", "__pycache__")) if not (tmp / "skills").exists() else None
        self.lib = Library(root=tmp / "skills").load()
        self.computer = computer or Zorg()
        self.server = FakeModelServer(self.computer)
        self.prompts: list[str] = []
        self.shown: list[str] = []
        self.door_files: list[bytes] = []
        self.link_events: list = []
        self.baseline = ScriptedBaseline({})
        self.verifier = ScriptedVerifier([VerifierVerdict(True, False, ["every value is quoted from the capture"])])
        self.shelf = Shelf()
        self.owner_refuses: str | None = None
        for role, data in (("runtime", b"runtime-bytes"), ("model", b"model-bytes")):
            (tmp / role).write_bytes(data)
        self.arts = {r: Artifact(tmp / r, hashlib.sha256((tmp / r).read_bytes()).hexdigest()) for r in ("runtime", "model")}
        import os
        os.environ["E2E_KEY"] = str(tmp / "key")
        self.entry = TargetEntry(id="toy-box", name="Toy box", host="192.0.2.50", allowed_transports=[Transport.ssh], ssh=SshBlock(user="owner", key_env="E2E_KEY"),
                                 limits=EntryLimits(max_disk_gb=20, max_model_params="1B"), owner_confirmed=True)
        self.runner = RemoteRunner(TargetConfig(targets=[self.entry]), exec_fn=self.computer.ssh_exec)

    def owner(self, text: str) -> bool:
        self.prompts.append(text)
        return not (self.owner_refuses and self.owner_refuses in text)

    peer_opens = True  # the fake owner opens the link on the other computer
    peer_runs_the_file = True  # ...and the file runs and reports back

    def show(self, message: str) -> None:
        self.shown.append(message)
        url = re.search(r"http://\S+", message).group(0)
        if not self.peer_opens:
            return
        with urllib.request.urlopen(url, timeout=3) as r:  # the owner opens the link on the other computer
            self.door_files.append(r.read())
        if self.peer_runs_the_file:  # the setup file runs there and reports who that computer is (the address is the one the connection came from)
            body = json.dumps({"user": "owner", "computer": "TOY-BOX", "os": "Zorg 4.2", "host_key": HOST_PUB}).encode()
            urllib.request.urlopen(urllib.request.Request(url.rsplit("/", 1)[0] + "/report", data=body, headers={"Content-Type": "application/json"}, method="POST"), timeout=3).read()

    def params(self) -> dict:
        return {
            "remote_runner": self.runner, "remote_target": "toy-box", "remote_host": "192.0.2.50", "library": self.lib, "journal": UndoJournal(self.tmp / "undo.jsonl"),
            "candidates_fn": lambda facts: [{"id": "toy-q4"}] if facts.get("os.arch") == "x64" and (facts.get("hw.ram_total_gib") or 0) >= 4 and (facts.get("hw.disk_free_gib") or 0) >= 2 else [],
            "artifacts_for": lambda pair: dict(self.arts), "expected_model_fn": lambda pair: {"entry_id": pair, "file": "toy-model.gguf"}, "open_tunnel": self.server.open,
            "ssh_dir": self.tmp / "ssh", "lan_address": "127.0.0.1", "link_options": {"check": ipaddress.IPv4Address}, "show": self.show, "emit": lambda k, i: self.link_events.append((k, i)),
            "ask_code": lambda q: pairing_code(HOST_PUB), "known_hosts": self.tmp / "known_hosts", "scan": lambda host, port: HOST_PUB, "proposals_dir": self.tmp / "proposals", "door_wait_s": 2,
            "door_name": "lan-peer",
        }

    def run(self, replies: list[str], *, max_steps: int = 80, stop_at: str | None = None, door: bool = False):
        store = Store(self.tmp / f"store{len(list(self.tmp.glob('store*')))}")
        from envrec.mission import LocalBlock
        door_entry = TargetEntry(id="this-mac-door", name="This Mac door", host="127.0.0.1", allowed_transports=[Transport.local], local=LocalBlock(scope="door"),
                                 limits=EntryLimits(max_disk_gb=1, max_model_params="1B"), owner_confirmed=True)
        cfg = TargetConfig(targets=[door_entry] if door else [self.entry])
        target = sync_targets(store, cfg)["this-mac-door" if door else "toy-box"]
        reg = RegistryAdapter(collector=LocalCollector(), salts=Salts(self.tmp / "salt"), library=self.lib, shelf=self.shelf, params=self.params(), approve=self.owner, ask_skill_approval=self.owner)
        wb = Workbench(self.lib, self.tmp / "stage", docs_dir=ROOT / "tests" / "fakes" / "docs", verifier=self.verifier, baseline=self.baseline, shelf=self.shelf)
        model = ScriptedModel(replies, scripted_profile())
        loop = Loop(store, [reg], model, config=RunConfig(max_steps=max_steps, max_seconds=120, protocols=ProtocolSet.from_library(self.lib), library=self.lib, stop_at=stop_at), workbench=wb,
                    ask_human=lambda q: None, mission=MissionCard(goal=GOAL, scope="the enrolled computer"))
        summary = asyncio.run(loop.run(target))
        return summary, store, target, model


DOOR_RUN = [call("ssh_key_create"), adv("serve"), call("ssh_door_serve"), adv("wait"), call("ssh_door_wait"), adv("pair"), call("ssh_door_pair"), adv("propose"), call("door_propose_target"), adv("done")]

FIRST_RUN = [
    call("route_check"), adv("identify"),
    call("skill.remote_os_probe"), call("remote_read", shell="cmd", argv=["zver"], name="zver"), call("remote_read", shell="cmd", argv=["zdisk"], name="zdisk"),
    call("remote_read", shell="cmd", argv=["zram"], name="zram"), adv("plan"),
    call("plan_workspace"), adv("research"), j(action="research", question="how to set up a workspace on a zorg computer"), adv("generate"),
    j(action="propose_skill", skill_draft=draft()), adv("plan"),
    call("plan_workspace"), adv("deploy"), call("deploy_workspace", pair="toy-q4"), adv("verify"), call("verify_workspace"), adv("task"), call("run_task_via_tunnel"), adv("undo"),
    call("undo_workspace"), adv("done"),
]


@pytest.fixture
def docs():
    d = ROOT / "tests" / "fakes" / "docs"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "zorg-workspace.md"
    if not f.exists():
        f.write_text("# Setting up a workspace on a zorg computer\n\nA zorg computer answers zver, zdisk and zram. Make a folder with zmk, store files with ztee, check them with zsum, start a program with zrun "
                     "and stop it with zkill. Remove a folder with zrm.\n\nA workspace server must listen on 127.0.0.1 only.\n")
    return f


def tags(store):
    return collections.Counter(e.tag for e in store.events())


def test_the_whole_flow_on_an_operating_system_nobody_wrote_a_skill_for(tmp_path, docs):
    w = World(tmp_path)
    summary, store, target, model = w.run(list(FIRST_RUN))
    with store:
        facts = {f.key: f for f in store.current_facts(target.id)}
        assert summary.stop_reason == "protocol_done", (summary.stop_reason, [e.payload for e in store.events(type="action_rejected")][-3:], summary.tools_run)
        # the ladder: unknown family, research found the document, the skill was kept after the baseline and the verifier, the owner approved the exact code once
        assert facts["os.family"].value == "zorg" and facts["workspace.doc"].value.startswith("zorg-workspace.md")
        assert facts["skill.saved"].value == "workspace-zorg" and len(w.verifier.inputs) == 1 and w.baseline.calls == 1
        asked_code = [p for p in w.prompts if p.startswith("Run the skill 'workspace-zorg'")]
        assert len(asked_code) == 1 and "RECIPE" in asked_code[0] and "zrun Z:\\envrec\\ws\\rt.bin --port 8089 --host 127.0.0.1" in asked_code[0]
        # every write was shown with its exact command AND its undo, and the undo was on disk first
        writes = [p for p in w.prompts if "\n  undo: " in p]
        assert [p.split("\n")[0] for p in writes if p.startswith("zmk") or p.startswith("zrun")] == ["zmk Z:\\envrec\\ws", "zrun Z:\\envrec\\ws\\rt.bin --port 8089 --host 127.0.0.1"]
        assert len([p for p in writes if p.startswith("send the")]) == 2
        # verification by code, then the task, then the undo with the proof
        assert facts["host.model_server"].status.value == "verified" and facts["host.model_tokens_per_s"].value == 11.5 and facts["host.model_rss_mib"].value == 512.0
        assert facts["workspace.task_passed"].value is True and facts["workspace.removed"].value is True
        assert not w.computer.running and w.computer.files == {} and w.computer.dirs == set() and not w.server.httpd
        assert UndoJournal(tmp_path / "undo.jsonl").pending("toy-box") == []
        # the actors: the model chose every action; nothing was forced by code
        c = tags(store)
        assert c["MODEL"] > 20 and c["SCRIPT"] == 0 and c["OWNER"] >= 10
        assert [h["by"] for h in summary.protocol_history[target.id] if h["by"] != "start"] == ["model"] * (len(summary.protocol_history[target.id]) - 1)
        assert summary.model_calls == len(FIRST_RUN) and summary.hangs == 0


SECOND_RUN = [
    call("route_check"), adv("identify"), adv("plan"),
    call("plan_workspace"), adv("deploy"), call("deploy_workspace", pair="toy-q4"), adv("verify"), call("verify_workspace"), adv("task"), call("run_task_via_tunnel"), adv("undo"),
    call("undo_workspace"), adv("done"),
]


def test_a_second_run_on_the_same_operating_system_climbs_no_ladder_and_asks_for_no_code_approval(tmp_path, docs):
    w = World(tmp_path)
    w.run(list(FIRST_RUN))
    verifier_calls, baseline_calls = len(w.verifier.inputs), w.baseline.calls
    w.prompts.clear()
    # a fresh run: a new store, the skill library kept
    summary, store, target, model = w.run(list(SECOND_RUN))
    with store:
        assert summary.stop_reason == "protocol_done", (summary.stop_reason, summary.tools_run, [e.payload for e in store.events(type="action_rejected")][-3:])
        assert len(w.verifier.inputs) == verifier_calls and w.baseline.calls == baseline_calls  # no baseline, no verifier, no authoring in the second run
        assert not [p for p in w.prompts if p.startswith("Run the skill 'workspace-zorg'")] and not [p for p in w.prompts if "RECIPE" in p]  # the code was approved once, by hash
        assert "research" not in [e.payload["action"] for e in store.events(type="action_chosen")] and "propose_skill" not in [e.payload["action"] for e in store.events(type="action_chosen")]
        facts = {f.key: f.value for f in store.current_facts(target.id)}
        assert facts["os.family"] == "zorg" and facts["workspace.recipe"] == "workspace-zorg" and facts["workspace.removed"] is True and facts["workspace.task_passed"] is True
        assert summary.model_calls == len(SECOND_RUN) < len(FIRST_RUN)
        scripted = [e.type for e in store.events() if e.tag == "SCRIPT"]
        assert scripted == ["first_rung_identify"]  # reuse is the loop running a stored skill by itself, tagged SCRIPT on purpose; the first run (the mission) has none
        # the skill was run by the loop at the first rung, not chosen by the model
        assert any(e.payload.get("skill") == "workspace-zorg" for e in store.events(type="first_rung_identify"))


# ---- failures, each typed and each leaving the way back --------------------------------------------------------------------------------------------

LADDER_TO_DEPLOY = FIRST_RUN[: FIRST_RUN.index(call("deploy_workspace", pair="toy-q4"))]


def facts_of(store, target):
    return {f.key: f for f in store.current_facts(target.id)}


def test_a_computer_that_stops_answering_during_the_deploy_is_a_typed_hang_and_everything_so_far_can_be_undone(tmp_path, docs):
    computer = Zorg()
    w = World(tmp_path, computer)
    computer.hang_on = "zrun"
    summary, store, target, _ = w.run(LADDER_TO_DEPLOY + [call("deploy_workspace", pair="toy-q4"), adv("undo"), call("undo_workspace"), adv("done")])
    with store:
        deploy = [e.payload for e in store.events(type="tool_result") if e.payload["tool"] == "deploy_workspace"][0]
        assert deploy["ok"] is False and "HANG: step start" in deploy["error"] and summary.hangs == 1
        computer.hang_on = None
        assert UndoJournal(tmp_path / "undo.jsonl").pending("toy-box") == [] and computer.files == {} and computer.dirs == set()  # the undo ran: the journal had the entries before the hang


def test_the_owner_refusing_a_write_stops_the_deploy_and_the_rest_is_never_sent(tmp_path, docs):
    w = World(tmp_path)
    w.owner_refuses = "127.0.0.1\n  undo: zkill"
    summary, store, target, _ = w.run(LADDER_TO_DEPLOY + [call("deploy_workspace", pair="toy-q4"), adv("undo"), call("undo_workspace"), adv("done")])
    with store:
        f = facts_of(store, target)
        assert f["workspace.deployed"].value is False and not w.computer.running and not any(c.startswith("zrun") for c in w.computer.log)
        assert f["workspace.removed"].value is True and w.computer.files == {}  # what had been written was undone, each undo asked for


def test_a_pair_that_is_not_one_of_the_candidates_is_refused_before_anything_is_sent(tmp_path, docs):
    w = World(tmp_path)
    summary, store, target, _ = w.run(LADDER_TO_DEPLOY + [call("deploy_workspace", pair="some-bigger-model")])
    with store:
        err = [e.payload["error"] for e in store.events(type="tool_result") if e.payload["tool"] == "deploy_workspace"]
        assert err and "pair must be one of workspace.candidates" in err[0] and not any(c.startswith("zmk") for c in w.computer.log)


def test_the_owner_not_approving_the_recipe_code_runs_nothing(tmp_path, docs):
    w = World(tmp_path)
    w.owner_refuses = "Run the skill 'workspace-zorg'"
    summary, store, target, _ = w.run(LADDER_TO_DEPLOY + [call("deploy_workspace", pair="toy-q4")])
    with store:
        err = [e.payload["error"] for e in store.events(type="tool_result") if e.payload["tool"] == "deploy_workspace"]
        assert err and "did not approve this recipe" in err[0] and not any(c.startswith("zmk") for c in w.computer.log)


def test_a_recipe_changed_after_the_owner_approved_it_is_not_run(tmp_path, docs):
    w = World(tmp_path)
    w.run(FIRST_RUN)  # approved once
    sk = w.lib.get("workspace-zorg")
    meta = json.loads((sk.path / "skill.meta.json").read_text())
    meta["recipe"]["steps"][0]["run"] = ["zmk", "{root}\\other"]
    (sk.path / "skill.meta.json").write_text(json.dumps(meta))
    w.lib = Library(root=tmp_path / "skills").load()
    w.prompts.clear()
    summary, store, target, _ = w.run(SECOND_RUN[:7] + [call("deploy_workspace", pair="toy-q4")])
    with store:
        assert any("Run the skill 'workspace-zorg'" in p for p in w.prompts)  # the changed code needs the owner again: one changed command voids the approval


def test_a_read_only_run_stops_at_the_named_step_and_nothing_is_deployed(tmp_path, docs):
    w = World(tmp_path)
    store_replies = FIRST_RUN[: FIRST_RUN.index(adv("research")) + 1] + [call("deploy_workspace", pair="toy-q4")]
    summary, store, target, _ = w.run(store_replies, stop_at="research")
    with store:
        assert summary.stop_reason == "stop_state_reached" and "deploy_workspace" not in summary.tools_run and w.computer.files == {} and not w.computer.running
        assert facts_of(store, target)["os.family"].status.value == "unknown"  # it read the system and stopped


def test_a_failed_read_leaves_its_first_error_line_as_a_short_quoted_fact_so_the_model_can_learn_the_shell(tmp_path, docs):
    w = World(tmp_path)
    summary, store, target, _ = w.run([call("route_check"), adv("identify"),
                                       call("remote_read", shell="sh", argv=["uname", "-s"], name="probe")])
    with store:
        f = facts_of(store, target)
        assert f["route.read_probe"].value == "exit 1, 0 bytes" and f["route.read_probe_error"].value == "'uname' is not recognized as an internal or external command."


# ---- the door, before enrollment: the other computer is discovered, never typed -----------------------------------------------------------------

def run_door(w, replies):
    return w.run(replies, door=True)


def test_the_door_finds_the_computer_by_itself_and_only_proposes_it(tmp_path, docs):
    w = World(tmp_path)
    summary, store, target, _ = run_door(w, DOOR_RUN)
    with store:
        f = facts_of(store, target)
        assert summary.stop_reason == "protocol_done", (summary.stop_reason, [e.payload for e in store.events(type="action_rejected")][-3:])
        assert f["door.peer_address"].value == "127.0.0.1" and f["door.peer_user"].value == "owner" and f["door.peer_computer"].value == "TOY-BOX"  # the address is the connection's, the rest is what the file reported
        assert f["door.paired"].value is True and f["door.host_fingerprint"].value.startswith("SHA256:") and (tmp_path / "known_hosts").read_text().strip() == f"127.0.0.1 {HOST_PUB}"
        link = w.shown[0].split("\n")[1].strip()
        assert not any(link in json.dumps(e.payload) for e in store.events()) and len(w.door_files) == 1 and b"@@" not in w.door_files[0]
        prop = (tmp_path / "proposals" / "targets.proposed.yaml").read_text()
        assert "ssh_user: owner" in prop and "host: 127.0.0.1" in prop and "authorizes anything" in prop and not (tmp_path / "targets.yaml").exists()  # a proposal, never an enrollment
        assert tags(store)["SCRIPT"] == 0 and tags(store)["OWNER"] >= 4 and tags(store)["MODEL"] >= 10


def test_the_proposal_is_enrolled_by_the_owner_typing_the_name_and_yes_without_typing_an_address_or_a_user(tmp_path, docs):
    from envrec.doors.proposal import read_target_proposal
    from envrec.enroll.enroll import enroll
    from envrec.inventory.report import Candidate
    from envrec.mission import load_targets
    w = World(tmp_path)
    run_door(w, DOOR_RUN)
    p = read_target_proposal(tmp_path / "proposals" / "targets.proposed.yaml")
    asked = []
    answers = iter(["TOY-BOX", "yes"])
    with Store(tmp_path / "es") as st:
        res = enroll(Candidate(name=p["name"], kind="host", seen_via="the door", host=p["host"]), targets_path=tmp_path / "targets.yaml", store=st, check=lambda e, t: True, transport=Transport.ssh,
                     prefill={"ssh_user": p["ssh_user"], "ssh_port": 22}, input_fn=lambda q: (asked.append(q), next(answers))[1], output_fn=lambda s: None, is_tty=lambda: True)
    assert res.enrolled and [q for q in asked if "address" in q.lower() or "ssh user" in q.lower()] == []
    entry = load_targets(tmp_path / "targets.yaml").targets[0]
    assert entry.host == "127.0.0.1" and entry.ssh.user == "owner" and entry.id == "toy-box"


def test_digits_that_do_not_match_the_host_key_pin_nothing_and_the_door_stays_unpaired(tmp_path, docs):
    w = World(tmp_path)
    params = w.params
    w.params = lambda: {**params(), "ask_code": lambda q: "000000" if pairing_code(HOST_PUB) != "000000" else "111111"}
    summary, store, target, _ = run_door(w, DOOR_RUN[:7])
    with store:
        assert facts_of(store, target)["door.paired"].value is False and not (tmp_path / "known_hosts").exists()


def test_a_host_key_that_is_not_the_one_the_file_reported_is_not_pinned_even_with_the_right_digits(tmp_path, docs):
    w = World(tmp_path)
    other = "ssh-ed25519 " + base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes(range(50, 82))).decode()
    params = w.params
    w.params = lambda: {**params(), "scan": lambda host, port: other, "ask_code": lambda q: pairing_code(other)}  # the Mac sees a different key than the file reported
    summary, store, target, _ = run_door(w, DOOR_RUN[:7])
    with store:
        assert facts_of(store, target)["door.paired"].value is False and not (tmp_path / "known_hosts").exists()


def test_a_link_nobody_opens_ends_with_a_typed_reason_that_says_the_computers_cannot_reach_each_other_and_scans_nothing(tmp_path, docs):
    w = World(tmp_path)
    w.peer_opens = False
    summary, store, target, _ = run_door(w, DOOR_RUN[:5])
    with store:
        err = [e.payload["error"] for e in store.events(type="tool_result") if e.payload["tool"] == "ssh_door_wait"]
        assert err and "NOT_REACHED" in err[0] and "reach each other" in err[0] and "hotspot or a home router" in err[0] and "Nothing on the network was scanned" in err[0]
        assert not w.computer.log  # nothing was sent anywhere


def test_a_file_that_was_downloaded_but_never_reported_is_a_different_typed_reason(tmp_path, docs):
    w = World(tmp_path)
    w.peer_runs_the_file = False
    summary, store, target, _ = run_door(w, DOOR_RUN[:5])
    with store:
        err = [e.payload["error"] for e in store.events(type="tool_result") if e.payload["tool"] == "ssh_door_wait"]
        assert err and "NOT_REPORTED" in err[0] and "NOT_REACHED" not in err[0]



def test_the_wait_lasts_as_long_as_the_link_so_the_run_cannot_end_and_kill_the_server_before_the_owner_opens_it(tmp_path, docs):
    import time
    w = World(tmp_path)
    w.peer_opens = False
    params = w.params
    w.params = lambda: {k: v for k, v in {**params(), "link_options": {"check": ipaddress.IPv4Address, "ttl_s": 1.5}}.items() if k != "door_wait_s"}
    t0 = time.monotonic()
    summary, store, target, _ = run_door(w, DOOR_RUN[:5])
    with store:
        assert 1.4 < time.monotonic() - t0 < 6  # it waited for the link's whole life, not for a shorter fixed time
        assert any("NOT_REACHED" in e.payload["error"] for e in store.events(type="tool_result") if e.payload["tool"] == "ssh_door_wait")


# ---- the door's own events are in the append-only log, not only printed ---------------------------------------------------------------------------

def link_events_in(store):
    return [e for e in store.events() if e.type.startswith("setup_link_")]


def test_the_links_events_are_written_to_the_append_only_log_in_order_tagged_as_the_codes_record(tmp_path, docs):
    w = World(tmp_path)
    summary, store, target, _ = run_door(w, DOOR_RUN)
    with store:
        evs = link_events_in(store)
        kinds = [e.type for e in evs]
        assert kinds[0] == "setup_link_started" and "setup_link_served" in kinds and kinds[-1] == "setup_link_reported", kinds
        assert kinds.index("setup_link_served") < kinds.index("setup_link_reported")
        assert {e.effective_tag for e in evs} == {"GUARD"}  # the code recorded what it saw; it decided nothing
        served = next(e for e in evs if e.type == "setup_link_served")
        assert served.payload["client"] == "127.0.0.1" and served.payload["bytes"] == len(w.door_files[0])
        assert [k for k, _ in w.link_events] == kinds  # the printed copy and the stored one say the same, in the same order
        link = w.shown[0].split("\n")[1].strip()
        token = link.split("/")[-2]
        assert not any(token in json.dumps(e.payload) for e in store.events())  # the bearer token is still in no event
        seqs = {e.id: i for i, e in enumerate(store.events())}
        wait_done = next(e for e in store.events(type="tool_result") if e.payload["tool"] == "ssh_door_wait")
        assert seqs[evs[0].id] < seqs[wait_done.id]


def test_a_file_that_was_downloaded_but_never_reported_leaves_the_download_in_the_log_and_no_report(tmp_path, docs):
    w = World(tmp_path)
    w.peer_runs_the_file = False
    summary, store, target, _ = run_door(w, DOOR_RUN[:5])
    with store:
        kinds = [e.type for e in link_events_in(store)]
        assert "setup_link_started" in kinds and "setup_link_served" in kinds and "setup_link_reported" not in kinds, kinds


def test_a_link_nobody_opens_leaves_it_started_and_never_served_in_the_log(tmp_path, docs):
    w = World(tmp_path)
    w.peer_opens = False
    summary, store, target, _ = run_door(w, DOOR_RUN[:5])
    with store:
        kinds = [e.type for e in link_events_in(store)]
        assert "setup_link_started" in kinds and "setup_link_served" not in kinds and "setup_link_reported" not in kinds, kinds
