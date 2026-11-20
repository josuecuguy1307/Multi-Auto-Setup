"""Portable memory: an open format that round-trips, encrypted at rest (DECISIONS D-033; criterion P2-MEMORY).

Open format: the `.mem` bundle of MacPaw/portable-memory (MIT, spec v1.0): a plain folder

    manifest.json          {"capabilities": [...], "counts": {kind: n}, "format": "1.0.0", "kinds": [...]}
    items/<kind>.jsonl     one JSON record per line, each with a string "id"; unknown kinds and fields round-trip verbatim
    CHECKSUMS              "<sha256>  <path>" per file

Any `.mem` reader can take `export_dir`'s output. A `secretRef` record is a metadata skeleton only: secrets are never exported, not even
as ciphertext (the .mem rule), so a secretRef carrying a value is refused.

At rest the bundle exists only inside one sealed file (the .mem spec leaves encryption to the container):

    MAGIC "MASMEM\\0\\1" | log2(n) u8 | r u8 | p u8 | salt[16] | nonce[12] | AES-256-GCM(payload, aad = all of the above)
    key = scrypt(NFC(passphrase), salt, n, r, p, 32)

The KDF parameters are read from the file but must lie in a pinned range, so a file cannot talk the reader into a cheap KDF. Saving writes
a fresh salt and nonce to a temporary file in the same folder, fsyncs it and renames it over the old one: a failed save leaves the previous
memory intact and no plaintext ever reaches the disk.

SIMULATED: the medium is a folder; the passphrase is the owner's (the key MCU only signs, it holds no encryption key).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import secrets
import unicodedata
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORMAT = "1.0.0"
CAPABILITIES = ["episodes", "unknown-kinds-verbatim", "unknown-fields-verbatim"]
MAGIC = b"MASMEM\x00\x01"
SCRYPT_LOG2_N, SCRYPT_R, SCRYPT_P = 15, 8, 1
SCRYPT_N = 1 << SCRYPT_LOG2_N
_LOG2_N_RANGE, _R_RANGE, _P_RANGE = range(15, 19), range(8, 9), range(1, 3)
SALT_LEN, NONCE_LEN, KEY_LEN = 16, 12, 32
HEADER_LEN = len(MAGIC) + 3 + SALT_LEN + NONCE_LEN
MIN_PASSPHRASE = 8
MAX_FILE = 64 << 20
_KIND = r"[a-z][A-Za-z0-9-]{0,63}"
_PATH = re.compile(r"manifest\.json|CHECKSUMS|items/(" + _KIND + r")\.jsonl")
_SECRET_FIELDS = frozenset({"value", "secret", "password", "token", "ciphertext", "key", "data"})


class MemoryFormatError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code, detail)
        self.code, self.detail = code, detail


class MemoryLocked(Exception):
    """Wrong passphrase, or the sealed file was changed: the two are indistinguishable by design."""


# ---- the open format ---------------------------------------------------------------------------------------------------------------

def _line(rec) -> bytes:
    try:
        return json.dumps(rec, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"
    except (TypeError, ValueError) as exc:
        raise MemoryFormatError("bad_record", str(exc)[:120]) from None


def _check_items(items) -> None:
    if not isinstance(items, dict):
        raise MemoryFormatError("bad_items", "items is {kind: [records]}")
    for kind, recs in items.items():
        if not isinstance(kind, str) or not re.fullmatch(_KIND, kind):
            raise MemoryFormatError("bad_kind", repr(kind)[:80])
        if not isinstance(recs, list):
            raise MemoryFormatError("bad_items", f"{kind} is a list of records")
        for rec in recs:
            if not isinstance(rec, dict) or not isinstance(rec.get("id"), str) or not rec["id"]:
                raise MemoryFormatError("bad_record", f"every {kind} record is an object with a string id")
            if kind == "secretRef" and _SECRET_FIELDS & {k.casefold() for k in rec if isinstance(k, str)}:
                raise MemoryFormatError("secret_value", "a secretRef is metadata only; secrets are never stored")


def checksums(files: dict[str, bytes]) -> bytes:
    return "".join(f"{hashlib.sha256(files[p]).hexdigest()}  {p}\n" for p in sorted(files)).encode("ascii")


def build_bundle(items: dict[str, list[dict]]) -> dict[str, bytes]:
    _check_items(items)
    files = {f"items/{kind}.jsonl": b"".join(_line(r) for r in recs) for kind, recs in items.items()}
    manifest = {"capabilities": CAPABILITIES, "counts": {k: len(v) for k, v in items.items()}, "format": FORMAT, "kinds": sorted(items)}
    files["manifest.json"] = _line(manifest)
    files["CHECKSUMS"] = checksums(files)
    return files


def read_bundle(files: dict[str, bytes]) -> dict[str, list[dict]]:
    if not isinstance(files, dict) or "CHECKSUMS" not in files or "manifest.json" not in files:
        raise MemoryFormatError("incomplete", "a bundle has manifest.json and CHECKSUMS")
    for p in files:
        if not isinstance(p, str) or not _PATH.fullmatch(p):
            raise MemoryFormatError("bad_path", repr(p)[:80])
    sums = {}
    try:
        text = files["CHECKSUMS"].decode("ascii")
    except UnicodeDecodeError:
        raise MemoryFormatError("bad_checksums") from None
    for line in text.split("\n")[:-1] if text.endswith("\n") else [None]:
        m = re.fullmatch(r"([0-9a-f]{64})  (\S+)", line or "")
        if not m or m.group(2) in sums:
            raise MemoryFormatError("bad_checksums", "one '<sha256>  <path>' line per file")
        sums[m.group(2)] = m.group(1)
    if set(sums) != set(files) - {"CHECKSUMS"}:
        raise MemoryFormatError("bad_checksums", "CHECKSUMS must name every file and nothing else")
    for p, digest in sums.items():
        if hashlib.sha256(files[p]).hexdigest() != digest:
            raise MemoryFormatError("checksum_mismatch", p)
    try:
        man = json.loads(files["manifest.json"].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise MemoryFormatError("bad_manifest") from None
    fmt = man.get("format") if isinstance(man, dict) else None
    if not isinstance(fmt, str) or not re.fullmatch(r"1\.\d+\.\d+", fmt) or not isinstance(man.get("counts"), dict):
        raise MemoryFormatError("bad_manifest", "format 1.x.y with counts")
    items: dict[str, list[dict]] = {}
    for p, data in files.items():
        m = _PATH.fullmatch(p)
        if not m.group(1):
            continue
        recs = []
        for raw in data.split(b"\n")[:-1] if data.endswith(b"\n") else ([] if not data else [None]):
            try:
                recs.append(json.loads(raw.decode("utf-8")))
            except (AttributeError, UnicodeDecodeError, ValueError):
                raise MemoryFormatError("bad_record", p) from None
        items[m.group(1)] = recs
    if man["counts"] != {k: len(v) for k, v in items.items()}:
        raise MemoryFormatError("count_mismatch", "manifest counts differ from the items")
    _check_items(items)
    return items


def export_dir(files: dict[str, bytes], out: Path) -> Path:
    """Write the plain bundle to a new folder (an explicit export by the owner; never on the medium by default)."""
    read_bundle(files)
    out = Path(out)
    try:
        out.mkdir(parents=True)
    except FileExistsError:
        raise MemoryFormatError("exists", str(out)) from None
    for p, data in files.items():
        dest = out.joinpath(*p.split("/"))
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return out


def import_dir(folder: Path) -> dict[str, bytes]:
    folder = Path(folder)
    files = {}
    for dirpath, _dirs, names in os.walk(folder):
        for n in names:
            full = Path(dirpath, n)
            files[full.relative_to(folder).as_posix()] = full.read_bytes()
    read_bundle(files)
    return files


# ---- encrypted at rest ---------------------------------------------------------------------------------------------------------------

def _passphrase(passphrase) -> bytes:
    if not isinstance(passphrase, str):
        raise TypeError("the passphrase is text")
    pw = unicodedata.normalize("NFC", passphrase)
    if len(pw) < MIN_PASSPHRASE:
        raise ValueError(f"the passphrase has at least {MIN_PASSPHRASE} characters")
    return pw.encode("utf-8")


def _header(blob: bytes) -> tuple[bytes, int, int, int, bytes, bytes]:
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < HEADER_LEN + 16 or not blob.startswith(MAGIC):
        raise MemoryFormatError("not_sealed", "not a sealed MAS memory file")
    i = len(MAGIC)
    log2_n, r, p = blob[i], blob[i + 1], blob[i + 2]
    if log2_n not in _LOG2_N_RANGE or r not in _R_RANGE or p not in _P_RANGE:
        raise MemoryFormatError("kdf_params", "KDF parameters outside the pinned range")
    salt = bytes(blob[i + 3:i + 3 + SALT_LEN])
    nonce = bytes(blob[i + 3 + SALT_LEN:HEADER_LEN])
    return bytes(blob[:HEADER_LEN]), log2_n, r, p, salt, nonce


def header_len(blob: bytes) -> int:
    _header(blob)
    return HEADER_LEN


def _scrypt(pw: bytes, salt: bytes, log2_n: int, r: int, p: int) -> bytes:
    n = 1 << log2_n
    return hashlib.scrypt(pw, salt=salt, n=n, r=r, p=p, maxmem=256 * r * n * p + (1 << 20), dklen=KEY_LEN)


def derive_key(blob: bytes, passphrase: str) -> bytes:
    _, log2_n, r, p, salt, _ = _header(blob)
    return _scrypt(_passphrase(passphrase), salt, log2_n, r, p)


def seal(files: dict[str, bytes], passphrase: str) -> bytes:
    read_bundle(files)
    pw = _passphrase(passphrase)
    salt, nonce = secrets.token_bytes(SALT_LEN), secrets.token_bytes(NONCE_LEN)
    header = MAGIC + bytes([SCRYPT_LOG2_N, SCRYPT_R, SCRYPT_P]) + salt + nonce
    payload = json.dumps({"files": {p: base64.b64encode(d).decode("ascii") for p, d in sorted(files.items())}},
                         sort_keys=True, separators=(",", ":")).encode("ascii")
    key = _scrypt(pw, salt, SCRYPT_LOG2_N, SCRYPT_R, SCRYPT_P)
    return header + AESGCM(key).encrypt(nonce, payload, header)


def unseal_with_key(blob: bytes, key: bytes) -> dict[str, bytes]:
    header, *_, nonce = _header(blob)
    try:
        payload = AESGCM(key).decrypt(nonce, bytes(blob[HEADER_LEN:]), header)
    except InvalidTag:
        raise MemoryLocked("wrong passphrase, or the memory file was changed") from None
    try:
        doc = json.loads(payload.decode("ascii"))
        files = {p: base64.b64decode(d, validate=True) for p, d in doc["files"].items()}
    except (UnicodeDecodeError, ValueError, KeyError, TypeError, AttributeError, binascii.Error):
        raise MemoryFormatError("bad_payload") from None
    read_bundle(files)
    return files


def unseal(blob: bytes, passphrase: str) -> dict[str, bytes]:
    return unseal_with_key(blob, derive_key(blob, passphrase))


class MemoryStore:
    """The memory on the medium: one sealed file, replaced atomically on every save."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self, passphrase: str) -> dict[str, list[dict]]:
        with open(self.path, "rb") as f:
            blob = f.read(MAX_FILE + 1)
        if len(blob) > MAX_FILE:
            raise MemoryFormatError("too_large")
        return read_bundle(unseal(blob, passphrase))

    def save(self, items: dict[str, list[dict]], passphrase: str) -> None:
        blob = seal(build_bundle(items), passphrase)  # everything is sealed in memory before anything touches the disk
        tmp = self.path.with_name(f"{self.path.name}.tmp-{secrets.token_hex(8)}")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        try:
            try:
                view = memoryview(blob)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
