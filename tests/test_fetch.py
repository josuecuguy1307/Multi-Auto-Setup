"""R2: controller-side fetch from the approved manifest only, with sha256 verification, caching and a disk reserve."""

import hashlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from envrec.remote.approval import ApprovalGate
from envrec.runtime import (
    Artifact,
    ArtifactNotInManifest,
    ChecksumMismatch,
    DiskReserveBreach,
    FetchDenied,
    Fetcher,
    Manifest,
    NotApproved,
)
from envrec.store import Store

ROOT = Path(__file__).resolve().parent.parent
GIB = 2**30


class Origin:
    """A local HTTP server standing in for the internet: serves chosen bytes per path, counts requests."""

    def __init__(self, files: dict[str, bytes], truncate: dict[str, int] | None = None):
        self.files, self.truncate, self.hits = files, truncate or {}, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.hits.append(self.path)
                data = outer.files.get(self.path)
                if data is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                cut = outer.truncate.get(self.path)
                self.wfile.write(data[:cut] if cut else data)
                if cut:
                    self.wfile.flush()
                    self.connection.close()

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def url(self, path):
        return f"http://127.0.0.1:{self.srv.server_port}{path}"

    def stop(self):
        self.srv.shutdown()


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "s") as s:
        yield s


def art(origin, path, data, **kw):
    return Artifact(kw.pop("id", path.strip("/")), "model", path.strip("/"), origin.url(path), hashlib.sha256(data).hexdigest(),
                    len(data), "apache-2.0", "", "test")


def make(store, tmp_path, registry, answers=("approve",), free=100 * GIB, reserve=4.0):
    shown, it = [], iter(answers)
    gate = ApprovalGate(store, lambda p: next(it, "deny"), shown.append)
    f = Fetcher(registry, tmp_path / "cache", gate, reserve_gib=reserve, free_fn=lambda p: free if isinstance(free, int) else free(p))
    return f, shown


DATA = b"model-bytes-" * 5000


def test_a_fetch_asks_with_url_size_and_checksum_then_caches_a_verified_file(store, tmp_path):
    o = Origin({"/m.gguf": DATA})
    a = art(o, "/m.gguf", DATA)
    f, shown = make(store, tmp_path, {a.id: a})
    path = f.fetch(a.id)
    o.stop()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == a.sha256 and path.parent.name == a.sha256
    prompt = shown[0]
    assert o.url("/m.gguf") in prompt and a.sha256 in prompt and "MB" in prompt and "recipe:  fetch_artifact" in prompt
    assert store.events(type="approval")[0].payload["checksum"] == a.sha256
    assert not list((tmp_path / "cache").rglob("*.partial"))


def test_a_cache_hit_needs_no_network_and_no_approval(store, tmp_path):
    o = Origin({"/m.gguf": DATA})
    a = art(o, "/m.gguf", DATA)
    f, shown = make(store, tmp_path, {a.id: a}, answers=("approve",))
    p1 = f.fetch(a.id)
    p2 = f.fetch(a.id)
    o.stop()
    assert p1 == p2 and f.network_calls == 1 and len(o.hits) == 1 and len(shown) == 1


def test_a_corrupted_cache_entry_is_not_trusted_and_is_fetched_again(store, tmp_path):
    o = Origin({"/m.gguf": DATA})
    a = art(o, "/m.gguf", DATA)
    f, _ = make(store, tmp_path, {a.id: a}, answers=("approve", "approve"))
    p = f.fetch(a.id)
    p.write_bytes(b"corrupted")
    again = f.fetch(a.id)
    o.stop()
    assert again.read_bytes() == DATA and f.network_calls == 2


def test_nothing_outside_the_manifest_is_ever_fetched(store, tmp_path):
    o = Origin({"/m.gguf": DATA, "/evil.bin": b"x" * 100})
    a = art(o, "/m.gguf", DATA)
    f, shown = make(store, tmp_path, {a.id: a})
    for wanted in ("evil.bin", o.url("/evil.bin"), "also install llama-xyz", "../../etc/passwd"):  # a model or the mission text asking
        with pytest.raises(ArtifactNotInManifest):
            f.fetch(wanted)
    o.stop()
    assert o.hits == [] and shown == [] and f.network_calls == 0
    assert not hasattr(f, "fetch_url")  # there is no fetch-by-URL entry point to misuse


def test_a_checksum_mismatch_aborts_and_leaves_nothing_in_the_cache(store, tmp_path):
    tampered = DATA[:-1] + b"X"
    o = Origin({"/m.gguf": tampered})
    a = art(o, "/m.gguf", DATA)  # the manifest has the hash of the good bytes
    f, _ = make(store, tmp_path, {a.id: a})
    with pytest.raises(ChecksumMismatch, match="does not match"):
        f.fetch(a.id)
    o.stop()
    assert [p for p in (tmp_path / "cache").rglob("*") if p.is_file()] == []


def test_a_download_larger_than_declared_is_aborted(store, tmp_path):
    big = b"y" * (10 * 1024 * 1024)
    o = Origin({"/m.gguf": big})
    a = Artifact("m", "model", "m.gguf", o.url("/m.gguf"), hashlib.sha256(big).hexdigest(), 1000, "apache-2.0", "", "test")  # declared 1 KB
    f, _ = make(store, tmp_path, {a.id: a})
    with pytest.raises(ChecksumMismatch, match="larger than declared"):
        f.fetch("m")
    o.stop()
    assert [p for p in (tmp_path / "cache").rglob("*") if p.is_file()] == []


def test_a_truncated_download_never_survives_as_a_cached_file(store, tmp_path):
    o = Origin({"/m.gguf": DATA}, truncate={"/m.gguf": len(DATA) // 2})
    a = art(o, "/m.gguf", DATA)
    f, _ = make(store, tmp_path, {a.id: a})
    with pytest.raises(Exception):  # httpx reports the short body, or the hash will not match
        f.fetch(a.id)
    o.stop()
    assert [p for p in (tmp_path / "cache").rglob("*") if p.is_file()] == []


def test_a_download_that_would_breach_the_disk_reserve_is_refused_before_it_starts(store, tmp_path):
    o = Origin({"/m.gguf": DATA})
    a = Artifact("m", "model", "m.gguf", o.url("/m.gguf"), hashlib.sha256(DATA).hexdigest(), 2 * GIB, "apache-2.0", "", "test")
    f, shown = make(store, tmp_path, {"m": a}, free=5 * GIB, reserve=4.0)  # 5 GiB free, 2 GiB wanted, 4 GiB reserve
    with pytest.raises(DiskReserveBreach, match="below the 4.0 GiB reserve"):
        f.fetch("m")
    o.stop()
    assert o.hits == [] and shown == [] and f.network_calls == 0  # not even the question was asked


def test_exactly_enough_room_is_allowed(store, tmp_path):
    o = Origin({"/m.gguf": DATA})
    a = art(o, "/m.gguf", DATA)
    f, _ = make(store, tmp_path, {a.id: a}, free=4 * GIB + len(DATA), reserve=4.0)
    assert f.fetch(a.id).is_file()
    o.stop()


def test_the_reserve_is_also_enforced_while_the_download_runs(store, tmp_path):
    big = b"z" * (6 * 1024 * 1024)
    o = Origin({"/m.gguf": big})
    a = art(o, "/m.gguf", big)
    state = {"free": 4 * GIB + len(big) + 8 * 1024 * 1024}  # fine at the start, then the disk fills up from elsewhere

    def free(_):
        state["free"] -= 2 * 1024 * 1024
        return state["free"]

    f, _ = make(store, tmp_path, {a.id: a}, free=free)
    with pytest.raises(DiskReserveBreach, match="aborted"):
        f.fetch(a.id)
    o.stop()
    assert [p for p in (tmp_path / "cache").rglob("*") if p.is_file()] == []


def test_a_denied_approval_downloads_nothing(store, tmp_path):
    o = Origin({"/m.gguf": DATA})
    a = art(o, "/m.gguf", DATA)
    f, _ = make(store, tmp_path, {a.id: a}, answers=("no",))
    with pytest.raises(FetchDenied):
        f.fetch(a.id)
    o.stop()
    assert o.hits == [] and f.network_calls == 0


# ---- the real manifest file -----------------------------------------------------------------------------------------

def test_the_real_manifest_parses_and_describes_the_smoke_set():
    m = Manifest.load(ROOT / "config" / "runtime_manifest.yaml")
    arts = m.smoke_artifacts()
    assert set(arts) == {"llama.cpp:b11856:linux/arm64:cpu", "llama.cpp:b11856:linux/amd64:cpu",
                         "qwen2.5-0.5b-q4km", "qwen2.5-1.5b-q4km", "qwen3-4b-q4km"}
    assert all(len(a.sha256) == 64 and a.url.startswith("https://") and a.license for a in arts.values())
    assert m.approved_runtime_ids() == ["llama.cpp", "mistral.rs"] and "qwen" in m.approved_model_source_ids()
    assert "gemma" not in m.license_allowlist and "mit" in m.license_allowlist


def test_an_unapproved_manifest_refuses_to_build_a_fetcher(store, tmp_path):
    m = Manifest.load(ROOT / "config" / "runtime_manifest.yaml")
    m.approved = False
    with pytest.raises(NotApproved):
        Fetcher.from_manifest(m, tmp_path / "c", ApprovalGate(store, lambda p: "approve", lambda t: None))
    with pytest.raises(NotApproved):
        m.require_approved()


def test_only_the_literal_true_counts_as_approval(tmp_path):
    for text, expected in (("approved: true", True), ("approved: false", False), ("approved: 'true'", False), ("approved: yes please", False)):
        f = tmp_path / "m.yaml"
        f.write_text(f"{text}\ndisk_reserve_gib: 4\nruntime_sources: []\nmodel_sources: []\n")
        assert Manifest.load(f).approved is expected, text
