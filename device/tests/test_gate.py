"""Guardrails 2 and 6 (MISSION 4.1): the policy gate writes only with a fresh signed approval for the exact action it recomputes, records the
undo in the audit log before touching anything, and runs an undo only if that undo was bound to an approval this gate consumed.

Spec: DECISIONS.md D-003 (gate is the only writer), D-004 (undo_hash bound at approval; UNDO is its own class), D-006 rows 2 and 6.
SIMULATED: the key is the Python reference model; the host is a temporary folder that stands in for the host's filesystem."""

from __future__ import annotations

import ast
import base64
import hashlib
import os
import sys
from pathlib import Path

import pytest

from mas_device import approval as ap
from mas_device import gate as gt
from mas_device.keymodel import KeyModel
from mas_device.log import AuditLog, verify

SESSION = ap.session_id(b"\x02" * 32, b"pairing transcript for the gate")
TARGET = "host-test"


class Tick:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Tick()


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "host"
    (r / "etc").mkdir(parents=True)
    (r / "etc" / "app.conf").write_bytes(b"port=1\n")
    return r


@pytest.fixture
def key(tmp_path):
    return KeyModel.create(tmp_path / "key", clock=Tick(5.0))


@pytest.fixture
def log(tmp_path):
    return AuditLog.open(tmp_path / "device" / "audit.log", clock=lambda: 42)


@pytest.fixture
def pg(root, key, log, clock):
    approvals = ap.Gate({key.key_id: key.public_key}, session=SESSION, clock=clock)
    return gt.PolicyGate(root, approvals, log, target=TARGET)


def press(pg, key, action, cls=ap.Class.WRITE):
    pending = pg.begin(action) if cls == ap.Class.WRITE else pg.begin_undo(action)
    req = key.request(pending.key_request())
    key.button.press()
    return key.sign(req)


def snapshot(root: Path) -> dict[str, bytes | None]:
    out: dict[str, bytes | None] = {}
    for p in sorted(root.rglob("*")):
        out[p.relative_to(root).as_posix()] = p.read_bytes() if p.is_file() else None
    return out


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# ---- the happy path, once ------------------------------------------------------------------

def test_write_runs_with_a_valid_token_and_logs_undo_first(pg, key, root, log):
    action = pg.plan_write({"etc/app.conf": b"port=2\n", "opt/mas/node.toml": b"[node]\n"})
    token = press(pg, key, action)
    result = pg.execute(token, action)
    assert (root / "etc" / "app.conf").read_bytes() == b"port=2\n"
    assert (root / "opt" / "mas" / "node.toml").read_bytes() == b"[node]\n"
    recs = log.records()
    types = [r["type"] for r in recs]
    assert types == ["write.approved", "write.done"]
    approved = recs[0]["payload"]
    assert approved["undo"] == action["undo"], "the full undo action is in the log before the write"
    assert approved["undo_hash"] == ap.undo_hash_of(action).hex()
    assert approved["action_hash"] == ap.action_hash(action).hex() == result["action_hash"]
    assert verify(log._path).ok


def test_plan_states_pre_state_and_derives_its_own_undo(pg, root):
    action = pg.plan_write({"etc/app.conf": b"port=2\n", "new.txt": b"x"})
    files = {f["path"]: f for f in action["files"]}
    assert files["etc/app.conf"]["pre"] == sha(b"port=1\n")
    assert files["new.txt"]["pre"] is None
    undo = {f["path"]: f for f in action["undo"]["files"]}
    assert base64.b64decode(undo["etc/app.conf"]["content_b64"]) == b"port=1\n"
    assert undo["etc/app.conf"]["pre"] == sha(b"port=2\n")
    assert undo["new.txt"]["content_b64"] is None, "undo of a created file deletes it"


def test_reads_need_no_token_and_change_nothing(pg, root):
    before = snapshot(root)
    assert pg.read("etc/app.conf") == b"port=1\n"
    assert snapshot(root) == before
    with pytest.raises(gt.Refused) as e:
        pg.read("../outside.txt")
    assert e.value.code == "outside_root"


# ---- no write without the exact approval -------------------------------------------------

@pytest.mark.parametrize("token", [None, b"", b"\x00" * (ap.BODY_LEN + ap.SIG_LEN), "a token", 12345])
def test_no_valid_token_no_write(pg, root, log, token):
    action = pg.plan_write({"etc/app.conf": b"evil\n"})
    pg.begin(action)
    before = snapshot(root)
    with pytest.raises((ap.Rejected, gt.Refused)):
        pg.execute(token, action)
    assert snapshot(root) == before
    assert log.records() == []


def test_token_for_one_action_does_not_run_another(pg, key, root, log):
    a = pg.plan_write({"etc/app.conf": b"port=2\n"})
    b = pg.plan_write({"etc/app.conf": b"port=666\n"})
    token = press(pg, key, a)
    before = snapshot(root)
    with pytest.raises(ap.Rejected) as e:
        pg.execute(token, b)
    assert e.value.code == "action_mismatch"
    assert snapshot(root) == before and log.records() == []
    pg.execute(token, a)  # the mismatch did not burn the token for its own action
    assert (root / "etc" / "app.conf").read_bytes() == b"port=2\n"


def test_replayed_token_is_refused(pg, key, root):
    action = pg.plan_write({"etc/app.conf": b"port=2\n"})
    token = press(pg, key, action)
    pg.execute(token, action)
    (root / "etc" / "app.conf").write_bytes(b"port=1\n")  # host goes back to the planned pre-state
    with pytest.raises(ap.Rejected) as e:
        pg.execute(token, action)
    assert e.value.code == "unknown_nonce"
    assert (root / "etc" / "app.conf").read_bytes() == b"port=1\n"


def test_undo_class_token_cannot_run_a_write(pg, key, root):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    pg.execute(press(pg, key, w), w)
    undo = pg.undo_for(ap.action_hash(w))
    undo_token = press(pg, key, undo, cls=ap.Class.UNDO)
    w2 = pg.plan_write({"etc/app.conf": b"port=3\n"})
    pg.begin(w2)
    with pytest.raises(ap.Rejected):
        pg.execute(undo_token, w2)
    assert (root / "etc" / "app.conf").read_bytes() == b"port=2\n"


def test_pre_state_changed_after_approval_is_refused(pg, key, root, log):
    action = pg.plan_write({"etc/app.conf": b"port=2\n"})
    token = press(pg, key, action)
    (root / "etc" / "app.conf").write_bytes(b"port=9\n")  # someone changed the file after the owner pressed
    with pytest.raises(gt.Refused) as e:
        pg.execute(token, action)
    assert e.value.code == "pre_state_changed"
    assert (root / "etc" / "app.conf").read_bytes() == b"port=9\n"
    assert "write.done" not in [r["type"] for r in log.records()]


def test_created_dir_appearing_before_execute_is_pre_state_change(pg, key, root):
    action = pg.plan_write({"opt/mas/node.toml": b"[node]\n"})
    token = press(pg, key, action)
    (root / "opt").mkdir()
    with pytest.raises(gt.Refused) as e:
        pg.execute(token, action)
    assert e.value.code == "pre_state_changed"


def test_model_supplied_undo_is_refused(pg):
    action = pg.plan_write({"etc/app.conf": b"port=2\n"})
    action["undo"]["files"][0]["content_b64"] = base64.b64encode(b"something else\n").decode()
    with pytest.raises(gt.Refused) as e:
        pg.begin(action)
    assert e.value.code == "undo_mismatch"


def test_write_without_undo_is_never_offered(pg):
    action = pg.plan_write({"etc/app.conf": b"port=2\n"})
    del action["undo"]
    with pytest.raises((gt.Refused, ap.Rejected)):
        pg.begin(action)


@pytest.mark.parametrize("path", ["../escape.txt", "/etc/passwd", "C:/Windows/x", "a/../../b", "a\\b", "", ".", "etc/", "etc//x",
                                  "etc/./app.conf", "\u0065\u0301.txt", "nul", "x\x00y"])
def test_paths_outside_or_ambiguous_are_refused(pg, root, path):
    before = snapshot(root)
    with pytest.raises((gt.Refused, ap.Rejected)):
        pg.plan_write({path: b"x"})
    assert snapshot(root) == before


def test_hand_built_action_with_escaping_path_is_refused_at_begin(pg, root):
    action = pg.plan_write({"ok.txt": b"x"})
    action["files"][0]["path"] = "../escape.txt"
    action["undo"]["files"][0]["path"] = "../escape.txt"
    with pytest.raises(gt.Refused) as e:
        pg.begin(action)
    assert e.value.code in ("outside_root", "bad_action")


def _make_link(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            raise
        import _winapi  # a junction needs no privilege on Windows; it is a reparse point like a symlink
        _winapi.CreateJunction(str(target), str(link))


def test_link_inside_root_pointing_outside_is_refused(pg, root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    _make_link(root / "link", outside)
    with pytest.raises(gt.Refused) as e:
        pg.plan_write({"link/planted.txt": b"x"})
    assert e.value.code == "outside_root"
    assert list(outside.iterdir()) == []


def test_oversized_file_is_refused(pg):
    with pytest.raises(gt.Refused) as e:
        pg.plan_write({"big.bin": b"\x00" * (gt.MAX_FILE_BYTES + 1)})
    assert e.value.code == "too_large"


# ---- the log comes first -------------------------------------------------------------------

def test_no_write_if_the_log_cannot_record_the_undo(pg, key, root, log):
    action = pg.plan_write({"etc/app.conf": b"port=2\n"})
    token = press(pg, key, action)
    with open(log._path, "ab") as f:  # the log is changed behind the logger: it must refuse to extend it
        f.write(b"garbage\n")
    with pytest.raises(gt.Refused) as e:
        pg.execute(token, action)
    assert e.value.code == "log_failed"
    assert (root / "etc" / "app.conf").read_bytes() == b"port=1\n"


def test_failure_midway_rolls_back_and_registers_no_undo(pg, key, root, log, monkeypatch):
    action = pg.plan_write({"a.txt": b"A", "etc/app.conf": b"port=2\n"})
    token = press(pg, key, action)
    real = gt._replace_file
    calls = []

    def flaky(path, data):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(path, data)

    monkeypatch.setattr(gt, "_replace_file", flaky)
    with pytest.raises(gt.Refused) as e:
        pg.execute(token, action)
    assert e.value.code == "apply_failed"
    assert not (root / "a.txt").exists()
    assert (root / "etc" / "app.conf").read_bytes() == b"port=1\n"
    assert [r["type"] for r in log.records()] == ["write.approved", "write.failed"]
    with pytest.raises(gt.Refused) as e:
        pg.undo_for(ap.action_hash(action))
    assert e.value.code == "undo_not_issued"


# ---- undo ------------------------------------------------------------------------------------

def test_undo_restores_the_exact_pre_state(pg, key, root, log):
    before = snapshot(root)
    w = pg.plan_write({"etc/app.conf": b"port=2\n", "opt/mas/deep/node.toml": b"[node]\n"})
    pg.execute(press(pg, key, w), w)
    undo = pg.undo_for(ap.action_hash(w))
    assert ap.action_hash(undo) == ap.undo_hash_of(w), "the undo run is the one bound at approval"
    pg.execute_undo(press(pg, key, undo, cls=ap.Class.UNDO), undo)
    assert snapshot(root) == before, "files restored, created files and folders removed"
    assert [r["type"] for r in log.records()] == ["write.approved", "write.done", "undo.approved", "undo.done"]


def test_undo_needs_its_own_press(pg, key, root):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    write_token = press(pg, key, w)
    pg.execute(write_token, w)
    undo = pg.undo_for(ap.action_hash(w))
    pg.begin_undo(undo)
    for bad in (write_token, None, b"\x00" * (ap.BODY_LEN + ap.SIG_LEN)):
        with pytest.raises((ap.Rejected, gt.Refused)):
            pg.execute_undo(bad, undo)
    assert (root / "etc" / "app.conf").read_bytes() == b"port=2\n"


def test_forged_undo_is_refused_before_the_key_is_asked(pg, key, root):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    pg.execute(press(pg, key, w), w)
    forged = pg.undo_for(ap.action_hash(w))
    forged["files"][0]["content_b64"] = base64.b64encode(b"backdoor=1\n").decode()
    with pytest.raises(gt.Refused) as e:
        pg.begin_undo(forged)
    assert e.value.code == "undo_not_issued"
    never = {"kind": "files.restore", "target": TARGET, "files": [{"path": "etc/app.conf", "pre": sha(b"port=2\n"),
                                                                   "content_b64": base64.b64encode(b"x").decode()}], "rmdirs": []}
    with pytest.raises(gt.Refused) as e:
        pg.begin_undo(never)
    assert e.value.code == "undo_not_issued"


def test_forged_undo_with_a_signed_undo_token_is_still_refused(pg, key, root):
    """Even a genuine UNDO press does not make an undo valid: it must match an undo_hash from a consumed write approval."""
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    pg.execute(press(pg, key, w), w)
    forged = {"kind": "files.restore", "target": TARGET, "files": [{"path": "etc/app.conf", "pre": sha(b"port=2\n"),
                                                                    "content_b64": base64.b64encode(b"backdoor=1\n").decode()}], "rmdirs": []}
    pending = pg._approvals.begin(forged, ap.Class.UNDO)  # bypass begin_undo: the attacker reaches the token gate directly
    req = key.request(pending.key_request())
    key.button.press()
    token = key.sign(req)
    with pytest.raises(gt.Refused) as e:
        pg.execute_undo(token, forged)
    assert e.value.code == "undo_not_issued"
    assert (root / "etc" / "app.conf").read_bytes() == b"port=2\n"


def test_forged_log_entry_does_not_make_an_undo_valid(pg, key, root, log, tmp_path):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    forged = pg.plan_write({"etc/app.conf": b"port=2\n"})["undo"]
    forged["files"][0]["content_b64"] = base64.b64encode(b"backdoor=1\n").decode()
    log.append("write.approved", {"undo": forged, "undo_hash": ap.action_hash(forged).hex()})  # an entry the model could not sign
    pg.execute(press(pg, key, w), w)
    with pytest.raises(gt.Refused) as e:
        pg.begin_undo(forged)
    assert e.value.code == "undo_not_issued"


def test_undo_runs_once(pg, key, root):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    pg.execute(press(pg, key, w), w)
    undo = pg.undo_for(ap.action_hash(w))
    pg.execute_undo(press(pg, key, undo, cls=ap.Class.UNDO), undo)
    with pytest.raises(gt.Refused) as e:
        pg.begin_undo(undo)
    assert e.value.code == "undo_not_issued"


def test_undo_refuses_to_clobber_a_later_change(pg, key, root):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    pg.execute(press(pg, key, w), w)
    undo = pg.undo_for(ap.action_hash(w))
    token = press(pg, key, undo, cls=ap.Class.UNDO)
    (root / "etc" / "app.conf").write_bytes(b"port=owner-edit\n")
    with pytest.raises(gt.Refused) as e:
        pg.execute_undo(token, undo)
    assert e.value.code == "pre_state_changed"
    assert (root / "etc" / "app.conf").read_bytes() == b"port=owner-edit\n"


def test_undo_keeps_a_folder_someone_else_filled(pg, key, root):
    w = pg.plan_write({"opt/mas/node.toml": b"[node]\n"})
    pg.execute(press(pg, key, w), w)
    (root / "opt" / "mas" / "owner.txt").write_bytes(b"mine")
    undo = pg.undo_for(ap.action_hash(w))
    token = press(pg, key, undo, cls=ap.Class.UNDO)
    with pytest.raises(gt.Refused) as e:
        pg.execute_undo(token, undo)
    assert e.value.code == "pre_state_changed"
    assert (root / "opt" / "mas" / "owner.txt").read_bytes() == b"mine"


def test_restart_forgets_tokens_and_undo_rights(root, key, tmp_path, clock):
    log1 = AuditLog.open(tmp_path / "d1" / "audit.log", clock=lambda: 1)
    g1 = gt.PolicyGate(root, ap.Gate({key.key_id: key.public_key}, session=SESSION, clock=clock), log1, target=TARGET)
    w = g1.plan_write({"etc/app.conf": b"port=2\n"})
    token = press(g1, key, w)
    g2 = gt.PolicyGate(root, ap.Gate({key.key_id: key.public_key}, session=SESSION, clock=clock), log1, target=TARGET)
    with pytest.raises(ap.Rejected) as e:
        g2.execute(token, w)
    assert e.value.code == "unknown_nonce"
    assert (root / "etc" / "app.conf").read_bytes() == b"port=1\n"


# ---- structure: the gate is the only writer, and only one function writes -------------------

WRITE_CALLS = {"write_bytes", "write_text", "replace", "rename", "unlink", "remove", "rmdir", "mkdir", "makedirs", "removedirs", "rmtree",
               "symlink", "link", "truncate", "chmod", "open", "fdopen"}


def _calls(fn: ast.AST) -> set[str]:
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return out


def test_only_the_apply_helpers_touch_the_filesystem_for_writing():
    tree = ast.parse(Path(gt.__file__).read_text(encoding="utf-8"))
    writers = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _calls(node) & WRITE_CALLS:
            writers.add(node.name)
    assert writers == {"_replace_file", "_remove_file", "_make_dir", "_remove_dir"}, writers


def test_apply_helpers_are_called_only_after_consume_and_log():
    tree = ast.parse(Path(gt.__file__).read_text(encoding="utf-8"))
    callers = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            callers[node.name] = _calls(node)
    helpers = {"_replace_file", "_remove_file", "_make_dir", "_remove_dir"}
    direct = {name for name, c in callers.items() if c & helpers and name not in helpers}
    assert direct == {"_apply"}, direct
    users_of_apply = {name for name, c in callers.items() if "_apply" in c}
    assert users_of_apply == {"_run"}, users_of_apply
    run = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_run")
    calls = sorted((c.lineno, c.col_offset, c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", ""))
                   for c in ast.walk(run) if isinstance(c, ast.Call))
    order = [name for _, _, name in calls if name in ("consume", "append", "_apply")]
    assert order.index("consume") < order.index("append") < order.index("_apply"), order
