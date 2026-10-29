"""P5a: the worker release. Built ahead of time, pinned, minimal, and unable to deploy, enroll, plan or scan."""

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

from envrec.mission import Transport, TargetConfig

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "build" / "worker"))
import build as worker_build  # noqa: E402

PINNED = json.loads((ROOT / "build" / "worker" / "release.json").read_text())
FORBIDDEN_MODULES = ("envrec/remote", "envrec/enroll", "envrec/inventory", "envrec/mission", "envrec/policy", "envrec/loop",
                     "envrec/verify", "envrec/store", "envrec/adapters", "planner", "provision", "runner", "recipes", "approval")
FORBIDDEN_SYMBOLS = ("Provisioner", "RemoteRunner", "plan_transport", "enroll", "decide_for_fleet", "open_tunnel", "ApprovalGate",
                     "zeroconf", "scapy", "nmap", "ARP", "port scan", "scan_ports", "mdns", "bluetooth", "ssh ", "paramiko", "docker exec")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("w")
    return out, worker_build.build(out)


def pyz(built):
    out, rel = built
    return out / rel["filename"]


def test_the_build_is_reproducible_and_matches_the_pinned_release(built):
    out, rel = built
    assert rel["sha256"] == PINNED["sha256"] == hashlib.sha256(pyz(built).read_bytes()).hexdigest()
    assert (out / f"{rel['filename']}.sha256").read_text().split()[0] == rel["sha256"]
    other = worker_build.build(out.parent / "second")
    assert other["sha256"] == rel["sha256"]


def test_the_artifact_contains_only_the_worker_package(built):
    names = zipfile.ZipFile(pyz(built)).namelist()
    assert "__main__.py" in names and "envrec_worker/runtime/supervisor.py" in names
    assert all(n == "__main__.py" or n.startswith("envrec_worker/") for n in names), names
    for bad in FORBIDDEN_MODULES:
        assert not any(bad in n for n in names), bad


def _code_identifiers(source: str) -> set[str]:
    """Names, attributes, imports, function names and non-docstring string constants: the code, not the prose."""
    import ast
    tree = ast.parse(source)
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body
                  and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant)}
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Import):
            out.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            out.add(n.module or "")
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings:
            out.add(n.value)
    return out


def _calls_with_shell_true(source: str) -> list[int]:
    import ast
    return [n.lineno for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)
            and any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True for k in n.keywords)]


def test_no_deploy_enroll_planner_runner_or_scanning_code_exists_in_the_artifact(built):
    z = zipfile.ZipFile(pyz(built))
    per_file = {n: _code_identifiers(z.read(n).decode()) for n in z.namelist() if n.endswith(".py")}
    blob = " ".join(" ".join(v) for v in per_file.values()).lower()
    for bad in FORBIDDEN_SYMBOLS:
        assert bad.lower() not in blob, f"{bad!r} appears in the worker's code"
    network_libs = {"socket", "ssl", "requests", "httpx", "paramiko", "asyncio"}
    for name, idents in per_file.items():
        used = {i.split(".")[0] for i in idents if i.split(".")[0] in network_libs | {"subprocess", "urllib"}}
        assert not (used & network_libs), (name, used)
        if name != "envrec_worker/runtime/supervisor.py":  # the one file allowed to start a process or talk (to localhost)
            assert not (used & {"subprocess", "urllib"}), (name, used)
        assert not _calls_with_shell_true(z.read(name).decode()), f"{name} uses shell=True"


def test_the_only_process_the_worker_can_start_is_the_configured_server_binary(built):
    """The supervisor builds one argv from a validated spec; no other subprocess call exists in the worker."""
    z = zipfile.ZipFile(pyz(built))
    calls = []
    import ast
    for n in z.namelist():
        if n.endswith(".py"):
            for node in ast.walk(ast.parse(z.read(n).decode())):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) \
                        and node.func.value.id == "subprocess" and node.func.attr in ("Popen", "run", "call", "check_output", "check_call"):
                    calls.append((n, node.func.attr))
    assert sorted(calls) == [("envrec_worker/runtime/supervisor.py", "Popen"), ("envrec_worker/runtime/supervisor.py", "run")]
    src = z.read("envrec_worker/runtime/supervisor.py").decode()
    assert "build_command(self.spec)" in src  # Popen gets the validated argv, nothing else
    assert src.count('["ps", "-o", "rss="') == 1  # the one other call: reading a pid's memory with a fixed ps command


def test_the_built_artifact_cannot_import_the_controller_at_all(built):
    code = ("import sys; sys.path.insert(0, sys.argv[1]);\n"
            "import envrec_worker.__main__ as m\n"
            "for name in ('envrec', 'envrec.remote', 'envrec.enroll', 'envrec.remote.provision'):\n"
            "    try:\n        __import__(name); print('LEAK', name)\n    except ImportError: pass\n"
            "loaded = [k for k in sys.modules if k.startswith('envrec.') ]\n"
            "print('OK', loaded)")
    r = subprocess.run([sys.executable, "-S", "-I", "-c", code, str(pyz(built))], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "OK []", r.stdout + r.stderr


def test_the_worker_refuses_a_non_localhost_bind_and_an_unverified_runtime_kind(built, tmp_path):
    base = [sys.executable, str(pyz(built)), "runtime", "start", "--root", str(tmp_path), "--binary", "/bin/true", "--model", "/tmp/m.gguf",
            "--port", "8123", "--threads", "1", "--context", "256"]
    r = subprocess.run([*base, "--kind", "llama.cpp", "--host", "0.0.0.0"], capture_output=True, text=True, timeout=20)
    assert r.returncode == 2 and "127.0.0.1" in r.stderr
    r = subprocess.run([*base, "--kind", "mystery"], capture_output=True, text=True, timeout=20)
    assert r.returncode == 2 and "no verified flag set" in r.stderr


def test_uninstall_refuses_roots_outside_the_install_folder(built):
    for bad in ("/", "/opt", "/etc", "/home/me", ""):
        r = subprocess.run([sys.executable, str(pyz(built)), "uninstall", "--root", bad], capture_output=True, text=True)
        assert r.returncode == 2


def test_the_workers_own_targets_yaml_has_exactly_one_entry_itself():
    from envrec.mission import DockerExecBlock, EntryLimits, TargetEntry
    from envrec.remote.provision import worker_targets_yaml
    controller_entry = TargetEntry(id="lab-b", name="Lab B", host="192.168.1.42", allowed_transports=[Transport.docker_exec],
                                   docker_exec=DockerExecBlock(container="lab-b"),
                                   limits=EntryLimits(max_disk_gb=20, max_model_params="9B"), owner_confirmed=True)
    cfg = TargetConfig.model_validate(yaml.safe_load(worker_targets_yaml(controller_entry)))
    assert [t.id for t in cfg.targets] == ["lab-b"] and cfg.targets[0].host == "127.0.0.1"
    assert cfg.targets[0].limits == controller_entry.limits  # same ceilings, no more


def test_the_release_pins_only_the_worker_and_no_stub_runtime_remains(built):
    out, rel = built
    assert set(rel) == {"version", "filename", "sha256", "bytes"} and set(PINNED) == set(rel)
    z = zipfile.ZipFile(pyz(built))
    text = "\n".join(z.read(n).decode() for n in z.namelist() if n.endswith(".py")).lower()
    assert "stub-runtime" not in text and "baseHTTPRequestHandler".lower() not in text  # the echo server is gone
