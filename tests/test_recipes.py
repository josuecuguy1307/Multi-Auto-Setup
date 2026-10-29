"""Recipes as data and the one executor: validation, the hash gate, the order approval > undo on disk > run, timeouts as typed hangs, undo newest first."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from envrec.recipes.executor import Artifact, RecipeExecutor, RecipeNotApproved, UndoJournal
from envrec.recipes.model import canonical, problems, recipe_hash, render
from envrec.recipes.shell import quote

RECIPE = {
    "shell": "sh", "root": "/opt/envrec/ws", "port": 8089,
    "steps": [
        {"id": "mkdir", "write": True, "run": ["mkdir", "-p", "{root}"], "undo": ["rm", "-r", "{root}"]},
        {"id": "send_runtime", "write": True, "put": {"artifact": "runtime", "dest": "{root}/rt.bin", "receive": ["sh", "-c", "cat > {dest}"]}, "undo": ["rm", "-f", "{root}/rt.bin"]},
        {"id": "check_runtime", "run": ["sha256sum", "{root}/rt.bin"], "expect_sha256": "runtime"},
        {"id": "start", "write": True, "run": ["{root}/rt.bin", "--port", "{port}", "--host", "127.0.0.1"], "undo": ["pkill", "-f", "{root}/rt.bin"]},
    ],
    "health": ["health-check", "{port}"], "absent": ["ls", "{root}"],
}


def good():
    return copy.deepcopy(RECIPE)


# ---- static checks ---------------------------------------------------------------------------------------------------------------------------------

def test_the_reference_recipe_is_acceptable():
    assert problems(good()) == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda r: r["steps"][0].pop("undo"), "needs an undo"),
    (lambda r: r["steps"][0].update(run=["mkdir", "-p", "/tmp/elsewhere"], undo=["rm", "/tmp/elsewhere"]), "must act under {root}"),
    (lambda r: r.update(root="/x"), "root must be a path"),
    (lambda r: r.update(root="/opt/../etc"), "root must be a path"),
    (lambda r: r.update(shell="fish"), "shell must be one of"),
    (lambda r: r.update(port=80), "port must be"),
    (lambda r: r["steps"][0].update(run="mkdir -p {root} && x"), "argv"),
    (lambda r: r["steps"][0].update(run=["sudo", "mkdir", "{root}"]), "`sudo`"),
    (lambda r: r["steps"][0].update(run=["curl", "http://example.invalid/{root}"]), "`curl`"),
    (lambda r: r["steps"][0].update(run=["powershell", "-EncodedCommand", "AAAA", "{root}"]), "-encodedcommand"),
    (lambda r: r["steps"][0].update(run=["mkdir", "{home}", "{root}"]), "unknown placeholder"),
    (lambda r: r["steps"][1]["put"].update(dest="/etc/rt.bin"), "put.dest must be under {root}"),
    (lambda r: r["steps"][1]["put"].update(artifact="anything"), "put needs artifact"),
    (lambda r: r["steps"][3].update(id="go"), "id `start`"),
    (lambda r: r["steps"].append(copy.deepcopy(r["steps"][0])), "unique id"),
    (lambda r: r.pop("absent"), "absent must be an argv"),
])
def test_each_unacceptable_recipe_is_refused_with_the_reason_named(mutate, expect):
    r = good()
    mutate(r)
    assert any(expect in p for p in problems(r)), problems(r)


def test_rendering_fills_only_the_closed_placeholders():
    assert render(["a", "{root}/x", "{port}"], {"root": "/r", "port": "9"}) == ["a", "/r/x", "9"]
    with pytest.raises(ValueError):
        render(["{nope}"], {"root": "/r"})


@pytest.mark.parametrize("shell,argv,expected", [("sh", ["echo", "a b"], "echo 'a b'"), ("cmd", ["echo", "a b"], 'echo "a b"'), ("powershell", ["echo", "a b", "it's"], "echo 'a b' 'it''s'")])
def test_quoting_is_per_shell(shell, argv, expected):
    assert quote(argv, shell) == expected


# ---- the executor ----------------------------------------------------------------------------------------------------------------------------------

class Remote:
    """A fake target: records every command, and the state of the undo journal at the moment each one ran."""

    def __init__(self, journal, replies=None):
        self.calls, self.journal_at_call, self.replies, self.journal = [], [], replies or {}, journal

    def __call__(self, argv, shell, stdin=None, timeout=60.0):
        self.calls.append((argv, shell, stdin, timeout))
        self.journal_at_call.append(len(self.journal.path.read_text().splitlines()) if self.journal.path.exists() else 0)
        for key, reply in self.replies.items():
            if key in " ".join(argv):
                return reply
        return 0, b"", b""


@pytest.fixture
def setup(tmp_path):
    art = tmp_path / "rt.bin"
    art.write_bytes(b"runtime-bytes")
    sha = hashlib.sha256(b"runtime-bytes").hexdigest()
    journal = UndoJournal(tmp_path / "undo.jsonl")
    shown = []
    remote = Remote(journal, {"sha256sum": (0, f"{sha}  /opt/envrec/ws/rt.bin\n".encode(), b"")})
    ex = RecipeExecutor(good(), recipe_hash(good()), "box", remote, lambda text: shown.append(text) or True, journal, {"runtime": Artifact(art, sha)})
    return ex, remote, journal, shown, art


def test_a_recipe_that_is_not_the_approved_one_runs_nothing(tmp_path):
    journal = UndoJournal(tmp_path / "u.jsonl")
    remote = Remote(journal)
    other = good()
    other["steps"][0]["run"] = ["mkdir", "-p", "{root}/sub"]
    with pytest.raises(RecipeNotApproved, match="hash differs"):
        RecipeExecutor(other, recipe_hash(good()), "box", remote, lambda t: True, journal)
    assert remote.calls == []
    bad = good()
    bad["steps"][0].pop("undo")
    with pytest.raises(RecipeNotApproved, match="not valid"):
        RecipeExecutor(bad, recipe_hash(bad), "box", remote, lambda t: True, journal)


def test_every_write_is_shown_with_its_exact_command_and_undo_and_the_undo_is_on_disk_before_the_command_runs(setup):
    ex, remote, journal, shown, _ = setup
    results = ex.deploy()
    assert [r.status for r in results] == ["ok"] * 4
    assert shown[0] == "mkdir -p /opt/envrec/ws\n  undo: rm -r /opt/envrec/ws"
    assert "send the runtime file" in shown[1] and "sha256" in shown[1] and shown[2].startswith("/opt/envrec/ws/rt.bin --port 8089 --host 127.0.0.1")
    assert len(shown) == 3  # the read-only check asked nothing; each of the three writes asked once
    # the journal already held the entry when the command ran: 1 line before mkdir's own command ran, 2 before the send, 3 before start
    writes = [(c[0][0], n) for c, n in zip(remote.calls, remote.journal_at_call) if c[0][0] in ("mkdir", "sh", "/opt/envrec/ws/rt.bin")]
    assert writes == [("mkdir", 1), ("sh", 2), ("/opt/envrec/ws/rt.bin", 3)]


def test_a_denied_step_stops_everything_after_it(setup):
    ex, remote, journal, _, _ = setup
    answers = iter([True, False])
    ex.ask = lambda text: next(answers)
    results = ex.deploy()
    assert [(r.id, r.status) for r in results] == [("mkdir", "ok"), ("send_runtime", "denied")]
    assert len(remote.calls) == 1 and [r["step"] for r in journal.pending("box")] == ["mkdir"]


def test_a_local_file_that_does_not_match_its_pin_is_never_sent(setup):
    ex, remote, _, _, art = setup
    art.write_bytes(b"tampered")
    results = ex.deploy()
    assert results[-1].status == "refused" and "pinned sha256" in results[-1].detail and not any("cat" in " ".join(c[0]) for c in remote.calls)


def test_a_file_whose_hash_on_the_target_differs_fails_the_step(setup):
    ex, remote, _, _, _ = setup
    remote.replies["sha256sum"] = (0, b"0" * 64 + b"  x\n", b"")
    results = ex.deploy()
    assert results[-1].id == "check_runtime" and results[-1].status == "failed" and "sha256" in results[-1].detail


def test_a_command_that_does_not_answer_in_time_is_a_typed_hang_not_a_wait(setup):
    ex, remote, _, _, _ = setup
    remote.replies["mkdir"] = (124, b"", b"timed out")
    results = ex.deploy()
    assert [(r.id, r.status) for r in results] == [("mkdir", "hang")] and all(c[3] <= 900 for c in remote.calls)


def test_undo_runs_newest_first_asks_for_each_and_the_absent_check_is_what_proves_nothing_is_left(setup):
    ex, remote, journal, shown, _ = setup
    ex.deploy()
    shown.clear()
    remote.calls.clear()
    results = ex.undo()
    assert [r.id for r in results] == ["start", "send_runtime", "mkdir"] and [c[0][0] for c in remote.calls] == ["pkill", "rm", "rm"]
    assert all(s.startswith("undo `") for s in shown) and journal.pending("box") == []
    remote.replies["ls"] = (1, b"", b"No such file")
    assert ex.nothing_left() == (True, "nothing left under the root")
    remote.replies["ls"] = (0, b"rt.bin\n", b"")
    assert ex.nothing_left()[0] is False


def test_a_refused_undo_step_stays_pending(setup):
    ex, remote, journal, _, _ = setup
    ex.deploy()
    ex.ask = lambda text: False
    assert [r.status for r in ex.undo()] == ["denied"] and len(journal.pending("box")) == 3


# ---- the dry run and the approval hash ----------------------------------------------------------------------------------------------------------

def test_the_dry_run_is_clean_for_a_good_recipe_and_names_a_recipe_that_cannot_be_rendered():
    from envrec.recipes.dryrun import dry_run
    assert dry_run(good()) == []
    r = good()
    r["steps"][2]["run"] = ["sha256sum", "{root}/{where}"]
    assert any("placeholder" in p for p in problems(r)) or any("placeholder" in p for p in dry_run(r))


def test_the_commands_of_a_recipe_are_part_of_what_the_owner_approves(tmp_path):
    from envrec.skills.lib import Library
    from envrec.skills.verify import Approvals, approval_text, script_hash
    from envrec.skills.author import stage_draft
    draft = {"name": "workspace-toy", "description": "Use when a toy computer needs a workspace.", "body": "toy", "script": "def parse(inp):\n    return {}\n",
             "steps": [{"id": "probe", "tool": "run_command", "args": {"argv": ["true"]}}], "produces": ["host.toy"], "recipe": good(),
             "tests": [{"name": "yes", "captures": {"probe": "x"}, "facts": {"host.toy": 1}}, {"name": "no", "captures": {"probe": "y"}, "match": False}]}
    folder = stage_draft(draft, tmp_path)
    from envrec.skills.skillmd import load_skill
    sk = load_skill(folder)
    h1 = script_hash(sk)
    text = approval_text(sk)
    assert "RECIPE" in text and "mkdir -p /opt/envrec/ws" in text and "undo: rm -r /opt/envrec/ws" in text and "pinned" in text
    changed = good()
    changed["steps"][0]["run"] = ["mkdir", "-p", "{root}/other"]
    draft["recipe"] = changed
    assert script_hash(load_skill(stage_draft(draft, tmp_path / "b"))) != h1  # one changed command voids the approval
    lib = Library(root=tmp_path / "lib")
    ap = Approvals(lib.root)
    assert not ap.approved(sk) and ap.approve(sk, text, lambda t: True) and ap.approved(sk)
    assert json.loads(ap.path.read_text())["workspace-toy"]["recipe_sha256"] == recipe_hash(good())


def test_a_second_deploy_of_the_same_steps_is_tracked_on_its_own_in_the_journal(setup):
    ex, remote, journal, shown, _ = setup
    ex.deploy()
    ex.undo()
    assert journal.pending("box") == []
    ex.deploy()
    assert [r["step"] for r in journal.pending("box")] == ["start", "send_runtime", "mkdir"]  # not hidden by the first run's `undone` rows
    assert [r.status for r in ex.undo()] == ["ok", "ok", "ok"] and journal.pending("box") == []
