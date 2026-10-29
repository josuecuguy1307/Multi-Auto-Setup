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
    assert set(names) == {"envrec_worker/", "envrec_worker/__init__.py", "envrec_worker/__main__.py", "__main__.py"}
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


def test_no_deploy_enroll_planner_runner_or_scanning_code_exists_in_the_artifact(built):
    z = zipfile.ZipFile(pyz(built))
    idents = set()
    for n in z.namelist():
        if n.endswith(".py"):
            idents |= _code_identifiers(z.read(n).decode())
    blob = " ".join(idents).lower()
    for bad in FORBIDDEN_SYMBOLS:
        assert bad.lower() not in blob, f"{bad!r} appears in the worker's code"
    allowed_imports = {"hashlib", "json", "os", "platform", "shutil", "sys", "http.server", "pathlib", "envrec_worker",
                       "__future__", "envrec_worker.__main__"}
    imported = {i for i in idents if i in allowed_imports or i.split(".")[0] in {"socket", "subprocess", "ssl", "urllib", "requests", "httpx"}}
    assert not ({i for i in imported if i.split(".")[0] in {"socket", "subprocess", "ssl", "urllib", "requests", "httpx"}}), imported


def test_the_built_artifact_cannot_import_the_controller_at_all(built):
    code = ("import sys; sys.path.insert(0, sys.argv[1]);\n"
            "import envrec_worker.__main__ as m\n"
            "for name in ('envrec', 'envrec.remote', 'envrec.enroll', 'envrec.remote.provision'):\n"
            "    try:\n        __import__(name); print('LEAK', name)\n    except ImportError: pass\n"
            "loaded = [k for k in sys.modules if k.startswith('envrec.') ]\n"
            "print('OK', loaded)")
    r = subprocess.run([sys.executable, "-S", "-I", "-c", code, str(pyz(built))], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == "OK []", r.stdout + r.stderr


def test_the_worker_binds_localhost_only_and_refuses_anything_else(built):
    r = subprocess.run([sys.executable, str(pyz(built)), "serve", "--host", "0.0.0.0", "--port", "8123", "--model", "x"],
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 2 and "127.0.0.1 only" in r.stderr


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


def test_the_stub_model_files_match_their_pinned_checksums(built):
    out, rel = built
    for name, meta in rel["models"].items():
        assert hashlib.sha256((out / "models" / name).read_bytes()).hexdigest() == meta["sha256"] == PINNED["models"][name]["sha256"]
