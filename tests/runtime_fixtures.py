"""Fake runtime and model artifacts for tests that must not need a real model or a download."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

from envrec.remote.provision import DeployPlan
from envrec.runtime.manifest import Artifact

FAKES = Path(__file__).resolve().parent / "fakes"


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def make_runtime_archive(dest: Path, script: Path | None = None, name: str = "fake-llama.tar.gz", inner: str = "llama-server") -> Path:
    """A tar.gz holding the fake server as `llama-server` (mode 755) inside a top folder, like real release archives."""
    script = script or FAKES / "fake_llama_server.py"
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / name
    with tarfile.open(path, "w:gz") as tar:
        data = script.read_bytes()
        info = tarfile.TarInfo(f"fake-build/{inner}")
        info.size, info.mode, info.mtime = len(data), 0o755, 0
        tar.addfile(info, io.BytesIO(data))
    return path


def make_model(dest: Path, name: str = "tiny-test.gguf", size: int = 64 * 1024) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / name
    path.write_bytes((b"GGUF-test-weights-" * (size // 18 + 1))[:size])
    return path


def artifact(path: Path, kind: str, art_id: str | None = None, **meta) -> Artifact:
    data = path.read_bytes()
    return Artifact(art_id or path.stem, kind, path.name, f"file://{path}", _sha(data), len(data), "apache-2.0", "", "test", meta)


def fake_plan(dest: Path, *, script: Path | None = None, model_name: str = "tiny-test.gguf", model_size: int = 64 * 1024,
              kind: str = "llama.cpp", binary_name: str = "llama-server", **caps) -> DeployPlan:  # the fake mimics that runtime's flags
    rt, md = make_runtime_archive(dest, script), make_model(dest, model_name, model_size)
    return DeployPlan(runtime=artifact(rt, "runtime", "fake-runtime"), runtime_path=rt,
                      model=artifact(md, "model", "fake-model"), model_path=md, kind=kind, binary_name=binary_name, **caps)
