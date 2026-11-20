"""P2-MEMORY: portable memory in an open format round-trips and is encrypted at rest (DECISIONS D-033).

Open format: the `.mem` bundle of MacPaw/portable-memory (MIT): a folder with `manifest.json` (`format` semver, `capabilities`, counts),
`items/<kind>.jsonl` (one record per line) and `CHECKSUMS` (one SHA-256 per file). Unknown kinds and fields round-trip verbatim.
At rest the bundle exists only inside a sealed container: AES-256-GCM, key from the owner's passphrase by scrypt, header authenticated.

SIMULATED: the medium is a folder; the passphrase is typed by a test.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest

from mas_device import memory as mm

PASS = "correct horse battery staple"
PLANTED = "PLANTED-MEMORY-7c1e-the-owner-likes-green-tea"

ITEMS = {
    "episode": [
        {"id": "ep-1", "text": PLANTED, "at": "2026-11-19T10:00:00Z"},
        {"id": "ep-2", "text": "Josu\u00e9 prefiere respuestas en espa\u00f1ol \u2014 \u00bfvale? \U0001F600", "tags": ["lang", "pref"],
         "ext": {"future_field": {"nested": [1, 2, 3]}}},
    ],
    "fact": [{"id": "f-1", "subject": "device", "predicate": "label", "object": "SIMULATED"}],
    "preference": [{"id": "p-1", "key": "tone", "value": "plain"}],
    "zz-unknown-kind": [{"id": "u-1", "anything": {"goes": True, "n": 7}}],  # unknown kinds round-trip verbatim
}


# ---- the open format --------------------------------------------------------------------------------------------------------------------

def test_bundle_has_the_mem_layout_and_checksums():
    files = mm.build_bundle(ITEMS)
    assert set(files) == {"manifest.json", "CHECKSUMS"} | {f"items/{k}.jsonl" for k in ITEMS}
    man = json.loads(files["manifest.json"])
    assert man["format"] == mm.FORMAT and tuple(int(x) for x in man["format"].split(".")) >= (1, 0, 0)
    assert man["counts"] == {k: len(v) for k, v in ITEMS.items()}
    assert isinstance(man["capabilities"], list)
    lines = files["CHECKSUMS"].decode("ascii").splitlines()
    sums = dict(reversed(line.split("  ", 1)) for line in lines)
    assert set(sums) == set(files) - {"CHECKSUMS"}
    for path, digest in sums.items():
        assert digest == hashlib.sha256(files[path]).hexdigest()
    for kind, recs in ITEMS.items():
        got = [json.loads(line) for line in files[f"items/{kind}.jsonl"].decode("utf-8").splitlines()]
        assert got == recs


def test_bundle_round_trips_and_is_deterministic():
    files = mm.build_bundle(ITEMS)
    assert mm.read_bundle(files) == ITEMS
    assert mm.build_bundle(ITEMS) == files


def test_bundle_round_trips_through_a_plain_folder(tmp_path):
    out = tmp_path / "export.mem"
    mm.export_dir(mm.build_bundle(ITEMS), out)
    assert (out / "manifest.json").is_file() and (out / "items" / "episode.jsonl").is_file() and (out / "CHECKSUMS").is_file()
    assert mm.read_bundle(mm.import_dir(out)) == ITEMS
    with pytest.raises(mm.MemoryFormatError):
        mm.export_dir(mm.build_bundle(ITEMS), out)  # never overwrites an existing export


def test_bundle_checks_are_enforced():
    good = mm.build_bundle(ITEMS)
    edited = dict(good)
    edited["items/fact.jsonl"] = good["items/fact.jsonl"].replace(b"SIMULATED", b"REAL!!!!!")
    with pytest.raises(mm.MemoryFormatError):
        mm.read_bundle(edited)  # checksum
    missing = {k: v for k, v in good.items() if k != "items/fact.jsonl"}
    with pytest.raises(mm.MemoryFormatError):
        mm.read_bundle(missing)
    extra = dict(good)
    extra["items/sneaky.jsonl"] = b'{"id":"x"}\n'
    with pytest.raises(mm.MemoryFormatError):
        mm.read_bundle(extra)  # not in CHECKSUMS
    man = json.loads(good["manifest.json"])
    man["counts"]["fact"] = 5
    recount = dict(good)
    recount["manifest.json"] = json.dumps(man).encode()
    recount["CHECKSUMS"] = mm.checksums({k: v for k, v in recount.items() if k != "CHECKSUMS"})
    with pytest.raises(mm.MemoryFormatError):
        mm.read_bundle(recount)  # counts
    for bad_path in ("../x.jsonl", "items/../../x.jsonl", "/abs.jsonl", "items\\x.jsonl", "C:/x.jsonl"):
        b = dict(good)
        b[bad_path] = b"{}\n"
        b["CHECKSUMS"] = mm.checksums({k: v for k, v in b.items() if k != "CHECKSUMS"})
        with pytest.raises(mm.MemoryFormatError):
            mm.read_bundle(b)


def test_bad_items_are_refused():
    for items in ({"episode": [{"text": "no id"}]}, {"Bad Kind": [{"id": "1"}]}, {"episode": [{"id": "1", "x": float("nan")}]},
                  {"episode": "not a list"}, {"secretRef": [{"id": "s", "name": "github", "value": "ghp_x"}]},
                  {"secretRef": [{"id": "s", "name": "aws", "secret": "abc"}]}):
        with pytest.raises(mm.MemoryFormatError):
            mm.build_bundle(items)
    # a secretRef is a metadata skeleton only (the .mem rule: secrets are never exported, not even as ciphertext)
    assert mm.read_bundle(mm.build_bundle({"secretRef": [{"id": "s", "name": "github"}]})) == {"secretRef": [{"id": "s", "name": "github"}]}


# ---- encrypted at rest ------------------------------------------------------------------------------------------------------------------

def _forms(s: str) -> list[bytes]:
    b = s.encode("utf-8")
    return [b, s.encode("utf-16-le"), base64.b64encode(b), b.hex().encode(), s[: len(s) // 2].encode()]


def test_store_round_trips_and_nothing_is_readable_at_rest(tmp_path):
    store = mm.MemoryStore(tmp_path / "memory.masmem")
    store.save(ITEMS, PASS)
    assert store.load(PASS) == ITEMS
    on_disk = sorted(p for p in tmp_path.rglob("*") if p.is_file())
    assert on_disk == [tmp_path / "memory.masmem"]  # no plaintext copy, no temp file left behind
    blob = on_disk[0].read_bytes()
    assert blob.startswith(mm.MAGIC)
    for needle in _forms(PLANTED) + _forms("green tea") + [b"episode", b"manifest", b"CHECKSUMS", b"items/"]:
        assert needle not in blob, needle
    # a fresh salt and nonce on every save: the same memory never gives the same bytes
    store.save(ITEMS, PASS)
    assert (tmp_path / "memory.masmem").read_bytes() != blob


def test_wrong_passphrase_and_bad_passphrases_are_refused(tmp_path):
    store = mm.MemoryStore(tmp_path / "m.masmem")
    store.save(ITEMS, PASS)
    for wrong in ("correct horse battery stapl", PASS + " ", PASS.upper()):
        with pytest.raises(mm.MemoryLocked):
            store.load(wrong)
    for bad in ("", "short", None, b"bytes are not a passphrase"):
        with pytest.raises((mm.MemoryLocked, mm.MemoryFormatError, TypeError, ValueError)):
            store.save(ITEMS, bad)


def test_every_byte_of_the_container_is_authenticated():
    blob = mm.seal(mm.build_bundle(ITEMS), PASS)
    hdr = mm.header_len(blob)
    assert 0 < hdr < len(blob)
    # every header byte (magic, version, KDF parameters, salt, nonce): each flip goes through the full KDF path
    for i in range(hdr):
        bad = bytearray(blob)
        bad[i] ^= 0x01
        with pytest.raises((mm.MemoryLocked, mm.MemoryFormatError)):
            mm.unseal(bytes(bad), PASS)
    # every ciphertext and tag byte, with the key derived once for this salt and parameters
    key = mm.derive_key(blob, PASS)
    assert mm.read_bundle(mm.unseal_with_key(blob, key)) == ITEMS
    for i in range(hdr, len(blob)):
        bad = bytearray(blob)
        bad[i] ^= 0x80
        with pytest.raises(mm.MemoryLocked):
            mm.unseal_with_key(bytes(bad), key)
    for cut in (0, 1, hdr - 1, hdr, hdr + 15, len(blob) - 1):
        with pytest.raises((mm.MemoryLocked, mm.MemoryFormatError)):
            mm.unseal(blob[:cut], PASS)
    with pytest.raises((mm.MemoryLocked, mm.MemoryFormatError)):
        mm.unseal(blob + b"\x00", PASS)


def test_kdf_parameters_cannot_be_downgraded():
    assert mm.SCRYPT_N >= 2 ** 15 and mm.SCRYPT_R >= 8
    blob = mm.seal(mm.build_bundle(ITEMS), PASS)
    # what an attacker holding the file would do: rewrite the stored parameters (log2 n, r, p) right after the magic
    weak = blob[:len(mm.MAGIC)] + bytes([10, 1, 1]) + blob[len(mm.MAGIC) + 3:]
    with pytest.raises(mm.MemoryFormatError):
        mm.unseal(weak, PASS)  # refused before any key is derived, and the tag would not match anyway


def test_a_failed_save_leaves_the_previous_memory_intact(tmp_path, monkeypatch):
    path = tmp_path / "m.masmem"
    store = mm.MemoryStore(path)
    store.save(ITEMS, PASS)
    before = path.read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(mm.os, "replace", boom)
    with pytest.raises(OSError):
        store.save({"episode": [{"id": "new"}]}, PASS)
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert store.load(PASS) == ITEMS
    assert sorted(p.name for p in tmp_path.iterdir()) == ["m.masmem"]


def test_memory_module_never_writes_plaintext_to_disk(tmp_path, monkeypatch):
    """Every byte the store writes goes through one function; none of it may hold the planted memory."""
    writes: list[bytes] = []
    real_write = os.write

    def spy(fd, data):
        writes.append(bytes(data))
        return real_write(fd, data)

    monkeypatch.setattr(mm.os, "write", spy)
    mm.MemoryStore(tmp_path / "m.masmem").save(ITEMS, PASS)
    assert writes, "the store writes through os.write"
    joined = b"".join(writes)
    for needle in _forms(PLANTED):
        assert needle not in joined
