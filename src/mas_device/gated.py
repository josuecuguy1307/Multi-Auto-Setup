"""The policy gate as its own process (guardrail 4, MISSION 4.1; DECISIONS D-025).

The host's owner starts it by hand, with every argument stated (nothing has a default and nothing starts it by itself):

    python -I -m mas_device.gated --root <host folder> --state <device folder> --pin <key public key, hex> --session <hex> --target <name>

It holds the audit log, the pinned key and the session. The model's side holds only its stdin/stdout and speaks JSON lines:

    request:  {"id": <int>, "method": <one of ALLOWED>, "params": {...}}
    answer:   {"id": <int|null>, "ok": true, "result": {...}}   or   {"id": ..., "ok": false, "error": {"code": ..., "detail": ...}}

There is no method that changes the policy, the pinned key or the log. Every refusal is logged (`rpc.refused`) so the owner sees what the
model tried; reads are logged with their hash; `begin` is logged with what the key will show. Paths that name credential stores are refused
for `read` and `plan_write` (whose undo would carry the old bytes). The daemon exits when its stdin closes.

Bytes travel as lowercase hex (tokens, hashes, key requests) or strict base64 (file contents). Requests are at most MAX_LINE bytes.

SIMULATED: the gate and the model share an OS user on this host (D-005); this process boundary is the separation tested in P2-GUARDRAILS.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

from mas_device import approval as ap
from mas_device.gate import PolicyGate, Refused
from mas_device.log import AuditLog, LogBroken

ALLOWED = frozenset({"read", "plan_write", "begin", "execute", "undo_for", "begin_undo", "execute_undo", "log_head"})
MAX_LINE = 8 << 20   # a 1 MiB file is ~1.4 MiB of base64, twice (content and undo) in an action
MAX_ID = 1 << 53
_HEX = re.compile(r"(?:[0-9a-f]{2})*")

CREDENTIAL_DIRS = frozenset({".ssh", ".gnupg", ".aws", ".azure", ".kube", ".password-store"})
CREDENTIAL_NAMES = frozenset({
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ecdsa_sk", "id_ed25519_sk", ".env", ".netrc", "_netrc", ".pgpass",
    ".git-credentials", ".htpasswd", "credentials", "credentials.json", "credentials.db", "secrets.json", "login data",
    "login data for account", "cookies", "local state", "web data", "logins.json", "key3.db", "key4.db", "wallet.dat", "shadow",
    "gshadow", "sam", "ntds.dit"})
CREDENTIAL_PREFIXES = ("credentials", ".env")  # credentials*, .env* (D-025): credentials.csv, .envrc, .env-prod... (P2-GUARDRAILS run 1)
CREDENTIAL_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx", ".kdb", ".keychain", ".keychain-db", ".ppk", ".jks", ".keystore")


class Bad(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code, detail)
        self.code, self.detail = code, detail


def is_credential_path(rel: str) -> bool:
    """True if any component of the path names a credential store. Names only: a secret inside an ordinary file is not seen here."""
    for part in re.split(r"[\\/]+", rel):
        name = unicodedata.normalize("NFC", part).casefold().split(":")[0].rstrip(" .")
        if name in CREDENTIAL_DIRS or name in CREDENTIAL_NAMES or name.startswith(CREDENTIAL_PREFIXES) or name.endswith(CREDENTIAL_SUFFIXES):
            return True
    return False


def _no_dup(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise ValueError(f"duplicate key {k!r}")
        d[k] = v
    return d


def _no_constant(name):
    raise ValueError(f"{name} is not JSON")


def _ascii(v) -> str:
    return ascii(v)[1:-1][:80] if isinstance(v, str) else ""


def _hex(v, n: int | None = None) -> bytes:
    if not isinstance(v, str) or not _HEX.fullmatch(v) or (n is not None and len(v) != 2 * n):
        raise Bad("malformed_request", "lowercase hex expected" + (f" ({n} bytes)" if n else ""))
    return bytes.fromhex(v)


def _content(v) -> bytes | None:
    if v is None:
        return None
    if not isinstance(v, str):
        raise Bad("malformed_request", "file contents are base64 strings or null")
    try:
        data = base64.b64decode(v, validate=True)
    except (binascii.Error, ValueError):
        raise Bad("malformed_request", "file contents are strict base64") from None
    if base64.b64encode(data).decode("ascii") != v:
        raise Bad("malformed_request", "file contents are canonical base64")
    return data


def _params(params: dict, *keys: str) -> list:
    if set(params) != set(keys):
        raise Bad("malformed_request", f"params are exactly {sorted(keys)}")
    return [params[k] for k in keys]


def _path(v) -> str:
    if not isinstance(v, str):
        raise Bad("malformed_request", "a path is a string")
    if is_credential_path(v):
        raise Bad("credential_path", "credential stores are never read (guardrail 8)")
    return v


def _key_request(p: ap.Pending) -> dict:
    return {"session_id": p.session.hex(), "nonce": p.nonce.hex(), "action_hash": p.action_hash.hex(), "undo_hash": p.undo_hash.hex(),
            "summary": p.summary.hex(), "cls": int(p.cls), "press_window_s": p.press_window_s}


class Daemon:
    def __init__(self, gate: PolicyGate, log: AuditLog) -> None:
        self.gate, self.log = gate, log

    # -- the methods the model may call (each one is in ALLOWED; nothing else is reachable)

    def rpc_read(self, params: dict) -> dict:
        (path,) = _params(params, "path")
        try:
            data = self.gate.read(_path(path))
        except FileNotFoundError:
            raise Bad("not_found", "no such file") from None
        self.log.append("read", {"path": path, "sha256": hashlib.sha256(data).hexdigest()})
        return {"content_b64": base64.b64encode(data).decode("ascii")}

    def rpc_plan_write(self, params: dict) -> dict:
        (files,) = _params(params, "files")
        if not isinstance(files, dict):
            raise Bad("malformed_request", "files is an object of path to base64 or null")
        return {"action": self.gate.plan_write({_path(k): _content(v) for k, v in files.items()})}

    def rpc_begin(self, params: dict) -> dict:
        (action,) = _params(params, "action")
        return {"key_request": self._requested(self.gate.begin(action))}

    def rpc_execute(self, params: dict) -> dict:
        token, action = _params(params, "token", "action")
        return self.gate.execute(_hex(token), action)

    def rpc_undo_for(self, params: dict) -> dict:
        (action_hash,) = _params(params, "action_hash")
        return {"undo": self.gate.undo_for(_hex(action_hash, 32))}

    def rpc_begin_undo(self, params: dict) -> dict:
        (undo,) = _params(params, "undo")
        return {"key_request": self._requested(self.gate.begin_undo(undo))}

    def rpc_execute_undo(self, params: dict) -> dict:
        token, undo = _params(params, "token", "undo")
        return self.gate.execute_undo(_hex(token), undo)

    def rpc_log_head(self, params: dict) -> dict:
        _params(params)
        head = self.log.head()
        return {"seq": head.seq, "hash": head.hash}

    # -- plumbing

    def _requested(self, p: ap.Pending) -> dict:
        self.log.append("approval.requested", {"class": p.cls.name, "action_hash": p.action_hash.hex(),
                                               "summary": p.summary.decode("ascii").rstrip()})
        return _key_request(p)

    def _log_refusal(self, rid, method: str, code: str) -> None:
        try:
            self.log.append("rpc.refused", {"id": rid, "method": method, "code": code})
        except (LogBroken, OSError) as exc:
            print(f"ERROR gated: refusal not logged: {type(exc).__name__}", file=sys.stderr)

    def _refuse(self, rid, method, code: str, detail: str = "") -> dict:
        self._log_refusal(rid, _ascii(method), code)
        return {"id": rid, "ok": False, "error": {"code": code, "detail": detail[:200]}}

    def handle(self, line: bytes) -> dict:
        rid, method = None, ""
        try:
            try:
                req = json.loads(line.decode("utf-8"), object_pairs_hook=_no_dup, parse_constant=_no_constant)
            except (UnicodeDecodeError, ValueError, RecursionError):
                raise Bad("malformed_request", "one JSON object per line, UTF-8, no duplicate keys") from None
            if not isinstance(req, dict):
                raise Bad("malformed_request", "a request is a JSON object")
            v = req.get("id")
            rid = v if type(v) is int and 0 <= v < MAX_ID else None
            method = req.get("method")
            if rid is None or set(req) != {"id", "method", "params"} or not isinstance(method, str):
                raise Bad("malformed_request", "a request is exactly {id, method, params}, id an integer below 2^53")
            if method not in ALLOWED:
                raise Bad("unknown_method", "not a method of the gate")
            if not isinstance(req["params"], dict):
                raise Bad("malformed_request", "params is an object")
            return {"id": rid, "ok": True, "result": getattr(self, "rpc_" + method)(req["params"])}
        except (Bad, Refused, ap.Rejected) as exc:
            return self._refuse(rid, method, exc.code, getattr(exc, "detail", "") or "")
        except LogBroken as exc:
            return self._refuse(rid, method, "log_failed", str(exc))
        except OSError as exc:
            return self._refuse(rid, method, "io_error", type(exc).__name__)
        except Exception as exc:  # the daemon outlives any request; the owner sees the refusal in the log
            return self._refuse(rid, method, "internal", type(exc).__name__)

    def serve(self, inp, out) -> int:
        while True:
            line = inp.readline(MAX_LINE + 1)
            if not line:
                return 0
            if len(line) > MAX_LINE and not line.endswith(b"\n"):
                while True:  # drop the rest of the oversized line
                    chunk = inp.readline(1 << 20)
                    if not chunk or chunk.endswith(b"\n"):
                        break
                resp = self._refuse(None, "", "request_too_large", f"a request is at most {MAX_LINE} bytes")
            else:
                resp = self.handle(line)
            out.write(json.dumps(resp, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n")
            out.flush()


def _inside(a: Path, b: Path) -> bool:
    a, b = os.path.normcase(os.path.realpath(a)), os.path.normcase(os.path.realpath(b))
    return os.path.commonpath([a, b]) == b


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mas-gated", description="The MAS device's policy gate (started by the host's owner).")
    for name, what in (("--root", "the host folder the gate may read and, with approval, write"),
                       ("--state", "the device's folder for the audit log (outside --root)"),
                       ("--pin", "the paired key's Ed25519 public key, 64 hex digits"),
                       ("--session", "the session id from pairing, 64 hex digits"),
                       ("--target", "the host's name as the owner sees it")):
        parser.add_argument(name, required=True, help=what)
    a = parser.parse_args(argv)
    try:
        pin, session = bytes.fromhex(a.pin), bytes.fromhex(a.session)
    except ValueError:
        parser.error("--pin and --session are hex")
    if len(pin) != 32 or len(session) != 32:
        parser.error("--pin and --session are 32 bytes each")
    root, state = Path(a.root), Path(a.state)
    if not root.is_dir() or not state.is_dir():
        parser.error("--root and --state are existing folders")
    if _inside(state, root) or _inside(root, state):
        parser.error("--state must be outside --root and --root outside --state: the model's writes must never reach the log")
    log = AuditLog.open(state / "audit.log")
    approvals = ap.Gate({ap.key_id_of(pin): pin}, session=session)
    gate = PolicyGate(root, approvals, log, target=a.target)
    log.append("gate.started", {"target": a.target, "key_id": ap.key_id_of(pin).hex(), "session": session.hex()})
    return Daemon(gate, log).serve(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    sys.exit(main())
