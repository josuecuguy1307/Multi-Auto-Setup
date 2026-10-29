"""R1: the worker's runtime supervisor, against a fake server that mimics llama-server's HTTP API."""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from envrec_worker.runtime import RuntimeFailed, RuntimeSpec, SpecError, Supervisor, build_command

FAKE = str(Path(__file__).resolve().parent / "fakes" / "fake_llama_server.py")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def model(tmp_path):
    m = tmp_path / "tiny.gguf"
    m.write_bytes(b"x" * 1024)
    return str(m)


def spec(model_path, **kw):
    base = dict(kind="llama.cpp", binary=FAKE, model=model_path, port=free_port(), threads=2, context=512, startup_timeout_s=15)
    return RuntimeSpec(**{**base, **kw})


@pytest.fixture
def run(model, tmp_path, monkeypatch):
    started = []

    def make(mode="ok", **kw):
        monkeypatch.setenv("FAKE_MODE", mode)
        sup = Supervisor(spec(model, **kw), log_path=str(tmp_path / "server.log"))
        started.append(sup)
        return sup
    yield make
    for s in started:
        s.stop()


def port_open(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
        return True
    except OSError:
        return False


def test_the_command_is_an_argv_with_the_caps_and_always_localhost(model):
    argv = build_command(spec(model, threads=3, context=1024))
    assert argv[0] == FAKE and argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("-t") + 1] == "3" and argv[argv.index("-c") + 1] == "1024" and "-m" in argv
    assert all(isinstance(a, str) for a in argv) and not any(";" in a or "|" in a for a in argv)


@pytest.mark.parametrize("bad", [dict(host="0.0.0.0"), dict(host="192.168.1.5"), dict(port=80), dict(threads=0), dict(context=10),
                                 dict(kind="mystery-runtime"), dict(binary="relative/llama-server"), dict(model="model.gguf")])
def test_unsafe_or_unverified_specs_are_refused_before_anything_starts(model, bad):
    with pytest.raises(SpecError):
        build_command(spec(model, **bad))


def test_start_waits_for_health_then_status_stop_and_the_port_closes(run):
    sup = run()
    st = sup.start()
    assert st.state == "running" and st.pid and sup.healthy()
    assert sup.status().state == "running" and sup.status().rss_mib
    port = sup.spec.port
    stopped = sup.stop()
    assert stopped.state == "stopped" and not port_open(port)
    assert sup.proc.poll() == -15  # ended by SIGTERM: no process left behind


def test_the_server_listens_on_127_0_0_1_only(run):
    sup = run()
    sup.start()
    listeners = [c.laddr for c in psutil.Process(sup.proc.pid).net_connections(kind="tcp") if c.status == "LISTEN"]
    assert listeners and all(a.ip == "127.0.0.1" and a.port == sup.spec.port for a in listeners)


def test_a_server_that_binds_all_interfaces_is_caught_by_the_listener_check(run):
    sup = run("bind_all")
    sup.spec = sup.spec  # the supervisor asks for 127.0.0.1; the fake ignores it, as a misbehaving binary could
    sup.start()
    listeners = [c.laddr.ip for c in psutil.Process(sup.proc.pid).net_connections(kind="tcp") if c.status == "LISTEN"]
    assert "0.0.0.0" in listeners or "::" in listeners  # this is what the live verifier must catch (R4)


def test_a_crash_is_detected_and_never_reported_as_running_or_stopped(run):
    sup = run("crash_after=1.5")
    assert sup.start().state == "running"
    time.sleep(2.5)
    st = sup.status()
    assert st.state == "crashed" and st.exit_code == 7 and "code 7" in st.reason
    assert not sup.healthy()


def test_a_server_that_dies_during_startup_fails_with_its_own_message(run):
    sup = run("exit_now")
    with pytest.raises(RuntimeFailed, match="exited during startup with code 3.*cannot load model"):
        sup.start()


def test_a_server_that_never_becomes_healthy_is_killed_and_reported(run):
    sup = run("never_healthy", startup_timeout_s=2)
    with pytest.raises(RuntimeFailed, match="did not become healthy"):
        sup.start()
    assert sup.proc.poll() == -15  # killed by SIGTERM when it never became healthy


def test_a_missing_binary_is_a_clean_failure(model, tmp_path):
    sup = Supervisor(spec(model, binary=str(tmp_path / "no-such-binary")))
    with pytest.raises(RuntimeFailed, match="cannot start"):
        sup.start()


def test_the_memory_cap_stops_the_server_on_its_own(run):
    sup = run("leak=200", max_rss_mib=100)
    sup.start()
    reason = sup.check_caps()
    assert "memory" in reason and "100 MiB cap" in reason
    st = sup.status()
    assert st.state == "stopped_by_watchdog" and sup.proc.poll() is not None


def test_the_free_disk_floor_stops_the_server_on_its_own(run):
    sup = run(min_free_disk_mb=10**9)  # an absurd floor: free disk is always below it
    sup.start()
    assert "free disk" in sup.check_caps()
    assert sup.status().state == "stopped_by_watchdog"


def test_within_the_caps_the_watchdog_leaves_it_alone(run):
    sup = run(max_rss_mib=4000, min_free_disk_mb=1)
    sup.start()
    assert sup.check_caps() == "" and sup.status().state == "running"


# ---- through the worker's own command line, as the controller will call it -----------------------------------

def worker(*args, env=None):
    root = Path(__file__).resolve().parent.parent / "src"
    e = {**os.environ, "PYTHONPATH": str(root), **(env or {})}
    return subprocess.run([sys.executable, "-m", "envrec_worker", *args], capture_output=True, text=True, env=e, timeout=60)


def test_cli_start_status_stop_cycle(tmp_path, model):
    root = tmp_path / "w"
    root.mkdir()
    port = free_port()
    args = ["runtime", "start", "--root", str(root), "--kind", "llama.cpp", "--binary", FAKE, "--model", model,
            "--port", str(port), "--threads", "1", "--context", "256", "--startup-timeout", "20"]
    proc = subprocess.Popen([sys.executable, "-m", "envrec_worker", *args], env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")})
    try:
        for _ in range(100):
            time.sleep(0.2)
            st = json.loads(worker("runtime", "status", "--root", str(root)).stdout)
            if st.get("state") == "running":
                break
        assert st["state"] == "running" and port_open(port)
        assert "stopped" in worker("runtime", "stop", "--root", str(root)).stdout
        proc.wait(timeout=15)
        assert proc.returncode == 0 and not port_open(port)
        assert json.loads(worker("runtime", "status", "--root", str(root)).stdout)["state"] == "stopped"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_cli_refuses_a_non_localhost_host_and_an_unverified_kind(tmp_path, model):
    base = ["runtime", "start", "--root", str(tmp_path), "--binary", FAKE, "--model", model, "--port", "8123", "--threads", "1", "--context", "256"]
    r = worker(*base, "--kind", "llama.cpp", "--host", "0.0.0.0")
    assert r.returncode == 2 and "127.0.0.1" in r.stderr
    r = worker(*base, "--kind", "mistral.rs")
    assert r.returncode == 2 and "no verified flag set" in r.stderr


def test_cli_status_notices_a_dead_supervisor(tmp_path):
    (tmp_path / "runtime.status.json").write_text(json.dumps({"state": "running", "pid": 999999}))
    (tmp_path / "server.pid").write_text("999999")
    assert json.loads(worker("runtime", "status", "--root", str(tmp_path)).stdout)["state"] == "crashed"
