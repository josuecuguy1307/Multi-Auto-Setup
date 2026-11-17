"""The policy gate (guardrails 2 and 6, MISSION 4.1): the only code that writes to the host, and only with a fresh signed approval.

Flow for a write (spec: DECISIONS.md D-003, D-004, D-006):

    plan_write(files)  -> action       the gate reads the current state and states it: each file's pre-state hash, the folders it must create,
                                       and the exact undo (old bytes back, created files and folders removed). The model never writes the undo.
    begin(action)      -> Pending      re-plans from the host's current state and refuses any action that differs from that plan
                                       (pre_state_changed / undo_mismatch / bad_action); then asks the token gate for a nonce.
    execute(token, action)             re-plans again (no token burned on a stale action), consumes the token for that exact action,
                                       re-checks the pre-state, appends the full undo to the audit log, and only then writes.
                                       A failure midway rolls back what was written and registers no undo.

An undo runs only if its hash equals the undo_hash of a write approval this gate consumed and executed, in this process, and only once.
Entries in the log, or tokens of class UNDO for any other undo, do not make an undo valid. A restart forgets every right to undo
(recovery from a key-countersigned log head is later work: DECISIONS D-006, LOG_HEAD).

What an undo restores: the file's bytes and the folders the write created. It does NOT restore ACLs, timestamps or alternate data
streams (files with non-plain attributes are refused instead, D-022): a file replaced and then restored takes its folder's inherited ACL.

The gate copies every action it is handed (it never reads the caller's object again; the copies in begin/begin_undo are defence in
depth, since consume recomputes everything from its own copy), serialises writes and undos (one at a time, from validation to undo
registration, across threads; a nested call from the same thread, e.g. from a log hook, is refused), refuses paths that collide without case or reach an entry through an alias (case variant, 8.3
name), and refuses files with other hard links.

SIMULATED: the host is a folder (`root`) on the dev machine; on the device the gate runs in its own process and the model reaches it only
through a narrow RPC (Phase 2, P2-GUARDRAILS). NOT closed: between the last link check and the write, another process of the same user on
the host could swap a parent folder for a junction and redirect one write outside the root (Python on Windows has no openat/O_NOFOLLOW).
The pre-state checks narrow the window; they do not close it.
"""

from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import threading
import unicodedata
from pathlib import Path

from mas_device import approval as ap
from mas_device.log import AuditLog, LogBroken

MAX_FILE_BYTES = 1 << 20
MAX_NAME = 200  # one path component, in UTF-16 units (NTFS: 255) and in UTF-8 bytes (ext4: 255), leaving room for the temporary name
TEMP_SUFFIX_LEN = 18  # "." + name + ".mas-tmp-" + 8 hex: the temporary file next to the target is this much longer than the target
LOGGED_REJECTIONS = ("expired", "counter_regressed", "wrong_session")  # authenticated tokens that can never be spent: logged with their counter


def _long_paths_enabled() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            return winreg.QueryValueEx(k, "LongPathsEnabled")[0] == 1
    except OSError:
        return False


PATH_LIMIT = (32000 if _long_paths_enabled() else 259) if sys.platform == "win32" else 4095  # MAX_PATH minus the NUL, or PATH_MAX
PLAIN_ATTRIBUTES = 0x20 | 0x80 | 0x2000  # ARCHIVE, NORMAL, NOT_CONTENT_INDEXED: anything else (hidden, system, read-only...) is refused
REASONS = ("bad_action", "outside_root", "pre_state_changed", "undo_mismatch", "undo_not_issued", "log_failed", "apply_failed", "too_large")
WRITE_KEYS = {"kind", "target", "files", "mkdirs", "undo"}
RESTORE_KEYS = {"kind", "target", "files", "rmdirs"}
FILE_KEYS = {"path", "pre", "content_b64"}
_RESERVED = ({"con", "prn", "aux", "nul", "conin$", "conout$", "clock$"} | {f"com{i}" for i in "0123456789\u00b9\u00b2\u00b3"}
             | {f"lpt{i}" for i in "0123456789\u00b9\u00b2\u00b3"})


class Refused(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        assert code in REASONS, code
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _b64(data: bytes | None) -> str | None:
    return None if data is None else base64.b64encode(data).decode("ascii")


def _unb64(text) -> bytes | None:
    if text is None:
        return None
    if not isinstance(text, str):
        raise Refused("bad_action", "content_b64 is a string or null")
    try:
        data = base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise Refused("bad_action", "content_b64 is not base64") from None
    if len(data) > MAX_FILE_BYTES:
        raise Refused("too_large", f"more than {MAX_FILE_BYTES} bytes")
    return data


def _check_rel(rel) -> list[str]:
    """A relative POSIX path with no '.', '..', empty part, drive, backslash, NUL, non-NFC text or Windows device name."""
    if not isinstance(rel, str) or not rel:
        raise Refused("bad_action", "a path is a non-empty string")
    if any(0xD800 <= ord(c) <= 0xDFFF for c in rel):
        raise Refused("bad_action", "path has a lone surrogate")
    if unicodedata.normalize("NFC", rel) != rel or any(ord(c) < 0x20 for c in rel):
        raise Refused("bad_action", "path is not NFC or has control characters")
    if rel.startswith("/") or "\\" in rel or ":" in rel:
        raise Refused("outside_root", rel)
    if any(c in '<>"|?*' for c in rel):
        raise Refused("bad_action", "characters Windows cannot hold in a name")
    parts = rel.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise Refused("outside_root", rel)
        if len(part.encode("utf-16-le")) // 2 > MAX_NAME or len(part.encode("utf-8")) > MAX_NAME:
            raise Refused("bad_action", f"a name longer than {MAX_NAME} UTF-16 units or UTF-8 bytes")
        if re.search(r"~\d", part):
            raise Refused("bad_action", f"{part!r} looks like an 8.3 short name: it could alias another entry of the same action")
        if part.split(".")[0].rstrip(" ").lower() in _RESERVED or part[-1] in ". ":  # "CON .txt" is CON on older Windows
            raise Refused("bad_action", f"{part!r} is not portable")
    return parts


def _is_link(p: Path) -> bool:
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


# ---- the only functions that write --------------------------------------------------------

def _replace_file(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.mas-tmp-{secrets.token_hex(4)}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o644)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _remove_file(path: Path) -> None:
    os.unlink(path)


def _make_dir(path: Path) -> None:
    os.mkdir(path)


def _remove_dir(path: Path) -> None:
    os.rmdir(path)


# ---- the gate -------------------------------------------------------------------------------

class PolicyGate:
    def __init__(self, root: Path, approvals: ap.Gate, log: AuditLog, *, target: str) -> None:
        self.root = Path(root).resolve(strict=True)
        self._root_real = os.path.realpath(self.root)
        self._approvals, self._log, self.target = approvals, log, target
        self._undo_by_write: dict[bytes, bytes] = {}   # write action_hash -> undo_hash, for executed writes
        self._undoable: dict[bytes, dict] = {}         # undo_hash -> undo action, until it runs once
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()  # one write or undo at a time, from validation to the undo registration
        self._run_owner: int | None = None   # a nested call from the same thread (a log hook) is refused, not run

    # -- paths and state

    def _resolve(self, rel) -> Path:
        p = self.root
        parts = _check_rel(rel)
        for i, part in enumerate(parts):
            parent, p = p, p / part
            if _is_link(p):
                raise Refused("outside_root", f"{rel}: a link inside the path")
            if os.path.lexists(p) and part not in os.listdir(parent):
                raise Refused("bad_action", f"{rel}: {part!r} is not the entry's own name (case variant or alias)")
            if i < len(parts) - 1 and os.path.lexists(p) and not p.is_dir():
                raise Refused("bad_action", f"{rel}: {part!r} is a file, not a folder")
        probe = p
        while not probe.exists() and probe != self.root:
            probe = probe.parent
        real = os.path.realpath(probe)
        if os.path.normcase(os.path.commonpath([real, self._root_real])) != os.path.normcase(self._root_real):
            raise Refused("outside_root", rel)
        return p

    def _current(self, p: Path) -> bytes | None:
        if not os.path.lexists(p):
            return None
        if not p.is_file() or _is_link(p):
            raise Refused("bad_action", f"{p.name} is not a regular file")
        attrs = getattr(p.stat(), "st_file_attributes", 0)
        if attrs & ~PLAIN_ATTRIBUTES:
            raise Refused("bad_action", f"{p.name} has attributes the undo could not restore (0x{attrs:x})")
        if p.stat().st_nlink > 1:
            raise Refused("bad_action", f"{p.name} has other hard links: its bytes may live outside the root")
        if p.stat().st_size > MAX_FILE_BYTES:
            raise Refused("too_large", p.name)
        return p.read_bytes()

    def read(self, rel: str) -> bytes:
        """Reads need no token (read-only by default) but stay inside the root."""
        data = self._current(self._resolve(rel))
        if data is None:
            raise FileNotFoundError(rel)
        return data

    # -- planning

    def plan_write(self, files: dict) -> dict:
        if not isinstance(files, dict) or not files:
            raise Refused("bad_action", "files is a non-empty mapping of path to bytes or None")
        entries, undo_entries, mkdirs = [], [], []
        spelled: dict[str, str] = {}
        for rel in files:
            if not isinstance(rel, str):
                raise Refused("bad_action", "a path is a string")
            parts = rel.split("/")
            for i in range(1, len(parts) + 1):
                prefix = "/".join(parts[:i])
                key = unicodedata.normalize("NFC", prefix).casefold()
                if spelled.setdefault(key, prefix) != prefix:
                    raise Refused("bad_action", f"{prefix!r} and {spelled[key]!r} are the same entry on a case-insensitive host")
        folders = {unicodedata.normalize("NFC", "/".join(r.split("/")[:i])).casefold() for r in files for i in range(1, len(r.split("/")))}
        clash = [r for r in files if unicodedata.normalize("NFC", r).casefold() in folders]
        if clash:
            raise Refused("bad_action", f"{clash[0]!r} would be both a file and a folder of this action")
        for rel in sorted(files):
            data = files[rel]
            p = self._resolve(rel)
            if len(str(p)) + TEMP_SUFFIX_LEN > PATH_LIMIT:
                raise Refused("bad_action", f"{rel}: the path is too long for this host ({PATH_LIMIT})")
            if data is not None and not isinstance(data, (bytes, bytearray)):
                raise Refused("bad_action", f"{rel}: content is bytes or None")
            if data is not None and len(data) > MAX_FILE_BYTES:
                raise Refused("too_large", rel)
            data = None if data is None else bytes(data)
            cur = self._current(p)
            if cur is None and data is None:
                raise Refused("bad_action", f"{rel}: nothing to delete")
            entries.append({"path": rel, "pre": _sha(cur), "content_b64": _b64(data)})
            undo_entries.append({"path": rel, "pre": _sha(data), "content_b64": _b64(cur)})
            if data is not None:
                parts = rel.split("/")
                for i in range(1, len(parts)):
                    d = "/".join(parts[:i])
                    if not (self.root / d).exists() and d not in mkdirs:
                        mkdirs.append(d)
        mkdirs.sort(key=lambda d: (d.count("/"), d))
        undo = {"kind": "files.restore", "target": self.target, "files": undo_entries, "rmdirs": sorted(mkdirs, key=lambda d: (-d.count("/"), d))}
        action = {"kind": "files.write", "target": self.target, "files": entries, "mkdirs": mkdirs, "undo": undo}
        ap.action_hash(action)  # strict canonical form or Rejected
        return action

    def _shape(self, action, keys: set, kind: str, dirs_key: str) -> None:
        if not isinstance(action, dict) or set(action) != keys or action.get("kind") != kind or action.get("target") != self.target:
            raise Refused("bad_action", f"not a {kind} action for {self.target}")
        files, dirs = action["files"], action[dirs_key]
        if not isinstance(files, list) or not files or not isinstance(dirs, list):
            raise Refused("bad_action", "files and folders are lists")
        paths = []
        for f in files:
            if not isinstance(f, dict) or set(f) != FILE_KEYS:
                raise Refused("bad_action", "a file entry is {path, pre, content_b64}")
            self._resolve(f["path"])
            if f["pre"] is not None and not (isinstance(f["pre"], str) and len(f["pre"]) == 64 and all(c in "0123456789abcdef" for c in f["pre"])):
                raise Refused("bad_action", "pre is a sha256 hex or null")
            _unb64(f["content_b64"])
            paths.append(f["path"])
        if paths != sorted(set(paths)):
            raise Refused("bad_action", "file paths are unique and sorted")
        for d in dirs:
            self._resolve(d)
        ap.action_hash(action)

    def _validate_write(self, action) -> None:
        """The action must be exactly what this gate would plan now from the host's current state."""
        self._shape(action, WRITE_KEYS, "files.write", "mkdirs")
        expected = self.plan_write({f["path"]: _unb64(f["content_b64"]) for f in action["files"]})
        if [f["pre"] for f in action["files"]] != [f["pre"] for f in expected["files"]] or action["mkdirs"] != expected["mkdirs"]:
            raise Refused("pre_state_changed", "the host changed since this action was planned")
        if action["files"] != expected["files"]:
            raise Refused("bad_action")
        if action["undo"] != expected["undo"]:
            raise Refused("undo_mismatch", "the undo is derived by the gate, not supplied")

    def _check_state(self, action: dict) -> None:
        for f in action["files"]:
            if _sha(self._current(self._resolve(f["path"]))) != f["pre"]:
                raise Refused("pre_state_changed", f["path"])
        if action["kind"] == "files.write":
            for d in action["mkdirs"]:
                if os.path.lexists(self._resolve(d)):
                    raise Refused("pre_state_changed", f"{d} exists now")
        else:
            for f in action["files"]:
                if f["content_b64"] is not None and not self._resolve(f["path"]).parent.is_dir():
                    raise Refused("pre_state_changed", f"{f['path']}: the folder to restore it into is gone")
            leaving = {f["path"] for f in action["files"] if f["content_b64"] is None} | set(action["rmdirs"])
            for d in action["rmdirs"]:
                p = self._resolve(d)
                if not p.is_dir() or _is_link(p):
                    raise Refused("pre_state_changed", f"{d} is not the folder this write created")
                for child in p.iterdir():
                    if f"{d}/{child.name}" not in leaving:
                        raise Refused("pre_state_changed", f"{d} holds {child.name}, which the undo would not remove")

    def _require_issued(self, undo) -> bytes:
        try:
            h = ap.action_hash(undo)
        except ap.Rejected:
            raise Refused("undo_not_issued") from None
        with self._lock:
            if h not in self._undoable:
                raise Refused("undo_not_issued", "not bound to a write this gate executed, or already run")
        return h

    # -- the two entry points that lead to a write

    @staticmethod
    def _own(action):
        """The gate's own plain copy: callers' objects (dict subclasses, objects other threads hold) are never read again."""
        try:
            return json.loads(ap.canonical(action))
        except ap.Rejected:
            raise Refused("bad_action", "not a canonical action") from None

    def begin(self, action) -> ap.Pending:
        action = self._own(action)
        self._validate_write(action)
        return self._approvals.begin(action, ap.Class.WRITE)

    def execute(self, token, action) -> dict:
        return self._run(token, action, ap.Class.WRITE)

    def undo_for(self, write_action_hash: bytes) -> dict:
        if type(write_action_hash) not in (bytes, bytearray):
            raise Refused("undo_not_issued", "an action hash is bytes")
        write_action_hash = bytes(write_action_hash)
        with self._lock:
            uh = self._undo_by_write.get(write_action_hash)
            if uh is None or uh not in self._undoable:
                raise Refused("undo_not_issued")
            return copy.deepcopy(self._undoable[uh])

    def begin_undo(self, undo) -> ap.Pending:
        undo = self._own(undo)
        self._require_issued(undo)
        self._shape(undo, RESTORE_KEYS, "files.restore", "rmdirs")
        self._check_state(undo)
        return self._approvals.begin(undo, ap.Class.UNDO)

    def execute_undo(self, token, undo) -> dict:
        return self._run(token, undo, ap.Class.UNDO)

    def _run(self, token, action, cls: ap.Class) -> dict:
        action = self._own(action)
        if self._run_owner == threading.get_ident():
            raise Refused("bad_action", "a write cannot start inside another write")
        with self._run_lock:
            self._run_owner = threading.get_ident()
            try:
                return self._run_locked(token, action, cls)
            finally:
                self._run_owner = None

    def _run_locked(self, token, action, cls: ap.Class) -> dict:
        if cls == ap.Class.WRITE:
            self._validate_write(action)          # stale or forged: refused before the token is touched
        else:
            self._require_issued(action)
            self._shape(action, RESTORE_KEYS, "files.restore", "rmdirs")
            self._check_state(action)
        kind = "write" if cls == ap.Class.WRITE else "undo"
        try:
            approval = self._approvals.consume(token, action, cls=cls)
        except ap.Rejected as exc:
            if exc.code in LOGGED_REJECTIONS:  # the signature was verified first: the counter is authentic and is now never spent
                self._log_rejected(kind, token, exc.code)
            raise
        try:
            self._check_state(action)                 # again, after the token is spent
        except (Refused, OSError) as exc:  # the token is spent: the log says so, so no counter goes missing from it (rule 5)
            code = exc.code if isinstance(exc, Refused) else "pre_state_changed"
            try:
                self._log.append(f"{kind}.refused", {"action_hash": approval.action_hash.hex(), "counter": approval.counter,
                                                     "key_id": approval.key_id.hex(), "nonce": approval.nonce.hex(), "reason": code})
            except (LogBroken, OSError):
                pass
            if isinstance(exc, Refused):
                raise
            raise Refused(code, f"the host could not be read after the token was spent: {type(exc).__name__}") from None
        try:
            self._log.append(f"{kind}.approved", {
                "action_hash": approval.action_hash.hex(), "undo_hash": approval.undo_hash.hex(), "key_id": approval.key_id.hex(),
                "counter": approval.counter, "nonce": approval.nonce.hex(), "paths": [f["path"] for f in action["files"]],
                "undo": action.get("undo")})
        except (LogBroken, OSError) as exc:
            raise Refused("log_failed", str(exc)[:120]) from None
        try:
            self._apply(action)
        except (OSError, Refused) as exc:
            code = exc.code if isinstance(exc, Refused) else "apply_failed"
            try:
                self._log.append(f"{kind}.failed", {"action_hash": approval.action_hash.hex(), "reason": code, "error": str(exc)[:200]})
            except (LogBroken, OSError):
                pass
            raise Refused(code, str(exc)[:120]) from None
        with self._lock:
            if cls == ap.Class.WRITE:
                self._undo_by_write[approval.action_hash] = approval.undo_hash
                self._undoable[approval.undo_hash] = copy.deepcopy(action["undo"])
            else:
                self._undoable.pop(approval.action_hash, None)
        post = {f["path"]: _sha(_unb64(f["content_b64"])) for f in action["files"]}
        try:
            self._log.append(f"{kind}.done", {"action_hash": approval.action_hash.hex(), "post": post})
        except (LogBroken, OSError) as exc:
            raise Refused("log_failed", f"written, but the done record failed: {str(exc)[:100]}") from None
        return {"action_hash": approval.action_hash.hex(), "undo_hash": approval.undo_hash.hex(), "counter": approval.counter}

    def _log_rejected(self, kind: str, token, code: str) -> None:
        try:
            b = ap.unpack_body(bytes(token)[:ap.BODY_LEN])
            self._log.append(f"{kind}.rejected", {"counter": b.counter, "key_id": b.key_id.hex(), "nonce": b.req.nonce.hex(),
                                                  "action_hash": b.req.action_hash.hex(), "reason": code})
        except (LogBroken, OSError, ap.Rejected, ValueError):
            pass

    def _apply(self, action: dict) -> None:
        """Make the change, or put back everything this call changed and re-raise. Each file is compared with its approved pre-state
        immediately before it is replaced or deleted, and each folder must still be absent immediately before it is made: an edit made
        on the host after the last check (an editor's autosave during the log append) is never overwritten."""
        done: list[tuple] = []
        try:
            for d in action.get("mkdirs", []):
                p = self._resolve(d)
                if os.path.lexists(p):
                    raise Refused("pre_state_changed", f"{d} appeared before it was made")
                _make_dir(p)
                done.append(("mkdir", p, None))
            for f in action["files"]:
                p = self._resolve(f["path"])
                prev = self._current(p)
                if _sha(prev) != f["pre"]:
                    raise Refused("pre_state_changed", f"{f['path']} changed after the approval was logged")
                data = _unb64(f["content_b64"])
                if data is None:
                    _remove_file(p)
                else:
                    _replace_file(p, data)
                done.append(("file", p, prev))
            for d in action.get("rmdirs", []):
                p = self._resolve(d)
                _remove_dir(p)
                done.append(("rmdir", p, None))
        except (OSError, Refused) as exc:
            for op, p, prev in reversed(done):
                try:
                    if op == "mkdir":
                        _remove_dir(p)
                    elif op == "rmdir":
                        _make_dir(p)
                    elif prev is None:
                        _remove_file(p)
                    else:
                        _replace_file(p, prev)
                except OSError:
                    print(f"ERROR gate rollback incomplete at {p}", file=sys.stderr)
            if isinstance(exc, Refused):
                raise
            raise OSError(str(exc)) from None
