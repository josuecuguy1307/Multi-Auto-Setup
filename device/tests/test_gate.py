"""Guardrails 2 and 6 (MISSION 4.1): the policy gate writes only with a fresh signed approval for the exact action it recomputes, records the
undo in the audit log before touching anything, and runs an undo only if that undo was bound to an approval this gate consumed.

Spec: DECISIONS.md D-003 (gate is the only writer), D-004 (undo_hash bound at approval; UNDO is its own class), D-006 rows 2 and 6.
SIMULATED: the key is the Python reference model; the host is a temporary folder that stands in for the host's filesystem."""

from __future__ import annotations

import ast
import base64
import inspect
import threading
import time
import hashlib
import os
import sys
from pathlib import Path

import pytest

from mas_device import approval as ap
from mas_device import gate as gt
from mas_device.keymodel import KeyModel
from mas_device.log import AuditLog, LogBroken, verify

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
    key._clock.t += KeyModel.MIN_DWELL_S  # the person reads the screen first (D-017)
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

    def flaky(path, data, *a, **k):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(path, data, *a, **k)

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
    key._clock.t += KeyModel.MIN_DWELL_S  # read the screen first (D-017)
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
    undo = pg.undo_for(ap.action_hash(w))
    (root / "opt" / "mas" / "early.txt").write_bytes(b"mine")
    with pytest.raises(gt.Refused) as e:  # refused before the key is even asked
        pg.begin_undo(undo)
    assert e.value.code == "pre_state_changed"
    (root / "opt" / "mas" / "early.txt").unlink()
    token = press(pg, key, undo, cls=ap.Class.UNDO)
    (root / "opt" / "mas" / "owner.txt").write_bytes(b"mine")  # and after the press
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
    assert users_of_apply == {"_run_locked"}, users_of_apply
    run = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_run_locked")
    calls = sorted((c.lineno, c.col_offset, c.func.attr if isinstance(c.func, ast.Attribute) else getattr(c.func, "id", ""))
                   for c in ast.walk(run) if isinstance(c, ast.Call))
    order = [name for _, _, name in calls if name in ("consume", "append", "_apply")]
    assert order.index("consume") < order.index("append") < order.index("_apply"), order



# ---- evaluator run 1 (device/evals/evaluator/P2-GATE.md) ---------------------------------------------

@pytest.mark.parametrize("names", [("etc/APP.conf", "etc/app.conf"), ("N.txt", "n.txt"), ("É.txt", "é.txt"),
                                   ("Opt/x.txt", "opt/y.txt")])
def test_paths_that_collide_without_case_are_refused(pg, root, names):
    before = snapshot(root)
    with pytest.raises(gt.Refused) as e:
        pg.plan_write({n: b"x" for n in names})
    assert e.value.code == "bad_action"
    assert snapshot(root) == before


def test_a_path_must_be_the_files_own_name_not_an_alias(pg, root):
    (root / "longdirectoryname").mkdir()
    (root / "longdirectoryname" / "f.txt").write_bytes(b"old")
    aliases = ["LongDirectoryName/f.txt", "longdirectoryname/F.TXT"]
    if sys.platform == "win32":
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(str(root / "longdirectoryname"), buf, 1024):
            short = Path(buf.value).name
            if short.lower() != "longdirectoryname":
                aliases.append(f"{short}/f.txt")  # an 8.3 alias, where the volume makes them
    for alias in aliases:
        if not (root / alias).exists():
            continue  # a case-sensitive filesystem: this spelling is simply another, absent file
        with pytest.raises(gt.Refused) as e:
            pg.plan_write({alias: b"new"})
        assert e.value.code == "bad_action", alias
    assert (root / "longdirectoryname" / "f.txt").read_bytes() == b"old"


@pytest.mark.parametrize("name", ["a<b", "a>b", 'a"b', "a|b", "a?b", "a*b"])
def test_names_windows_cannot_hold_are_refused_cleanly(pg, name):
    with pytest.raises(gt.Refused) as e:
        pg.plan_write({name: b"x"})
    assert e.value.code == "bad_action"


def test_read_refuses_a_file_with_other_hard_links(pg, root, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"secret")
    os.link(outside, root / "hl.txt")
    with pytest.raises(gt.Refused) as e:
        pg.read("hl.txt")
    assert e.value.code == "bad_action"


class Sly(dict):
    """Looks like the approved entry to everything except the code that writes."""

    def __getitem__(self, k):
        if k == "content_b64" and any(f.function == "_apply" for f in inspect.stack()[1:4]):
            return base64.b64encode(b"backdoor=1\n").decode()
        return super().__getitem__(k)


def test_the_gate_writes_only_its_own_copy_of_the_approved_action(pg, key, root):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    token = press(pg, key, w)
    w["files"] = [Sly(f) for f in w["files"]]
    pg.execute(token, w)
    assert (root / "etc" / "app.conf").read_bytes() == b"port=2\n"


def test_the_gate_runs_its_own_copy_of_the_undo(pg, key, root):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    pg.execute(press(pg, key, w), w)
    u = pg.undo_for(ap.action_hash(w))
    token = press(pg, key, u, cls=ap.Class.UNDO)
    u["files"] = [Sly(f) for f in u["files"]]
    pg.execute_undo(token, u)
    assert (root / "etc" / "app.conf").read_bytes() == b"port=1\n"


def test_two_approved_writes_at_once_never_both_run_against_the_same_pre_state(pg, key, root, log, monkeypatch):
    w1 = pg.plan_write({"etc/app.conf": b"port=2\n"})
    w2 = pg.plan_write({"etc/app.conf": b"port=3\n"})
    t1, t2 = press(pg, key, w1), press(pg, key, w2)
    real = log.append

    def slow(*a, **k):
        time.sleep(0.2)
        return real(*a, **k)

    monkeypatch.setattr(log, "append", slow)
    results = []

    def go(tok, w):
        try:
            pg.execute(tok, w)
            results.append("ok")
        except gt.Refused as exc:
            results.append(exc.code)

    ts = [threading.Thread(target=go, args=a) for a in ((t1, w1), (t2, w2))]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == ["ok", "pre_state_changed"], results


def test_a_host_change_between_validation_and_consume_is_caught(pg, key, root, monkeypatch):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    token = press(pg, key, w)
    real = pg._approvals.consume

    def consume_after_a_change(*a, **k):
        (root / "etc" / "app.conf").write_bytes(b"port=9\n")  # the host changes after the gate validated, before the token is spent
        return real(*a, **k)

    monkeypatch.setattr(pg._approvals, "consume", consume_after_a_change)
    with pytest.raises(gt.Refused) as e:
        pg.execute(token, w)
    assert e.value.code == "pre_state_changed"
    assert (root / "etc" / "app.conf").read_bytes() == b"port=9\n"


def test_a_forged_undo_inside_a_directly_approved_write_is_refused_at_execute(pg, key, root):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    w["undo"]["files"][0]["content_b64"] = base64.b64encode(b"backdoor=1\n").decode()
    pending = pg._approvals.begin(w, ap.Class.WRITE)  # the attacker reaches the token gate directly, skipping PolicyGate.begin
    req = key.request(pending.key_request())
    key._clock.t += KeyModel.MIN_DWELL_S
    key.button.press()
    token = key.sign(req)
    with pytest.raises(gt.Refused) as e:
        pg.execute(token, w)
    assert e.value.code == "undo_mismatch"
    assert (root / "etc" / "app.conf").read_bytes() == b"port=1\n"


def test_a_link_that_stays_inside_the_root_is_refused_too(pg, root):
    (root / "inner").mkdir()
    _make_link(root / "alias", root / "inner")
    with pytest.raises(gt.Refused) as e:
        pg.plan_write({"alias/planted.txt": b"x"})
    assert e.value.code == "outside_root"
    assert list((root / "inner").iterdir()) == []



# ---- evaluator run 2 (device/evals/evaluator/P2-GATE.run2.md) ------------------------------------------

def test_a_nested_write_from_inside_a_write_is_refused(pg, key, root, log, monkeypatch):
    w1 = pg.plan_write({"etc/app.conf": b"port=2\n"})
    w2 = pg.plan_write({"etc/app.conf": b"port=3\n"})
    t1, t2 = press(pg, key, w1), press(pg, key, w2)
    real = log.append
    nested = []

    def hook(kind, payload):
        out = real(kind, payload)
        if kind == "write.approved" and not nested:
            try:
                pg.execute(t2, w2)
                nested.append("ran")
            except gt.Refused as exc:
                nested.append(exc.code)
        return out

    monkeypatch.setattr(log, "append", hook)
    worker = threading.Thread(target=pg.execute, args=(t1, w1), daemon=True)
    worker.start()
    worker.join(20)
    assert not worker.is_alive(), "a nested write deadlocked the gate"
    assert nested == ["bad_action"], nested
    assert (root / "etc" / "app.conf").read_bytes() == b"port=2\n"
    assert [r["type"] for r in log.records()] == ["write.approved", "write.done"]


def test_the_realpath_check_alone_stops_a_link_out_of_the_root(pg, root, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    _make_link(root / "link", outside)
    monkeypatch.setattr(gt, "_is_link", lambda p: False)  # suppose the link check missed it
    with pytest.raises(gt.Refused) as e:
        pg.plan_write({"link/planted.txt": b"x"})
    assert e.value.code == "outside_root"
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("name", ["COM0", "LPT0", "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³", "CONIN$",
                                  "CONOUT$", "CLOCK$", "conin$.txt", "com0.log"])
def test_every_windows_device_name_is_refused(pg, name):
    with pytest.raises(gt.Refused) as e:
        pg.plan_write({name: b"x"})
    assert e.value.code == "bad_action"


def test_a_lone_surrogate_path_is_refused_by_the_gate(pg):
    with pytest.raises(gt.Refused):
        pg.plan_write({"a\udc00": b"x"})


@pytest.mark.parametrize("bad", [[1], None, "abc", 12])
def test_undo_for_anything_but_a_hash_is_refused(pg, bad):
    with pytest.raises(gt.Refused) as e:
        pg.undo_for(bad)
    assert e.value.code == "undo_not_issued"



# ---- evaluator run 3 (device/evals/evaluator/P2-GATE.run3.md) ------------------------------------------

def _make_junction(link: Path, target: Path) -> None:
    """A real NTFS junction on Windows (a reparse point that is not a symlink); a symlink elsewhere."""
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def test_undo_for_takes_a_bytearray_hash(pg, key):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    pg.execute(press(pg, key, w), w)
    assert pg.undo_for(bytearray(ap.action_hash(w))) == pg.undo_for(ap.action_hash(w))


def test_a_foreign_target_is_refused_at_begin_and_at_execute(pg, key, root):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    w["target"] = "bank-server"
    w["undo"]["target"] = "bank-server"
    with pytest.raises(gt.Refused) as e:
        pg.begin(w)
    assert e.value.code == "bad_action"
    pending = pg._approvals.begin(w, ap.Class.WRITE)  # the attacker reaches the token gate directly
    req = key.request(pending.key_request())
    key._clock.t += KeyModel.MIN_DWELL_S
    key.button.press()
    with pytest.raises(gt.Refused) as e:
        pg.execute(key.sign(req), w)
    assert e.value.code == "bad_action"
    assert (root / "etc" / "app.conf").read_bytes() == b"port=1\n"


def test_a_junction_swapped_in_after_consume_is_refused_at_apply(pg, key, root, log, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "sub").mkdir()
    w = pg.plan_write({"sub/f.txt": b"x"})
    token = press(pg, key, w)
    real = log.append

    def hook(kind, payload):
        out = real(kind, payload)
        if kind == "write.approved":  # after the token is spent and the undo logged, before the write
            os.rename(root / "sub", root / "sub_old")
            _make_junction(root / "sub", outside)
        return out

    monkeypatch.setattr(log, "append", hook)
    with pytest.raises(gt.Refused) as e:
        pg.execute(token, w)
    assert e.value.code in ("apply_failed", "outside_root")  # refused inside the apply step, with the precise reason
    assert list(outside.iterdir()) == []


def test_a_real_junction_inside_the_root_is_refused(pg, root):
    (root / "inner").mkdir()
    _make_junction(root / "alias", root / "inner")
    with pytest.raises(gt.Refused) as e:
        pg.plan_write({"alias/p.txt": b"x"})
    assert e.value.code == "outside_root"
    assert list((root / "inner").iterdir()) == []


@pytest.mark.parametrize("name", ["con", "prn", "aux", "nul", "PRN.txt", "AUX.log", "con.txt", "Nul.dat",
                                  "CON .txt", "nul .txt", "COM1 .txt", "CONIN$ .txt", "a.", "a ", "dir./x", "dir /x"])
def test_device_names_and_trailing_dots_or_spaces_are_refused(pg, root, name):
    before = snapshot(root)
    with pytest.raises(gt.Refused) as e:
        pg.plan_write({name: b"x"})
    assert e.value.code == "bad_action", name
    assert snapshot(root) == before


def test_a_spent_token_is_always_in_the_log(pg, key, root, log, monkeypatch):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    token = press(pg, key, w)
    real = pg._approvals.consume

    def consume_then_change(*a, **k):
        out = real(*a, **k)
        (root / "etc" / "app.conf").write_bytes(b"port=9\n")  # the host changes right after the token is spent
        return out

    monkeypatch.setattr(pg._approvals, "consume", consume_then_change)
    with pytest.raises(gt.Refused) as e:
        pg.execute(token, w)
    assert e.value.code == "pre_state_changed"
    recs = log.records()
    assert [r["type"] for r in recs] == ["write.refused"]
    assert recs[0]["payload"]["counter"] == 1 and recs[0]["payload"]["reason"] == "pre_state_changed"


def test_a_file_that_is_also_a_folder_of_the_same_action_is_refused(pg):
    with pytest.raises(gt.Refused) as e:
        pg.plan_write({"q": b"1", "q/r": b"2"})
    assert e.value.code == "bad_action"



# ---- evaluator run 4 (device/evals/evaluator/P2-GATE.run4.md) ------------------------------------------

def test_an_os_error_after_the_token_is_spent_is_logged_and_refused(pg, key, root, log, monkeypatch):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    token = press(pg, key, w)
    real = pg._approvals.consume

    def consume_then_lock(*a, **k):
        out = real(*a, **k)

        def locked(p):
            raise PermissionError("another process holds the file")

        monkeypatch.setattr(pg, "_current", locked)  # the file becomes unreadable right after the token is spent
        return out

    monkeypatch.setattr(pg._approvals, "consume", consume_then_lock)
    with pytest.raises(gt.Refused) as e:
        pg.execute(token, w)
    assert e.value.code == "pre_state_changed"
    recs = log.records()
    assert [r["type"] for r in recs] == ["write.refused"] and recs[0]["payload"]["counter"] == 1


def test_a_folder_created_after_approval_but_before_the_write_is_refused_and_logged(pg, key, root, log, monkeypatch):
    w = pg.plan_write({"opt/mas/node.toml": b"[node]\n"})
    token = press(pg, key, w)
    real = pg._approvals.consume

    def consume_then_mkdir(*a, **k):
        out = real(*a, **k)
        (root / "opt").mkdir()
        return out

    monkeypatch.setattr(pg._approvals, "consume", consume_then_mkdir)
    with pytest.raises(gt.Refused) as e:
        pg.execute(token, w)
    assert e.value.code == "pre_state_changed"
    assert [r["type"] for r in log.records()] == ["write.refused"]


@pytest.mark.parametrize("files", [{"etc/app.conf/x": b"1"}, {"n" * 245: b"1"}, {"d/" + "a" * 300: b"1"}])
def test_paths_that_could_never_be_written_are_refused_before_the_press(pg, files):
    with pytest.raises(gt.Refused) as e:
        pg.plan_write(files)
    assert e.value.code in ("bad_action", "outside_root")


@pytest.mark.parametrize("path", ["etc/app.conf:evil", "etc/app.conf::$DATA", "x:y"])
def test_alternate_data_streams_are_refused(pg, path):
    with pytest.raises(gt.Refused):
        pg.plan_write({path: b"x"})


def test_undo_for_after_the_undo_has_run_is_refused(pg, key):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    pg.execute(press(pg, key, w), w)
    u = pg.undo_for(ap.action_hash(w))
    pg.execute_undo(press(pg, key, u, cls=ap.Class.UNDO), u)
    with pytest.raises(gt.Refused) as e:
        pg.undo_for(ap.action_hash(w))
    assert e.value.code == "undo_not_issued"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file attributes")
def test_files_with_special_attributes_are_refused(pg, root):
    import ctypes
    target = root / "etc" / "app.conf"
    assert ctypes.windll.kernel32.SetFileAttributesW(str(target), 0x2)  # FILE_ATTRIBUTE_HIDDEN
    try:
        with pytest.raises(gt.Refused) as e:
            pg.plan_write({"etc/app.conf": b"port=2\n"})
        assert e.value.code == "bad_action"
    finally:
        ctypes.windll.kernel32.SetFileAttributesW(str(target), 0x80)



# ---- evaluator run 5 (device/evals/evaluator/P2-GATE.run5.md) ------------------------------------------

def _after(log, monkeypatch, kind, fn):
    real = log.append

    def hook(k, payload):
        out = real(k, payload)
        if k == kind:
            fn()
        return out

    monkeypatch.setattr(log, "append", hook)


def test_an_edit_after_the_approval_is_logged_is_never_overwritten(pg, key, root, log, monkeypatch):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    token = press(pg, key, w)
    _after(log, monkeypatch, "write.approved", lambda: (root / "etc" / "app.conf").write_bytes(b"port=1\nowner_edit=1\n"))
    with pytest.raises(gt.Refused) as e:
        pg.execute(token, w)
    assert e.value.code == "pre_state_changed"
    assert (root / "etc" / "app.conf").read_bytes() == b"port=1\nowner_edit=1\n"
    assert [r["type"] for r in log.records()] == ["write.approved", "write.failed"]


def test_an_edit_after_the_undo_is_logged_is_never_overwritten(pg, key, root, log, monkeypatch):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    pg.execute(press(pg, key, w), w)
    u = pg.undo_for(ap.action_hash(w))
    token = press(pg, key, u, cls=ap.Class.UNDO)
    _after(log, monkeypatch, "undo.approved", lambda: (root / "etc" / "app.conf").write_bytes(b"port=2\nowner_edit=1\n"))
    with pytest.raises(gt.Refused) as e:
        pg.execute_undo(token, u)
    assert e.value.code == "pre_state_changed"
    assert (root / "etc" / "app.conf").read_bytes() == b"port=2\nowner_edit=1\n"


def test_names_are_measured_as_the_filesystem_measures_them(pg):
    with pytest.raises(gt.Refused) as e:
        pg.plan_write({"\U0001F600" * 120: b"x"})  # 120 code points, 240 UTF-16 units, 480 UTF-8 bytes
    assert e.value.code == "bad_action"


def test_a_path_too_long_for_the_host_is_refused_before_the_press(pg, root):
    long = {"d" * 150 + "/" + "e" * 150: b"x", "n" * 200: b"x"}
    for rel, data in long.items():
        total = len(str(root / rel)) + gt.TEMP_SUFFIX_LEN
        if total > gt.PATH_LIMIT:
            with pytest.raises(gt.Refused) as e:
                pg.plan_write({rel: data})
            assert e.value.code == "bad_action", rel
        else:
            pg.plan_write({rel: data})
    assert gt.PATH_LIMIT == (259 if sys.platform == "win32" and not gt._long_paths_enabled() else 4095 if sys.platform != "win32" else 32000)


@pytest.mark.parametrize("files", [{"ABCDEFGHIJ.TXT": b"1", "ABCDEF~1.TXT": b"2"}, {"PROGRA~1/x": b"1"}])
def test_names_that_look_like_short_aliases_are_refused(pg, files):
    with pytest.raises(gt.Refused) as e:
        pg.plan_write(files)
    assert e.value.code == "bad_action"


def test_an_undo_whose_folder_is_gone_is_refused_before_the_press(pg, key, root):
    w = pg.plan_write({"etc/app.conf": None})
    pg.execute(press(pg, key, w), w)
    (root / "etc").rmdir()
    u = pg.undo_for(ap.action_hash(w))
    with pytest.raises(gt.Refused) as e:
        pg.begin_undo(u)
    assert e.value.code == "pre_state_changed"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file attributes")
@pytest.mark.parametrize("attr", [0x1, 0x4, 0x100])  # read-only, system, temporary
def test_every_non_plain_attribute_is_refused(pg, root, attr):
    import ctypes
    target = root / "etc" / "app.conf"
    assert ctypes.windll.kernel32.SetFileAttributesW(str(target), attr)
    try:
        with pytest.raises(gt.Refused) as e:
            pg.plan_write({"etc/app.conf": b"port=2\n"})
        assert e.value.code == "bad_action"
    finally:
        ctypes.windll.kernel32.SetFileAttributesW(str(target), 0x80)


def test_a_stale_undo_is_refused_before_its_token_is_touched(pg, key, root):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    pg.execute(press(pg, key, w), w)
    u = pg.undo_for(ap.action_hash(w))
    token = press(pg, key, u, cls=ap.Class.UNDO)
    (root / "etc" / "app.conf").write_bytes(b"port=7\n")
    with pytest.raises(gt.Refused) as e:
        pg.execute_undo(token, u)
    assert e.value.code == "pre_state_changed"
    (root / "etc" / "app.conf").write_bytes(b"port=2\n")
    pg.execute_undo(token, u)  # the same token still works: it was never spent
    assert (root / "etc" / "app.conf").read_bytes() == b"port=1\n"


def test_deleting_a_file_that_is_not_there_is_refused(pg):
    with pytest.raises(gt.Refused) as e:
        pg.plan_write({"absent.txt": None})
    assert e.value.code == "bad_action"


def test_an_existing_host_file_too_large_to_undo_is_refused(pg, root):
    (root / "big.bin").write_bytes(b"\0" * (gt.MAX_FILE_BYTES + 1))
    with pytest.raises(gt.Refused) as e:
        pg.plan_write({"big.bin": b"small"})
    assert e.value.code == "too_large"


def test_a_failing_refused_record_still_comes_back_as_refused(pg, key, root, log, monkeypatch):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    token = press(pg, key, w)
    real_consume = pg._approvals.consume
    real_append = log.append

    def consume_then_change(*a, **k):
        out = real_consume(*a, **k)
        (root / "etc" / "app.conf").write_bytes(b"port=9\n")
        return out

    def append(kind, payload):
        if kind.endswith(".refused"):
            raise LogBroken("disk full")
        return real_append(kind, payload)

    monkeypatch.setattr(pg._approvals, "consume", consume_then_change)
    monkeypatch.setattr(log, "append", append)
    with pytest.raises(gt.Refused):
        pg.execute(token, w)


def test_folders_are_never_created_through_a_link_swapped_in_after_approval(pg, key, root, log, tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "a").mkdir()
    w = pg.plan_write({"a/new/deep/f.txt": b"x"})
    token = press(pg, key, w)
    made = []
    real_make = gt._make_dir
    monkeypatch.setattr(gt, "_make_dir", lambda p: (made.append(Path(os.path.realpath(p.parent))), real_make(p)))

    def swap():
        os.rename(root / "a", root / "a_old")
        _make_junction(root / "a", outside)

    _after(log, monkeypatch, "write.approved", swap)
    with pytest.raises(gt.Refused):
        pg.execute(token, w)
    real_root = Path(os.path.realpath(root))
    assert all(m == real_root or real_root in m.parents for m in made), made
    assert list(outside.iterdir()) == []


def test_authenticated_tokens_the_gate_refuses_are_logged_with_their_counter(pg, key, root, log, monkeypatch):
    w1 = pg.plan_write({"etc/app.conf": b"port=2\n"})
    t1 = press(pg, key, w1)  # counter 1
    w2 = pg.plan_write({"new.txt": b"x"})
    t2 = press(pg, key, w2)  # counter 2
    pg.execute(t2, w2)        # spent out of order
    with pytest.raises(ap.Rejected) as e:
        pg.execute(t1, w1)
    assert e.value.code == "counter_regressed"
    w3 = pg.plan_write({"other.txt": b"y"})
    t3 = press(pg, key, w3)  # counter 3
    clock = pg._approvals.clock
    monkeypatch.setattr(pg._approvals, "clock", lambda: clock() + ap.MAX_TTL_S + 1)
    with pytest.raises(ap.Rejected) as e:
        pg.execute(t3, w3)
    assert e.value.code == "expired"
    rejected = [(r["payload"]["counter"], r["payload"]["reason"]) for r in log.records() if r["type"] == "write.rejected"]
    assert rejected == [(1, "counter_regressed"), (3, "expired")]



# ---- evaluator run 6 (device/evals/evaluator/P2-GATE.run6.md) ------------------------------------------

def _fail_on_second_replace(monkeypatch, before_fail):
    real = gt._replace_file
    calls = []

    def flaky(path, data, *a, **k):
        calls.append(path)
        if len(calls) == 2:
            before_fail()
            raise OSError("disk full")
        return real(path, data, *a, **k)

    monkeypatch.setattr(gt, "_replace_file", flaky)


def test_the_rollback_never_overwrites_a_file_the_owner_saved_meanwhile(pg, key, root, log, monkeypatch):
    (root / "a.conf").write_bytes(b"a=1\n")
    (root / "b.conf").write_bytes(b"b=1\n")
    w = pg.plan_write({"a.conf": b"a=2\n", "b.conf": b"b=2\n"})
    token = press(pg, key, w)
    _fail_on_second_replace(monkeypatch, lambda: (root / "a.conf").write_bytes(b"a=owner\n"))
    with pytest.raises(gt.Refused):
        pg.execute(token, w)
    assert (root / "a.conf").read_bytes() == b"a=owner\n", "the owner's save survives the rollback"
    failed = [r for r in log.records() if r["type"] == "write.failed"]
    assert failed and failed[0]["payload"]["left_changed"] == ["a.conf"]


def test_the_rollback_never_deletes_a_created_file_the_owner_then_saved(pg, key, root, log, monkeypatch):
    (root / "b.conf").write_bytes(b"b=1\n")
    w = pg.plan_write({"a_new.txt": b"gate\n", "b.conf": b"b=2\n"})
    token = press(pg, key, w)
    _fail_on_second_replace(monkeypatch, lambda: (root / "a_new.txt").write_bytes(b"mine\n"))
    with pytest.raises(gt.Refused):
        pg.execute(token, w)
    assert (root / "a_new.txt").read_bytes() == b"mine\n"


def test_a_failed_write_leaves_no_folders_it_created(pg, key, root, monkeypatch):
    (root / "z.conf").write_bytes(b"z=1\n")
    w = pg.plan_write({"new/deep/f.txt": b"x", "z.conf": b"z=2\n"})
    token = press(pg, key, w)
    _fail_on_second_replace(monkeypatch, lambda: None)
    with pytest.raises(gt.Refused):
        pg.execute(token, w)
    assert not (root / "new").exists()


def test_the_final_compare_happens_after_the_temporary_file_is_on_disk(pg, key, root, monkeypatch):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    token = press(pg, key, w)
    real_fsync = os.fsync

    def fsync_then_owner_edit(fd):
        real_fsync(fd)
        if any(f.function == "_replace_file" for f in inspect.stack()[1:3]):  # only the gate's temporary file, not the log's fsync
            (root / "etc" / "app.conf").write_bytes(b"port=1\nowner_edit=1\n")

    monkeypatch.setattr(gt.os, "fsync", fsync_then_owner_edit)
    with pytest.raises(gt.Refused) as e:
        pg.execute(token, w)
    assert e.value.code == "pre_state_changed"
    assert (root / "etc" / "app.conf").read_bytes() == b"port=1\nowner_edit=1\n"
    assert not [q for q in (root / "etc").iterdir() if "mas-tmp" in q.name], "no temporary file left behind"


@pytest.mark.skipif(not (sys.platform == "win32" and gt.PATH_LIMIT == 259), reason="the Windows MAX_PATH boundary")
def test_the_path_limit_counts_the_temporary_suffix_and_utf16_units(pg, root):
    base = len(str(root).encode("utf-16-le")) // 2 + 1
    n = 259 - base - 5  # fits without the 18-character temporary suffix, not with it
    assert 0 < n <= gt.MAX_NAME
    with pytest.raises(gt.Refused) as e:
        pg.plan_write({"n" * n: b"x"})
    assert e.value.code == "bad_action"
    k = (259 - base - 3) // 2 // 2  # astral characters count twice in UTF-16
    astral = "\U0001F600" * k + "/" + "\U0001F600" * k + "/x"
    assert base + len(astral) + 18 <= 259 < base + len(astral.encode("utf-16-le")) // 2 + 18 + 10
    with pytest.raises(gt.Refused) as e:
        pg.plan_write({"\U0001F600" * 50 + "/" + "\U0001F600" * 50 + "/x": b"x"})
    assert e.value.code == "bad_action"


def test_two_approved_writes_released_together_never_share_a_pre_state(pg, key, root, monkeypatch):
    w1 = pg.plan_write({"etc/app.conf": b"port=2\n"})
    w2 = pg.plan_write({"etc/app.conf": b"port=3\n"})
    t1, t2 = press(pg, key, w1), press(pg, key, w2)
    real = gt._replace_file
    barrier = threading.Barrier(2)

    def meet(path, data, *a, **k):
        try:
            barrier.wait(timeout=1.0)  # without the run lock both writers arrive here together
        except threading.BrokenBarrierError:
            pass
        return real(path, data, *a, **k)

    monkeypatch.setattr(gt, "_replace_file", meet)
    results = []

    def go(tok, w):
        try:
            pg.execute(tok, w)
            results.append("ok")
        except gt.Refused as exc:
            results.append(exc.code)

    ts = [threading.Thread(target=go, args=a) for a in ((t1, w1), (t2, w2))]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert sorted(results) == ["ok", "pre_state_changed"], results


def test_a_wrong_session_token_is_logged_with_its_counter(pg, key, root, log):
    w = pg.plan_write({"etc/app.conf": b"port=2\n"})
    pending = pg._approvals.begin(w, ap.Class.WRITE)
    req = key.request(pending.key_request()._replace(session_id=b"\x42" * 32))
    key._clock.t += KeyModel.MIN_DWELL_S
    key.button.press()
    with pytest.raises(ap.Rejected) as e:
        pg.execute(key.sign(req), w)
    assert e.value.code == "wrong_session"
    assert [(r["type"], r["payload"]["reason"], r["payload"]["counter"]) for r in log.records()] == [("write.rejected", "wrong_session", 1)]


# ---- evaluator run 7 (device/evals/evaluator/P2-GATE.run7.md) ------------------------------------------

def test_the_rollback_restore_compares_after_its_temporary_file_is_on_disk(pg, key, root, log, monkeypatch):
    (root / "a.conf").write_bytes(b"a=1\n")
    (root / "b.conf").write_bytes(b"b=1\n")
    w = pg.plan_write({"a.conf": b"a=2\n", "b.conf": b"b=2\n"})
    token = press(pg, key, w)
    real, real_fsync = gt._replace_file, os.fsync
    calls, armed = [], []

    def flaky(path, data, *a, **k):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("disk full")
        if len(calls) == 3:
            armed.append(True)  # the rollback restoring a.conf
        return real(path, data, *a, **k)

    def fsync(fd):
        real_fsync(fd)
        if armed and any(f.function == "_replace_file" for f in inspect.stack()[1:3]):
            armed.clear()
            (root / "a.conf").write_bytes(b"a=owner\n")  # the owner saves while the rollback's temporary file is on disk

    monkeypatch.setattr(gt, "_replace_file", flaky)
    monkeypatch.setattr(gt.os, "fsync", fsync)
    with pytest.raises(gt.Refused):
        pg.execute(token, w)
    assert (root / "a.conf").read_bytes() == b"a=owner\n"
    failed = [r for r in log.records() if r["type"] == "write.failed"]
    assert failed and failed[0]["payload"]["left_changed"] == ["a.conf"]
    assert not [q for q in root.iterdir() if "mas-tmp" in q.name]


def test_an_approved_delete_never_removes_an_edit_made_during_the_log_append(pg, key, root, log, monkeypatch):
    w = pg.plan_write({"etc/app.conf": None})
    token = press(pg, key, w)
    real_append = log.append

    def append(type, payload):
        head = real_append(type, payload)
        if type == "write.approved":
            (root / "etc" / "app.conf").write_bytes(b"port=1\nowner_edit=1\n")
        return head

    monkeypatch.setattr(log, "append", append)
    with pytest.raises(gt.Refused) as e:
        pg.execute(token, w)
    assert e.value.code == "pre_state_changed"
    assert (root / "etc" / "app.conf").read_bytes() == b"port=1\nowner_edit=1\n"


def test_a_failed_write_puts_back_every_existing_file_it_replaced(pg, key, root, log, monkeypatch):
    (root / "a.conf").write_bytes(b"a=1\n")
    (root / "b.conf").write_bytes(b"b=1\n")
    w = pg.plan_write({"a.conf": b"a=2\n", "b.conf": b"b=2\n"})
    token = press(pg, key, w)
    _fail_on_second_replace(monkeypatch, lambda: None)
    with pytest.raises(gt.Refused):
        pg.execute(token, w)
    assert (root / "a.conf").read_bytes() == b"a=1\n" and (root / "b.conf").read_bytes() == b"b=1\n"
    failed = [r for r in log.records() if r["type"] == "write.failed"]
    assert failed and failed[0]["payload"]["left_changed"] == []


def test_a_failed_undo_remakes_the_folders_it_removed(pg, key, root, log, monkeypatch):
    w = pg.plan_write({"n/a/x.txt": b"x\n"})
    pg.execute(press(pg, key, w), w)
    undo = pg.undo_for(ap.action_hash(w))
    utoken = press(pg, key, undo, ap.Class.UNDO)
    real_rmdir = gt._remove_dir

    def rmdir(p):
        if p.name == "n":
            raise OSError("in use: another process has its working folder here")
        real_rmdir(p)

    monkeypatch.setattr(gt, "_remove_dir", rmdir)
    with pytest.raises(gt.Refused):
        pg.execute_undo(utoken, undo)
    assert (root / "n" / "a" / "x.txt").read_bytes() == b"x\n"
    failed = [r for r in log.records() if r["type"] == "undo.failed"]
    assert failed and failed[0]["payload"]["left_changed"] == []
