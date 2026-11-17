"""P2-LOG (guardrail 5): a complete, append-only, hash-chained log. Any edit, deletion, reorder or truncation is detected; a broken log is
never appended to (fail closed); the only write operation is append. SIMULATED (Python, host filesystem)."""

from __future__ import annotations

import json

import pytest

from mas_device import log as lg


class Clock:
    def __init__(self):
        self.t = 1_800_000_000_000

    def __call__(self):
        self.t += 1
        return self.t


@pytest.fixture
def path(tmp_path):
    return tmp_path / "audit.jsonl"


@pytest.fixture
def filled(path):
    log = lg.AuditLog.open(path, clock=Clock())
    for i in range(5):
        log.append("approval", {"n": i, "action_hash": f"{i:064x}"})
    return log


def lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def write(path, ls):
    path.write_text("\n".join(ls) + "\n", encoding="utf-8")


def test_fresh_log_verifies_and_head_moves(path):
    log = lg.AuditLog.open(path, clock=Clock())
    h0 = log.head()
    log.append("session_start", {"host": "vm-a"})
    assert log.head() != h0 and log.head().seq == 1
    assert lg.verify(path).ok


def test_records_chain_by_hash(filled, path):
    recs = [json.loads(l) for l in lines(path)]
    assert recs[0]["prev"] == lg.GENESIS
    assert all(recs[i]["prev"] == recs[i - 1]["hash"] for i in range(1, len(recs)))
    assert [r["seq"] for r in recs] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("edit", [
    lambda r: r["payload"].__setitem__("n", 99),
    lambda r: r.__setitem__("type", "nothing"),
    lambda r: r.__setitem__("at", r["at"] + 1),
    lambda r: r.__setitem__("seq", 7),
])
def test_editing_any_record_is_detected(filled, path, edit):
    ls = lines(path)
    r = json.loads(ls[2])
    edit(r)
    ls[2] = json.dumps(r)
    write(path, ls)
    v = lg.verify(path)
    assert not v.ok and v.bad_seq == 3


def test_recomputing_the_edited_records_hash_is_still_detected(filled, path):
    """An attacker who edits a record and fixes its own hash breaks the next record's link."""
    ls = lines(path)
    r = json.loads(ls[1])
    r["payload"]["n"] = 42
    r["hash"] = lg.record_hash(r)
    ls[1] = json.dumps(r)
    write(path, ls)
    v = lg.verify(path)
    assert not v.ok and v.bad_seq == 3


def test_deleting_a_middle_record_is_detected(filled, path):
    ls = lines(path)
    del ls[2]
    write(path, ls)
    assert not lg.verify(path).ok


def test_reordering_is_detected(filled, path):
    ls = lines(path)
    ls[1], ls[2] = ls[2], ls[1]
    write(path, ls)
    assert not lg.verify(path).ok


def test_truncating_the_tail_is_detected_against_a_known_head(filled, path):
    head = filled.head()
    write(path, lines(path)[:3])
    assert lg.verify(path).ok  # the file alone cannot tell
    v = lg.verify(path, expected=head)
    assert not v.ok and "truncated" in v.reason


def test_a_partial_last_line_is_detected(filled, path):
    with path.open("ab") as f:
        f.write(b'{"seq": 6, "prev": ')
    assert not lg.verify(path).ok


def test_garbage_and_duplicate_keys_are_detected(filled, path):
    ls = lines(path)
    ls[0] = ls[0].replace('"seq": 1', '"seq": 1, "seq": 1', 1)
    write(path, ls)
    assert not lg.verify(path).ok


def test_appending_to_a_broken_log_is_refused(filled, path):
    ls = lines(path)
    del ls[1]
    write(path, ls)
    with pytest.raises(lg.LogBroken):
        lg.AuditLog.open(path, clock=Clock())


def test_reopen_continues_the_chain(filled, path):
    head = filled.head()
    again = lg.AuditLog.open(path, clock=Clock())
    assert again.head() == head
    again.append("x", {})
    assert lg.verify(path).ok and again.head().seq == 6


def test_writes_from_outside_between_appends_are_caught(filled, path):
    """Someone appends a forged line behind the logger's back; the logger notices before its next append."""
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"seq": 6, "prev": filled.head().hash, "type": "approval", "payload": {}, "at": 0, "hash": "0" * 64}) + "\n")
    with pytest.raises(lg.LogBroken):
        filled.append("approval", {"n": 7})


def test_payload_must_be_canonical(path):
    log = lg.AuditLog.open(path, clock=Clock())
    with pytest.raises(lg.LogBroken):
        log.append("bad", {"x": 1.5})


def test_only_append_is_offered():
    public = {m for m in dir(lg.AuditLog) if not m.startswith("_")}
    assert public == {"open", "append", "head", "records"}


def test_owner_view_is_readable_and_complete(filled, path):
    text = lg.render(path)
    assert text.count("\n") >= 5 and "approval" in text and "chain: ok" in text


def test_owner_view_flags_a_broken_chain(filled, path):
    ls = lines(path)
    del ls[0]
    write(path, ls)
    assert "chain: BROKEN" in lg.render(path)
