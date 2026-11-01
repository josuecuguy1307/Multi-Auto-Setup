"""The node's setup operations (envrec_node.executors): download with a SHA-256 check, safe unpacking, the loopback-only server, the minimal call and the undo of each. SIMULATED-LOCAL: the origin is a
server on loopback and the "runtime" is a small program with the real server's flags and endpoints. The server tests need a POSIX program to start: they are skipped on Windows (UNTESTED there)."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import socket
import stat
import sys
import tarfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import envrec_os
from envrec_coordinator.setup_plan import load_recipes
from envrec_node import executors as X
from envrec_node.setup import WriteRefused, WriteScope
from envrec_os import procs

REAL = load_recipes()["local-server"]
POSIX = envrec_os.family() != "windows"
FAKE_SERVER = '''#!%s
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
a = sys.argv[1:]
get = lambda f, d=None: a[a.index(f) + 1] if f in a else d
host, port, mode = get("--host"), int(get("--port")), get("-c")
if "%s" == "wide": host = "0.0.0.0"
class H(BaseHTTPRequestHandler):
    def log_message(self, *x): pass
    def do_GET(self):
        self.send_response(200 if self.path == "/health" else 404); self.end_headers(); self.wfile.write(b"ok")
    def do_POST(self):
        n = int(self.headers.get("content-length", 0)); self.rfile.read(n)
        body = json.dumps({"choices": [{"message": {"content": "ready"}}], "usage": {"completion_tokens": 2}}).encode()
        self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers(); self.wfile.write(body)
if "%s" == "exit": sys.exit(3)
if "%s" == "hang": import time; time.sleep(600)
HTTPServer((host, port), H).serve_forever()
'''


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def tar_bytes(members: dict, *, server: str | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            ti = tarfile.TarInfo(name)
            if isinstance(data, tarfile.TarInfo):
                tf.addfile(data)
                continue
            ti.size, ti.mode = len(data), 0o755 if name.endswith("llama-server") else 0o644
            tf.addfile(ti, io.BytesIO(data))
    return buf.getvalue()


def fake_runtime(mode: str = "normal") -> bytes:
    return tar_bytes({"llama-b1/llama-server": (FAKE_SERVER % (sys.executable, mode, mode, mode)).encode(), "llama-b1/README": b"x"})


class Origin:
    """A web server on loopback that serves named bodies and can redirect."""

    def __init__(self, files: dict[str, bytes], redirects: dict[str, str] | None = None) -> None:
        files, redirects = files, redirects or {}

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path in redirects:
                    self.send_response(302)
                    self.send_header("location", redirects[self.path])
                    self.end_headers()
                elif self.path in files:
                    self.send_response(200)
                    self.send_header("content-length", str(len(files[self.path])))
                    self.end_headers()
                    self.wfile.write(files[self.path])
                else:
                    self.send_response(404)
                    self.end_headers()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def world(tmp_path):
    recipe = copy.deepcopy(REAL)
    recipe["download"] = {"schemes": ["http"], "hosts": {"127.0.0.1": ["127.0.0.1"]}, "max_redirects": 3, "disk_margin": 1.0}  # a test origin on loopback; the real recipe says https and the real hosts
    home = tmp_path / "mas"
    home.mkdir()
    made: list[Origin] = []

    def origin(files, redirects=None):
        o = Origin(files, redirects)
        made.append(o)
        return o
    ctx = {"recipe": recipe, "mas_home": home, "ready_timeout_s": 20, "call_timeout_s": 10}
    scope = lambda *declared: WriteScope(home, list(declared))  # noqa: E731
    yield type("W", (), {"recipe": recipe, "home": home, "ctx": ctx, "origin": staticmethod(origin), "scope": staticmethod(scope)})
    for o in made:
        o.close()
    for pidf in home.rglob("server.json"):  # never leave a server behind, whatever a test did
        d = json.loads(pidf.read_text())
        procs.stop(d["pid"], d["created"], grace_s=2)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def fetch(w, body: bytes, name="f.bin", **over):
    o = w.origin({"/" + name: body})
    args = {"url": o.url("/" + name), "sha256": sha(body), "bytes": len(body), "dest": f"<mas>/downloads/{name}", **over}
    return X.fetch_check(args, w.scope(args["dest"]), w.ctx), args


# ---- download ------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def test_a_download_is_checked_against_the_pinned_size_and_sha256_and_is_idempotent(world):
    body = b"hello model" * 1000
    out, args = fetch(world, body)
    assert out[0]["skipped"] is False and out[0]["bytes"] == len(body) and out[0]["sha256"] == sha(body)
    assert (world.home / "downloads" / "f.bin").read_bytes() == body and not list((world.home / "downloads").glob("*.part"))
    again = X.fetch_check(args, world.scope(args["dest"]), world.ctx)
    assert again[0]["skipped"] is True  # already there with the pinned checksum: nothing is downloaded again


@pytest.mark.parametrize("what", ["wrong_sha", "wrong_size_smaller", "wrong_size_bigger"])
def test_a_download_that_is_not_what_the_catalog_pinned_is_refused_and_leaves_nothing(world, what):
    body = b"x" * 5000
    o = world.origin({"/f": body})
    args = {"url": o.url("/f"), "sha256": sha(body) if what != "wrong_sha" else "0" * 64, "bytes": {"wrong_size_smaller": 100, "wrong_size_bigger": 9000}.get(what, len(body)), "dest": "<mas>/downloads/f"}
    with pytest.raises(X.StepFailed) as e:
        X.fetch_check(args, world.scope(args["dest"]), world.ctx)
    assert e.value.code in ("checksum_mismatch", "size_mismatch")
    assert not (world.home / "downloads" / "f").exists() and not list((world.home / "downloads").glob("*.part"))  # neither the file nor a half file stays


def test_a_download_only_comes_from_the_hosts_the_recipe_names_and_a_redirect_cannot_leave_them(world):
    body = b"y" * 100
    o = world.origin({"/f": body}, redirects={"/go": "http://localhost:9/elsewhere"})
    base = {"sha256": sha(body), "bytes": 100, "dest": "<mas>/downloads/f"}
    for url, code in (("https://evil.example/f", "url_not_allowed"), ("ftp://127.0.0.1/f", "url_not_allowed"), (o.url("/go"), "url_not_allowed")):
        with pytest.raises(X.StepFailed) as e:
            X.fetch_check({**base, "url": url}, world.scope(base["dest"]), world.ctx)
        assert e.value.code == code, url
    with pytest.raises(X.StepFailed) as e:
        X.fetch_check({**base, "url": o.url("/missing")}, world.scope(base["dest"]), world.ctx)
    assert e.value.code == "http_error"


def test_a_download_cannot_write_outside_its_declared_path(world):
    body = b"z" * 10
    o = world.origin({"/f": body})
    with pytest.raises(WriteRefused):
        X.fetch_check({"url": o.url("/f"), "sha256": sha(body), "bytes": 10, "dest": "<mas>/other/f"}, world.scope("<mas>/downloads/f"), world.ctx)


def test_the_undo_of_a_download_removes_the_file_and_the_empty_folder_it_made_and_nothing_else(world):
    out, args = fetch(world, b"q" * 50)
    (world.home / "keep.txt").write_text("mine")
    undone = X.undo_fetch_check(args, world.scope(args["dest"]), world.ctx)[0]
    assert undone["left"] == [] and not (world.home / "downloads").exists() and (world.home / "keep.txt").read_text() == "mine"
    assert X.undo_fetch_check(args, world.scope(args["dest"]), world.ctx)[0]["left"] == []  # undoing twice is harmless


# ---- unpack --------------------------------------------------------------------------------------------------------------------------------------------------------------------------

def unpack(w, data: bytes, fmt="tar.gz", name="a.tar.gz"):
    (w.home / "downloads").mkdir(exist_ok=True)
    (w.home / "downloads" / name).write_bytes(data)
    args = {"archive": f"<mas>/downloads/{name}", "dest": "<mas>/runtime/rt/", "format": fmt}
    return X.unpack_archive(args, w.scope("<mas>/runtime/rt/"), w.ctx), args


@pytest.mark.skipif(not POSIX, reason="the execute bit and links are a POSIX matter; the Windows branch of the layer has no equivalent (UNTESTED there)")
def test_a_tar_archive_is_unpacked_with_its_execute_bit_and_marked_as_ours(world):
    (out, _), args = unpack(world, fake_runtime())
    assert out["files"] == 2 and (world.home / "runtime" / "rt" / "llama-b1" / "llama-server").stat().st_mode & stat.S_IXUSR
    assert (world.home / "runtime" / "rt" / ".mas-unpacked").read_text() == sha((world.home / "downloads" / "a.tar.gz").read_bytes())  # the marker names the archive it came from
    assert X.unpack_archive(args, world.scope("<mas>/runtime/rt/"), world.ctx)[0]["skipped"] is True


def test_a_zip_archive_is_unpacked_and_a_zip_with_a_link_or_an_escaping_name_is_refused(world):
    good = io.BytesIO()
    with zipfile.ZipFile(good, "w") as zf:
        zf.writestr("llama/llama-server.exe", b"MZ")
        zf.writestr("llama/ggml.dll", b"x")
    (out, _), _ = unpack(world, good.getvalue(), "zip", "a.zip")
    assert out["files"] == 2 and (world.home / "runtime" / "rt" / "llama" / "ggml.dll").is_file()
    X.undo_unpack_archive({"dest": "<mas>/runtime/rt/"}, world.scope("<mas>/runtime/rt/"), world.ctx)
    for bad_name in ("../escape.txt", "/abs/escape.txt", "C:/escape.txt", "a/../../escape.txt"):
        bad = io.BytesIO()
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr(bad_name, b"x")
        with pytest.raises(X.StepFailed) as e:
            unpack(world, bad.getvalue(), "zip", "b.zip")
        assert e.value.code == "unsafe_archive", bad_name
        assert not (world.home / "runtime" / "rt").exists() and not (world.home.parent / "escape.txt").exists()  # a refused archive leaves no half-unpacked folder
    link = io.BytesIO()
    with zipfile.ZipFile(link, "w") as zf:
        zi = zipfile.ZipInfo("l")
        zi.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(zi, "../../etc")
    with pytest.raises(X.StepFailed) as e:
        unpack(world, link.getvalue(), "zip", "c.zip")
    assert e.value.code == "unsafe_archive"


@pytest.mark.skipif(not POSIX, reason="links in a tar archive are made with the POSIX file API")
def test_a_tar_archive_that_escapes_through_a_name_a_link_a_hard_link_or_a_device_is_refused(world):
    def member(name, **kw):
        ti = tarfile.TarInfo(name)
        for k, v in kw.items():
            setattr(ti, k, v)
        return ti
    cases = {"dotdot": {"../x": b"x"}, "absolute": {"/tmp/x": b"x"},
             "outside_link": {"l": member("l", type=tarfile.SYMTYPE, linkname="../../outside")}, "absolute_link": {"l": member("l", type=tarfile.SYMTYPE, linkname="/etc/passwd")},
             "hard_link": {"h": member("h", type=tarfile.LNKTYPE, linkname="x")}, "device": {"d": member("d", type=tarfile.CHRTYPE)}}
    for name, members in cases.items():
        with pytest.raises(X.StepFailed) as e:
            unpack(world, tar_bytes(members))
        assert e.value.code == "unsafe_archive", name
        assert not (world.home / "runtime" / "rt").exists(), name
    ok = tar_bytes({"a/b": b"1", "inside": member("inside", type=tarfile.SYMTYPE, linkname="a/b")})
    assert unpack(world, ok)[0][0]["files"] == 1  # a link that stays inside the folder is fine


def test_an_archive_that_unpacks_to_more_than_the_recipe_allows_or_into_a_folder_that_is_not_ours_is_refused(world):
    world.ctx["recipe"]["unpack"]["max_unpacked_gib"] = 0.000001  # about 1 KB
    with pytest.raises(X.StepFailed) as e:
        unpack(world, tar_bytes({"big": b"0" * 100_000}))
    assert e.value.code == "archive_too_large" and not (world.home / "runtime" / "rt").exists()
    world.ctx["recipe"]["unpack"]["max_unpacked_gib"] = 8
    (world.home / "runtime" / "rt").mkdir(parents=True)
    (world.home / "runtime" / "rt" / "mine.txt").write_text("not from this setup")
    with pytest.raises(X.StepFailed) as e:
        unpack(world, tar_bytes({"a": b"1"}))
    assert e.value.code == "folder_not_empty"
    with pytest.raises(X.StepFailed) as e:
        X.undo_unpack_archive({"dest": "<mas>/runtime/rt/"}, world.scope("<mas>/runtime/rt/"), world.ctx)
    assert e.value.code == "not_ours" and (world.home / "runtime" / "rt" / "mine.txt").exists()  # the undo does not delete what this setup did not put there


@pytest.mark.skipif(not POSIX, reason="needs the POSIX test program")
def test_the_undo_of_an_unpack_removes_exactly_the_unpacked_folder(world):
    (_, args) = unpack(world, fake_runtime())
    out = X.undo_unpack_archive(args, world.scope("<mas>/runtime/rt/"), world.ctx)[0]
    assert out["left"] == [] and not (world.home / "runtime").exists() and (world.home / "downloads" / "a.tar.gz").exists()  # the download is another step's to undo


# ---- the server config, the loopback-only server, the minimal call ----------------------------------------------------------------------------------------------

def config(w, **over):
    (w.home / "downloads").mkdir(exist_ok=True)
    (w.home / "downloads" / "m.gguf").write_bytes(b"gguf")
    args = {"path": "<mas>/config/server.json", "model": "<mas>/downloads/m.gguf", "host": "127.0.0.1", "port": 8099, "context": 512, "ngl": 0, "backend": "cpu", "mmproj": None, **over}
    return X.write_server_config(args, w.scope(args["path"]), w.ctx), args


@pytest.mark.parametrize("host", ["0.0.0.0", "localhost", "192.168.1.5", "::", "", "127.0.0.2"])
def test_a_server_address_that_is_not_a_loopback_literal_is_refused_everywhere_it_could_be_used(world, host):
    with pytest.raises(X.StepFailed) as e:
        config(world, host=host)
    assert e.value.code == "not_loopback" and not (world.home / "config").exists()
    with pytest.raises(X.StepFailed) as e:
        X.minimal_call({"host": host, "port": 8099, "prompt": "hi", "max_tokens": 4}, world.scope(), world.ctx)
    assert e.value.code == "not_loopback"
    with pytest.raises(X.StepFailed) as e:
        X.start_server({"host": host, "port": 8099, "model": "<mas>/downloads/m.gguf", "runtime_dir": "<mas>/runtime/rt/", "binary_name": "x", "context": 1, "ngl": 0}, world.scope("<mas>/run/"), world.ctx)
    assert e.value.code in ("not_loopback", "model_missing") and e.value.code != "server_exited"


def test_the_config_holds_settings_only_is_private_and_is_undone_exactly(world):
    (out, _), args = config(world)
    cfg = json.loads((world.home / "config" / "server.json").read_text())
    assert cfg["host"] == "127.0.0.1" and not [k for k in cfg if any(w in k.lower() for w in ("key", "token", "secret", "password"))]
    assert envrec_os.current().is_private(world.home / "config" / "server.json")
    for bad in (80, 70000, "8080", True):
        with pytest.raises(X.StepFailed):
            config(world, port=bad)
    assert X.undo_write_server_config(args, world.scope(args["path"]), world.ctx)[0]["left"] == [] and not (world.home / "config").exists()


def start_args(w, **over):
    return {"runtime_dir": "<mas>/runtime/rt/", "binary_name": "llama-server", "config": "<mas>/config/server.json", "model": "<mas>/downloads/m.gguf", "host": "127.0.0.1", "port": free_port(),
            "context": 512, "ngl": 0, "mmproj": None, **over}


def run_server(w, mode="normal", **over):
    if (w.home / "runtime" / "rt").exists():
        X.undo_unpack_archive({"dest": "<mas>/runtime/rt/"}, w.scope("<mas>/runtime/rt/"), w.ctx)
    unpack(w, fake_runtime(mode))
    (w.home / "downloads" / "m.gguf").write_bytes(b"gguf")
    args = start_args(w, **over)
    return X.start_server(args, w.scope("<mas>/run/"), w.ctx), args


@pytest.mark.skipif(not POSIX, reason="the test program is a POSIX script (the Windows branch of start/stop is UNTESTED)")
def test_the_server_starts_on_loopback_answers_the_minimal_call_and_the_undo_stops_it_and_leaves_nothing(world):
    ((out, ev), args) = run_server(world)
    assert out["endpoint"] == f"127.0.0.1:{args['port']}" and out["bound_loopback"] in (True, None) and all(a.startswith("127.0.0.1:") for a in out["listening"])
    pid = json.loads((world.home / "run" / "server.json").read_text())
    assert procs.is_ours(pid["pid"], pid["created"])
    call = X.minimal_call({"host": "127.0.0.1", "port": args["port"], "prompt": "Reply with the single word: ready", "max_tokens": 8}, world.scope(), world.ctx)[0]
    assert call["ok"] is True and call["answer"] == "ready" and call["latency_ms"] >= 0 and call["tokens"] == 2 and call["endpoint"] == f"127.0.0.1:{args['port']}"
    with pytest.raises(X.StepFailed) as e:
        X.start_server(args, world.scope("<mas>/run/"), world.ctx)
    assert e.value.code == "already_running"  # a second server is not started over the first
    undone = X.undo_start_server(args, world.scope("<mas>/run/"), world.ctx)[0]
    assert undone["stopped"] is True and undone["left"] == [] and not procs.is_ours(pid["pid"], pid["created"]) and not (world.home / "run").exists()
    with pytest.raises(X.StepFailed):
        X.minimal_call({"host": "127.0.0.1", "port": args["port"], "prompt": "hi", "max_tokens": 4}, world.scope(), world.ctx)  # nothing answers any more


@pytest.mark.skipif(not POSIX, reason="the test program is a POSIX script")
def test_a_server_that_stops_by_itself_or_never_answers_ends_the_step_with_a_typed_reason_and_leaves_no_process(world):
    with pytest.raises(X.StepFailed) as e:
        run_server(world, "exit")
    assert e.value.code == "server_exited"
    world.ctx["ready_timeout_s"] = 1.5
    X.undo_start_server({}, world.scope("<mas>/run/"), world.ctx)
    with pytest.raises(X.StepFailed) as e:
        run_server(world, "hang")
    assert e.value.code == "server_not_ready"
    d = json.loads((world.home / "run" / "server.json").read_text())
    assert not procs.is_ours(d["pid"], d["created"])  # it was stopped, not left running


@pytest.mark.skipif(not POSIX, reason="the test program is a POSIX script")
def test_a_server_that_listens_on_more_than_loopback_is_stopped_and_not_used(world):
    if procs.listening(__import__("os").getpid()) is None:
        pytest.skip("this system does not let the process list its own sockets: the run-time loopback check is not measurable here")
    with pytest.raises(X.StepFailed) as e:
        run_server(world, "wide")
    assert e.value.code == "server_not_loopback"
    d = json.loads((world.home / "run" / "server.json").read_text())
    assert not procs.is_ours(d["pid"], d["created"])


def test_a_process_is_ours_only_when_its_id_and_creation_time_both_match():
    me = __import__("os").getpid()
    c = procs.created_at(me)
    assert procs.is_ours(me, c) and not procs.is_ours(me, c + 3600) and not procs.is_ours(2 ** 22 + 12345, c)
    assert procs.stop(me, c + 3600) == {"was_running": False, "stopped": True}  # a reused id is not stopped
