"""The operations of the closed recipe, as functions a node runs for an approved `setup_step` (docs/NETWORK_CONTRACT.md 3.9). Each takes (args, scope, ctx) and returns (result, evidence).

Everything here is bounded by what the recipe says (`ctx["recipe"]` is this computer's OWN copy, never the plan's): which hosts a download may come from, how large an unpacked archive may be, what the
server's arguments and endpoints are. Every path is obtained from the `WriteScope`, which refuses anything outside the step's declared paths; nothing here knows an operating system (the per-OS layer
starts the process and sets the execute bit). The server is only ever started on a loopback address, is checked after it starts, and is stopped by its recorded identity (id AND creation time).

Operations (and their undo, `undo_<name>`): fetch_check, unpack_archive, write_server_config, start_server, minimal_call, verify_program."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tarfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import httpx

import envrec_os
from envrec_net import envrun as E
from envrec_net import setup as P
from envrec_node.setup import WriteRefused, WriteScope
from envrec_os import procs

CHUNK = 1 << 20
GIB = 2 ** 30
PID_FILE, LOG_FILE = "server.json", "server.log"
RUN_DIR = f"{P.WRITE_ROOT}/run"


class StepFailed(RuntimeError):
    """A step ran and did not succeed. The message starts with a typed code (`checksum_mismatch`, `server_exited`, ...) that the task result carries to the coordinator."""

    def __init__(self, code: str, text: str = "") -> None:
        super().__init__(f"{code}: {text}"[:300])
        self.code = code


def _recipe(ctx: dict) -> dict:
    r = ctx.get("recipe")
    if not r:
        raise WriteRefused("unknown_recipe")
    return r


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _prune(path: Path, home: Path) -> list[str]:
    """Remove the empty folders above a removed path, up to (not including) the node's own folder. Returns what was removed."""
    gone = []
    p = path.parent
    while p != home and home in p.parents and p.is_dir() and not any(p.iterdir()):
        p.rmdir()
        gone.append(p.name)
        p = p.parent
    return gone


def _loopback(host: str) -> str:
    if host not in procs.LOOPBACK:
        raise StepFailed("not_loopback", f"the server's address must be {' or '.join(procs.LOOPBACK)}, not {host!r}")  # N5: an inference server on a node is only ever on loopback
    return host


def _port(port: Any) -> int:
    if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
        raise StepFailed("bad_port", "the port must be a number from 1024 to 65535")
    return port


# ---- fetch_check -------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def _host_ok(url: str, rec: dict, *, first: bool, origin: str | None) -> None:
    dl = rec["download"]
    u = urlparse(url)
    if u.scheme not in dl["schemes"]:
        raise StepFailed("url_not_allowed", f"scheme {u.scheme!r} is not allowed")
    host = u.hostname or ""
    if first:
        if host not in dl["hosts"]:
            raise StepFailed("url_not_allowed", f"host {host!r} is not one of the hosts this recipe downloads from")
    elif not any(host == s.lstrip(".") or host.endswith(s if s.startswith(".") else "." + s) for s in dl["hosts"][origin or ""]):
        raise StepFailed("url_not_allowed", f"a redirect left the recipe's hosts: {host!r}")


def fetch_check(args: dict, scope: WriteScope, ctx: dict) -> tuple[dict, list[dict]]:
    rec = _recipe(ctx)
    url, want, size = str(args["url"]), str(args["sha256"]).lower(), int(args["bytes"])
    dest = scope.path(str(args["dest"]))
    origin = urlparse(url).hostname
    _host_ok(url, rec, first=True, origin=origin)
    if dest.is_file() and dest.stat().st_size == size and _sha256(dest) == want:
        return {"skipped": True, "bytes": size, "sha256": want}, [{"provider": "local file", "tool": "sha256", "note": "already there with the pinned checksum"}]
    created = not dest.parent.exists()
    dest.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(dest.parent).free
    if free < size * float(rec["download"]["disk_margin"]):
        raise StepFailed("no_disk", f"needs {size} bytes and only {free} are free")
    part = dest.with_name(dest.name + ".part")
    h, got = hashlib.sha256(), 0
    try:
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=20.0), headers={"user-agent": "mas-node/1"}) as c:
            cur = url
            for hop in range(int(rec["download"]["max_redirects"]) + 1):
                with c.stream("GET", cur, follow_redirects=False) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        cur = str(httpx.URL(cur).join(r.headers["location"]))
                        _host_ok(cur, rec, first=False, origin=origin)
                        continue
                    if r.status_code != 200:
                        raise StepFailed("http_error", f"HTTP {r.status_code} from {urlparse(cur).hostname}")
                    with part.open("wb") as fh:
                        for block in r.iter_bytes(CHUNK):
                            got += len(block)
                            if got > size:
                                raise StepFailed("size_mismatch", "more bytes arrived than the catalog pinned")
                            h.update(block)
                            fh.write(block)
                    break
            else:
                raise StepFailed("too_many_redirects", "")
        if got != size:
            raise StepFailed("size_mismatch", f"{got} bytes arrived, the catalog pinned {size}")
        if h.hexdigest() != want:
            raise StepFailed("checksum_mismatch", "the SHA-256 of what arrived is not the pinned one")
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)  # a half or wrong download never stays
        raise
    return {"bytes": got, "sha256": h.hexdigest(), "skipped": False, "created_folder": created}, [{"provider": "httpx", "tool": "download", "host": urlparse(cur).hostname, "sha256": h.hexdigest()}]


def undo_fetch_check(args: dict, scope: WriteScope, ctx: dict) -> tuple[dict, list[dict]]:
    dest = scope.path(str(args["dest"]))
    for f in (dest, dest.with_name(dest.name + ".part")):
        f.unlink(missing_ok=True)
    _prune(dest, scope.home)
    return {"removed": [dest.name], "left": [str(dest)] if dest.exists() else []}, [{"provider": "local file", "tool": "unlink"}]


# ---- unpack_archive ----------------------------------------------------------------------------------------------------------------------------------------------------------------

def _inside(base: Path, target: Path) -> bool:
    return target == base or base in target.parents


def _clean_name(name: str) -> PurePosixPath:
    n = PurePosixPath(name.replace("\\", "/"))
    if n.is_absolute() or ".." in n.parts or (n.parts and ":" in n.parts[0]):
        raise StepFailed("unsafe_archive", f"a path outside the folder: {name[:80]!r}")
    return n


def _unpack_tar(archive: Path, dest: Path, limit: int, layer) -> tuple[int, int]:
    files = total = 0
    with tarfile.open(archive, "r:gz") as tf:
        for m in tf:
            rel = _clean_name(m.name)
            target = (dest / rel).resolve()
            if not _inside(dest, target):
                raise StepFailed("unsafe_archive", f"escapes the folder: {m.name[:80]!r}")
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif m.issym():
                link = PurePosixPath(m.linkname)
                if link.is_absolute() or not _inside(dest, (target.parent / link).resolve()):
                    raise StepFailed("unsafe_archive", f"a link that points outside the folder: {m.name[:80]!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(m.linkname)
            elif m.isfile():
                total += m.size
                if total > limit:
                    raise StepFailed("archive_too_large", "the archive unpacks to more than the recipe allows")
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(m)
                with target.open("wb") as out:
                    shutil.copyfileobj(src, out)  # type: ignore[arg-type]
                if m.mode & stat.S_IXUSR:
                    layer.make_executable(target)
                files += 1
            else:
                raise StepFailed("unsafe_archive", f"a kind of entry that is not a file, folder or inside link: {m.name[:80]!r}")
    return files, total


def _unpack_zip(archive: Path, dest: Path, limit: int, layer) -> tuple[int, int]:
    files = total = 0
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            rel = _clean_name(info.filename)
            target = (dest / rel).resolve()
            if not _inside(dest, target):
                raise StepFailed("unsafe_archive", f"escapes the folder: {info.filename[:80]!r}")
            if (info.external_attr >> 16) & 0o170000 == stat.S_IFLNK:
                raise StepFailed("unsafe_archive", "a link inside a zip is not unpacked")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            total += info.file_size
            if total > limit:
                raise StepFailed("archive_too_large", "the archive unpacks to more than the recipe allows")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            if (info.external_attr >> 16) & stat.S_IXUSR:
                layer.make_executable(target)
            files += 1
    return files, total


def unpack_archive(args: dict, scope: WriteScope, ctx: dict) -> tuple[dict, list[dict]]:
    rec = _recipe(ctx)
    archive, dest = scope.read_path(str(args["archive"])), scope.path(str(args["dest"]))
    fmt, marker = str(args["format"]), dest / rec["unpack"]["marker"]
    if not archive.is_file():
        raise StepFailed("archive_missing", "the archive to unpack is not there: the download step comes first")
    sha = _sha256(archive)
    if marker.is_file() and marker.read_text().strip() == sha:
        return {"skipped": True, "dest": str(dest.name)}, [{"provider": "local file", "note": "already unpacked from this archive"}]
    if dest.exists() and any(dest.iterdir()):
        raise StepFailed("folder_not_empty", "the folder to unpack into already has content that this setup did not put there")
    dest.mkdir(parents=True, exist_ok=True)
    limit = int(float(rec["unpack"]["max_unpacked_gib"]) * GIB)
    layer = envrec_os.current()
    try:
        files, total = {"tar.gz": _unpack_tar, "zip": _unpack_zip}[fmt](archive, dest.resolve(), limit, layer)
    except KeyError:
        raise StepFailed("unknown_archive", f"no way to unpack {fmt!r}") from None
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)  # a half-unpacked folder never stays
        raise
    marker.write_text(sha)
    return {"files": files, "bytes": total, "skipped": False}, [{"provider": "tarfile/zipfile", "tool": "unpack", "files": files}]


def undo_unpack_archive(args: dict, scope: WriteScope, ctx: dict) -> tuple[dict, list[dict]]:
    rec = _recipe(ctx)
    dest = scope.path(str(args["dest"]))
    if dest.exists():
        if not (dest / rec["unpack"]["marker"]).is_file() and any(dest.iterdir()):
            raise StepFailed("not_ours", "this folder has content that this setup did not unpack: it is left alone")
        shutil.rmtree(dest)
    _prune(dest, scope.home)
    return {"removed": [dest.name], "left": [str(dest)] if dest.exists() else []}, [{"provider": "local file", "tool": "remove folder"}]


# ---- verify_program ------------------------------------------------------------------------------------------------------------------------------------------------------------

FLAG = re.compile(r"^(-{1,2}[A-Za-z][A-Za-z0-9-]{0,30}|version)$")


def verify_program(args: dict, scope: WriteScope, ctx: dict) -> tuple[dict, list[dict]]:
    """Run a program the previous steps unpacked, once, with flags only (no shell, a minimal environment, a time limit), see that it exits 0 and, if the plan says what to expect, that the text appears;
    then record where it is (`.mas-exe.json` in its folder) so the environment runs of this node find it by its name. The program must lie inside the step's declared folder."""
    rec = _recipe(ctx)
    tool_dir, binary = scope.path(str(args["tool_dir"])), scope.path(str(args["binary_path"]))
    argv = [str(a) for a in args.get("argv", [])]
    if not argv or not all(FLAG.match(a) for a in argv):
        raise StepFailed("bad_arguments", "a verification passes flags only (for example --version)")
    if not binary.is_file() or not _inside(tool_dir, binary):
        raise StepFailed("program_missing", "the program is not where the plan says: the unpack step comes first")
    run = (ctx.get("run") or procs.run_capture)([str(binary), *argv], binary.parent, {**{k: os.environ[k] for k in E.load_policy()["env_base"] if k in os.environ}, "HOME": str(tool_dir), "USERPROFILE": str(tool_dir)}, timeout_s=float(rec["verify"]["timeout_s"]))
    out = (str(run.get("stdout", "")) + str(run.get("stderr", ""))).strip()
    if run.get("timed_out") or run.get("exit") != 0:
        raise StepFailed("program_failed", f"exit {run.get('exit')}: {out[:160]}")
    expect = str(args.get("expect") or "")
    if expect and expect not in out:
        raise StepFailed("program_unexpected", f"the output does not contain {expect!r}: {out[:120]}")
    (tool_dir / rec["verify"]["record"]).write_text(json.dumps({"exe": str(args["exe"]), "binary": binary.relative_to(tool_dir).as_posix()}), encoding="utf-8")
    return {"ok": True, "exit": 0, "output": out[:160], "binary": binary.name}, [{"provider": "local file", "tool": "run program once", "argv": argv}]


def undo_verify_program(args: dict, scope: WriteScope, ctx: dict) -> tuple[dict, list[dict]]:
    rec = _recipe(ctx)
    (scope.path(str(args["tool_dir"])) / rec["verify"]["record"]).unlink(missing_ok=True)
    return {"removed": [rec["verify"]["record"]]}, [{"provider": "local file", "tool": "unlink"}]


# ---- write_server_config -----------------------------------------------------------------------------------------------------------------------------------------------------------

def write_server_config(args: dict, scope: WriteScope, ctx: dict) -> tuple[dict, list[dict]]:
    path = scope.path(str(args["path"]))
    host, port = _loopback(str(args["host"])), _port(args["port"])
    model = scope.read_path(str(args["model"]))
    if not model.is_file():
        raise StepFailed("model_missing", "the model file to configure is not there")
    cfg = {"host": host, "port": port, "context": int(args["context"]), "ngl": int(args["ngl"]), "backend": str(args["backend"]), "model": str(args["model"]),
           **({"mmproj": str(args["mmproj"])} if args.get("mmproj") else {})}  # settings only: no key, no token, no password
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    envrec_os.current().protect_private(tmp)
    tmp.replace(path)
    return {"path": path.name, "settings": sorted(cfg)}, [{"provider": "local file", "tool": "write settings"}]


def undo_write_server_config(args: dict, scope: WriteScope, ctx: dict) -> tuple[dict, list[dict]]:
    path = scope.path(str(args["path"]))
    path.unlink(missing_ok=True)
    path.with_name(path.name + ".tmp").unlink(missing_ok=True)
    _prune(path, scope.home)
    return {"removed": [path.name], "left": [str(path)] if path.exists() else []}, [{"provider": "local file", "tool": "unlink"}]


# ---- start_server / minimal_call ----------------------------------------------------------------------------------------------------------------------------------------------

def _find_binary(root: Path, name: str) -> Path:
    hits = sorted((p for p in root.rglob("*") if p.is_file() and p.name.lower() == name.lower()), key=lambda p: (len(p.parts), str(p)))
    if not hits:
        raise StepFailed("binary_not_found", f"no {name} inside the unpacked runtime")
    return hits[0]


def _argv(rec: dict, binary: Path, cfg: dict[str, Any]) -> list[str]:
    srv = rec["server"]
    parts = list(srv["argv"]) + (list(srv["argv_mmproj"]) if cfg.get("mmproj") else [])
    return [str(binary)] + [str(x).format(**cfg) for x in parts]


def _pidfile(scope: WriteScope) -> Path:
    return scope.path(f"{RUN_DIR}/{PID_FILE}")


def _read_pid(scope: WriteScope) -> dict | None:
    f = scope.read_path(f"{RUN_DIR}/{PID_FILE}")  # reading it needs no write permission (the minimal call reads it to report the server's memory)
    try:
        return json.loads(f.read_text()) if f.is_file() else None
    except (OSError, ValueError):
        return None


def start_server(args: dict, scope: WriteScope, ctx: dict) -> tuple[dict, list[dict]]:
    rec = _recipe(ctx)
    srv = rec["server"]
    host, port = _loopback(str(args["host"])), _port(args["port"])
    model = scope.read_path(str(args["model"]))
    mmproj = scope.read_path(str(args["mmproj"])) if args.get("mmproj") else None
    if not model.is_file() or (mmproj is not None and not mmproj.is_file()):
        raise StepFailed("model_missing", "the model file is not there: the download step comes first")
    prev = _read_pid(scope)
    if prev and procs.is_ours(prev["pid"], prev["created"]):
        raise StepFailed("already_running", f"a server this setup started is already running (port {prev.get('port')}): stop it first")
    binary = _find_binary(scope.read_path(str(args["runtime_dir"])), str(args["binary_name"]))
    layer = envrec_os.current()
    layer.make_executable(binary)
    argv = _argv(rec, binary, {"model": str(model), "host": host, "port": port, "context": int(args["context"]), "ngl": int(args["ngl"]), "mmproj": str(mmproj) if mmproj else ""})
    log = scope.path(f"{RUN_DIR}/{LOG_FILE}")
    log.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    pid = layer.spawn_detached(argv, binary.parent, log)
    created = procs.created_at(pid) or time.time()
    _pidfile(scope).write_text(json.dumps({"pid": pid, "created": created, "port": port, "host": host, "started": time.time(), "argv": argv[1:]}))
    deadline = t0 + float(ctx.get("ready_timeout_s", srv["ready_timeout_s"]))
    url = f"http://{host if ':' not in host else '[' + host + ']'}:{port}{srv['health_path']}"
    ready = False
    with httpx.Client(timeout=5.0) as c:
        while time.monotonic() < deadline:
            if not procs.is_ours(pid, created):
                tail = " ".join(log.read_text(errors="replace").split()[-40:])[-300:] if log.is_file() else ""
                raise StepFailed("server_exited", f"the server stopped by itself while starting: {tail}")
            try:
                if c.get(url).status_code == 200:
                    ready = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
    if not ready:
        procs.stop(pid, created)
        raise StepFailed("server_not_ready", "the server did not answer its health check in time; it was stopped")
    bound = procs.listening(pid)
    if bound is not None and any(ip not in procs.LOOPBACK for ip, _p in bound):  # N5 at run time: a server on any other address is stopped, not used
        procs.stop(pid, created)
        raise StepFailed("server_not_loopback", f"the server listens on {sorted({ip for ip, _p in bound if ip not in procs.LOOPBACK})}, not only on loopback; it was stopped")
    return ({"pid": pid, "endpoint": f"{host}:{port}", "bound_loopback": (True if bound else None), "listening": [f"{ip}:{p}" for ip, p in (bound or [])], "ready_s": round(time.monotonic() - t0, 2),
             "resident_gib": procs.resident_gib(pid)},
            [{"provider": "psutil", "tool": "listening sockets", "limitations": [] if bound else ["the system did not let the node list the server's sockets: loopback is by the address it was given"]}])


def undo_start_server(args: dict, scope: WriteScope, ctx: dict) -> tuple[dict, list[dict]]:
    info = _read_pid(scope)
    stopped = {"was_running": False, "stopped": True}
    if info:
        stopped = procs.stop(info["pid"], info["created"])
        if not stopped["stopped"]:
            raise StepFailed("undo_incomplete", "the server is still running")
    for name in (PID_FILE, LOG_FILE):
        scope.path(f"{RUN_DIR}/{name}").unlink(missing_ok=True)
    _prune(scope.path(f"{RUN_DIR}/{PID_FILE}"), scope.home)
    run = scope.path(RUN_DIR + "/")
    return {**stopped, "left": [str(run)] if run.exists() else []}, [{"provider": "psutil", "tool": "stop process"}]


def minimal_call(args: dict, scope: WriteScope, ctx: dict) -> tuple[dict, list[dict]]:
    rec = _recipe(ctx)
    srv = rec["server"]
    host, port = _loopback(str(args["host"])), _port(args["port"])
    body = {"messages": [{"role": "user", "content": str(args["prompt"])[:500]}], "max_tokens": int(args["max_tokens"]), "temperature": 0, "stream": False}
    url = f"http://{host if ':' not in host else '[' + host + ']'}:{port}{srv['chat_path']}"
    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=float(ctx.get("call_timeout_s", srv["call_timeout_s"]))) as c:
            r = c.post(url, json=body)
    except httpx.HTTPError as exc:
        raise StepFailed("call_failed", f"{type(exc).__name__}") from None
    latency = int((time.monotonic() - t0) * 1000)
    if r.status_code != 200:
        raise StepFailed("call_failed", f"HTTP {r.status_code}")
    try:
        data = r.json()
        answer = str(data["choices"][0]["message"]["content"]).strip()
    except (ValueError, KeyError, IndexError, TypeError):
        raise StepFailed("call_failed", "the answer did not have the agreed form") from None
    if not answer:
        raise StepFailed("call_failed", "the model answered with nothing")
    n = (data.get("usage") or {}).get("completion_tokens")
    info = _read_pid(scope)
    return ({"ok": True, "tested": "text", "vision": "untested", "endpoint": f"{host}:{port}", "latency_ms": latency, "answer": answer[:200], "tokens": n, "tokens_per_s": round(n / (latency / 1000), 2) if n and latency else None,
             "resident_gib": procs.resident_gib(info["pid"]) if info and procs.is_ours(info["pid"], info["created"]) else None},
            [{"provider": "loopback server", "tool": "chat completion", "limitations": ["one short text prompt; the latency includes loading the first token", "tested: text only. No image was sent, so vision is untested"]}])


def undo_minimal_call(args: dict, scope: WriteScope, ctx: dict) -> tuple[dict, list[dict]]:
    return {"nothing_to_undo": True, "left": []}, []


EXECUTORS = {
    "fetch_check": fetch_check, "undo_fetch_check": undo_fetch_check,
    "unpack_archive": unpack_archive, "undo_unpack_archive": undo_unpack_archive,
    "write_server_config": write_server_config, "undo_write_server_config": undo_write_server_config,
    "start_server": start_server, "undo_start_server": undo_start_server,
    "minimal_call": minimal_call, "undo_minimal_call": undo_minimal_call,
    "verify_program": verify_program, "undo_verify_program": undo_verify_program,
}
