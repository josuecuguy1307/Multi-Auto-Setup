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
    path.write_bytes(("\n".join(ls) + "\n").encode("utf-8"))  # bytes: write_text turns every newline into CRLF on Windows


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


# ---- evaluator run 1 (device/evals/evaluator/P2-LOG.md): each BROKE item and each surviving mutant gets a test ----

import hashlib
import threading


def rechain(recs, *, start=1, domain=lg.DOMAIN):
    """Rebuild a fully self-consistent chain from edited records: what an attacker with write access to the file can do."""
    prev, out = lg.GENESIS, []
    for i, r in enumerate(recs):
        r = {k: r[k] for k in lg.FIELDS}
        r["seq"], r["prev"] = start + i, prev
        r["hash"] = hashlib.sha256(domain + lg.canonical({k: r[k] for k in lg.FIELDS})).hexdigest()
        out.append(json.dumps(r, sort_keys=True, ensure_ascii=True))
        prev = r["hash"]
    return out


def test_same_length_rewrite_is_detected_against_a_known_head(filled, path):
    head = filled.head()
    recs = [json.loads(l) for l in lines(path)]
    recs[2]["payload"]["n"] = 666
    write(path, rechain(recs))
    assert lg.verify(path).ok  # self-consistent on its own
    v = lg.verify(path, expected=head)
    assert not v.ok and v.bad_seq == 5


def test_rewrite_extended_past_the_known_head_is_detected(filled, path):
    head = filled.head()
    recs = [json.loads(l) for l in lines(path)]
    recs[2]["payload"]["n"] = 666
    recs.append(dict(recs[-1], payload={"n": "forged"}))
    write(path, rechain(recs))
    v = lg.verify(path, expected=head)
    assert not v.ok and v.bad_seq == head.seq


def test_truncate_then_forge_a_longer_tail_is_detected(filled, path):
    head = filled.head()
    recs = [json.loads(l) for l in lines(path)][:2]
    recs += [dict(recs[-1], payload={"n": f"forged{i}"}) for i in range(4)]
    write(path, rechain(recs))
    assert not lg.verify(path, expected=head).ok


@pytest.mark.parametrize("sep", [" ", " ", "\u0085", "\r", "\x0b", "\x0c", "\x1c"])
def test_line_separator_characters_in_strings_do_not_break_the_log(path, sep):
    log = lg.AuditLog.open(path, clock=Clock())
    log.append("host", {"name": f"vm{sep}evil"})
    log.append(f"t{sep}", {"k": 1})
    raw = path.read_bytes()
    assert raw.isascii() and raw.count(b"\n") == 2
    assert lg.verify(path).ok
    again = lg.AuditLog.open(path, clock=Clock())
    assert again.records()[0]["payload"]["name"] == f"vm{sep}evil"


def test_same_size_edit_then_append_is_refused(filled, path):
    data = path.read_bytes()
    edited = data.replace(b'"n": 1}', b'"n": 7}', 1)
    assert len(edited) == len(data) and edited != data
    path.write_bytes(edited)
    with pytest.raises(lg.LogBroken):
        filled.append("approval", {"n": 99})
    assert path.read_bytes() == edited


def test_reopen_with_a_known_head_refuses_a_truncated_log(filled, path):
    head = filled.head()
    write(path, lines(path)[:2])
    with pytest.raises(lg.LogBroken):
        lg.AuditLog.open(path, clock=Clock(), expected=head)


def test_two_writers_never_break_the_chain(path):
    a = lg.AuditLog.open(path, clock=Clock())
    a.append("x", {"i": 0})
    for round_ in range(60):
        b = lg.AuditLog.open(path, clock=Clock())
        a = lg.AuditLog.open(path, clock=Clock())
        barrier, errors = threading.Barrier(2), []

        def go(log, who):
            barrier.wait()
            try:
                log.append("x", {"who": who, "round": round_})
            except lg.LogBroken as exc:
                errors.append(exc)

        ts = [threading.Thread(target=go, args=(a, "a")), threading.Thread(target=go, args=(b, "b"))]
        [t.start() for t in ts]
        [t.join() for t in ts]
        assert lg.verify(path).ok, f"chain broken in round {round_}"
        assert len(errors) <= 1
    assert lg.verify(path).head.seq >= 61


def test_bool_and_float_seq_or_at_are_refused(filled, path):
    for field, bad in (("seq", True), ("seq", 1.0), ("at", 1.5), ("at", False)):
        recs = [json.loads(l) for l in lines(path)]
        r = recs[0]
        r[field] = bad
        r["hash"] = hashlib.sha256(lg.DOMAIN + json.dumps({k: r[k] for k in lg.FIELDS}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        ls = lines(path)
        ls[0] = json.dumps(r, sort_keys=True)
        write(path, ls)
        assert not lg.verify(path).ok, (field, bad)
        write(path, [json.dumps(x, sort_keys=True) for x in [json.loads(l) for l in lines(path)]])


def test_extra_field_outside_the_hash_is_refused(filled, path):
    ls = lines(path)
    r = json.loads(ls[1])
    r["note"] = "owner approved"
    ls[1] = json.dumps(r, sort_keys=True)
    write(path, ls)
    assert not lg.verify(path).ok


def test_chain_that_does_not_start_at_one_is_refused(filled, path):
    recs = [json.loads(l) for l in lines(path)]
    write(path, rechain(recs, start=2))
    assert not lg.verify(path).ok


def test_chain_hashed_without_the_domain_tag_is_refused(filled, path):
    recs = [json.loads(l) for l in lines(path)]
    write(path, rechain(recs, domain=b""))
    assert not lg.verify(path).ok


def test_full_record_without_trailing_newline_is_partial(filled, path):
    path.write_bytes(path.read_bytes()[:-1])
    v = lg.verify(path)
    assert not v.ok and v.bad_seq == 5


def test_partial_tail_reports_the_right_seq_and_can_be_repaired_on_request(filled, path):
    with path.open("ab") as f:
        f.write(b'{"at": 1, "hash": ')
    v = lg.verify(path)
    assert not v.ok and v.bad_seq == 6
    with pytest.raises(lg.LogBroken):
        lg.AuditLog.open(path, clock=Clock())
    fixed = lg.AuditLog.open(path, clock=Clock(), repair_partial=True)
    recs = fixed.records()
    assert recs[-1]["type"] == "log.repaired" and recs[-1]["payload"]["dropped_bytes"] == len(b'{"at": 1, "hash": ')
    assert lg.verify(path).ok and fixed.head().seq == 6


def test_repair_never_drops_a_complete_record(filled, path):
    ls = lines(path)
    del ls[1]
    write(path, ls)
    with pytest.raises(lg.LogBroken):
        lg.AuditLog.open(path, clock=Clock(), repair_partial=True)


def test_crlf_or_reformatted_lines_are_refused(filled, path):
    data = path.read_bytes()
    path.write_bytes(data.replace(b"\n", b"\r\n"))
    assert not lg.verify(path).ok
    path.write_bytes(data.replace(b"\n", b" \n", 1))
    assert not lg.verify(path).ok


def test_records_refuses_to_return_a_tampered_log(filled, path):
    path.write_bytes(path.read_bytes().replace(b'"n": 1}', b'"n": 7}', 1))
    with pytest.raises(lg.LogBroken):
        filled.records()
