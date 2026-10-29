"""Build the worker release: a reproducible .pyz, its sha256, and stub model files, plus release.json.

Run by the human or the controller's build script. Never run by the model, never given model-written code.
Usage: python build/worker/build.py [--out DIR] [--write-release]
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from envrec_worker import __version__  # noqa: E402

os.environ["TZ"] = "UTC"  # zip stores local time; pin it so the bytes do not depend on the builder's timezone
time.tzset()
FIXED_MTIME = 315619200  # 1980-01-02 UTC: just after the zip epoch, makes the archive byte-for-byte reproducible
STUB_SIZES_B = [0.5, 1.0, 1.5, 3.0, 7.0, 13.0]  # stub model files for the test runtime, one per catalog size


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stub_model_bytes(params_b: float) -> bytes:
    """Deterministic placeholder weights: 4 KiB per billion parameters. NOT a language model."""
    unit = f"envrec stub weights {params_b}b\n".encode()
    return (unit * (4096 * max(1, int(params_b * 2)) // len(unit) + 1))[: 4096 * max(1, int(params_b * 2))]


def build(out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "stage"
        shutil.copytree(ROOT / "src" / "envrec_worker", stage / "envrec_worker", ignore=shutil.ignore_patterns("__pycache__"))
        # zipapp would generate this entry with the CURRENT time; writing it ourselves keeps the bytes reproducible
        (stage / "__main__.py").write_text("import sys\nimport envrec_worker.__main__\nsys.exit(envrec_worker.__main__.main())\n")
        for p in [*stage.rglob("*")]:
            os.utime(p, (FIXED_MTIME, FIXED_MTIME))
        pyz = out / f"envrec-worker-{__version__}.pyz"
        zipapp.create_archive(stage, pyz)
    models = {}
    (out / "models").mkdir(exist_ok=True)
    for p in STUB_SIZES_B:
        name = f"stub-{p:g}b"
        data = stub_model_bytes(p)
        (out / "models" / name).write_bytes(data)
        models[name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "params_b": p}
    (out / f"envrec-worker-{__version__}.pyz.sha256").write_text(f"{sha256_file(pyz)}  {pyz.name}\n")
    return {"version": __version__, "filename": pyz.name, "sha256": sha256_file(pyz), "bytes": pyz.stat().st_size, "models": models}


if __name__ == "__main__":
    out = Path(sys.argv[sys.argv.index("--out") + 1]) if "--out" in sys.argv else ROOT / "build" / "worker" / "dist"
    release = build(out)
    if "--write-release" in sys.argv:
        (ROOT / "build" / "worker" / "release.json").write_text(json.dumps(release, indent=2) + "\n")
    print(json.dumps({k: v for k, v in release.items() if k != "models"}, indent=2))
