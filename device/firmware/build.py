"""Build the key firmware for the host (SIMULATED: HAL mocks, flash in a file, TRNG from a file). One command:

    python device/firmware/build.py [--out DIR] [--src DIR] [--cflags "..."]

The C compiler is `zig cc` (pinned ziglang 0.13.0 from PyPI) found through MAS_CC, or the toolchain venv in runs/toolchain, or `zig` on PATH.
Outputs are cached under runs/fw-build/<hash of sources and flags>/. Prints the path of the built binary on stdout.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CORE = ["core/mas_core.c", "core/mas_frame.c", "core/mas_nv.c", "core/sha256.c"]
HOST = ["host/hal_host.c", "host/main_host.c"]
VENDOR = ["vendor/monocypher/monocypher.c", "vendor/monocypher/monocypher-ed25519.c"]
CORE_FLAGS = ["-std=c99", "-O2", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter"]
VENDOR_FLAGS = ["-std=c99", "-O2"]
EXE = ".exe" if os.name == "nt" else ""


class BuildError(Exception):
    pass


def compiler() -> list[str]:
    env = os.environ.get("MAS_CC")
    if env:
        return shlex.split(env)
    venv_py = ROOT / "runs" / "toolchain" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if venv_py.exists():
        return [str(venv_py), "-m", "ziglang", "cc"]
    if shutil.which("zig"):
        return ["zig", "cc"]
    raise BuildError("no C compiler: install ziglang==0.13.0 into runs/toolchain (see device/firmware/README.md) or set MAS_CC")


def build(src: Path = HERE, out: Path | None = None, extra: list[str] | None = None, name: str = "keyfw_host") -> Path:
    src = Path(src)
    extra = list(extra or [])
    h = hashlib.sha256()
    files = sorted(p for p in src.rglob("*") if p.is_file() and p.suffix in (".c", ".h"))
    for p in files:
        h.update(p.relative_to(src).as_posix().encode() + b"\0" + p.read_bytes())
    h.update(" ".join(extra).encode())
    out = Path(out) if out else ROOT / "runs" / "fw-build" / h.hexdigest()[:16]
    exe = out / (name + EXE)
    if exe.exists():
        return exe
    out.mkdir(parents=True, exist_ok=True)
    cc = compiler()
    inc = ["-I", str(src / "core"), "-I", str(src / "vendor" / "monocypher")]
    objs = []
    for rel, flags in [(r, CORE_FLAGS) for r in CORE + HOST] + [(r, VENDOR_FLAGS) for r in VENDOR]:
        obj = out / (rel.replace("/", "_") + ".o")
        r = subprocess.run(cc + flags + extra + inc + ["-c", str(src / rel), "-o", str(obj)], capture_output=True, text=True)
        if r.returncode != 0:
            raise BuildError(f"ERROR compiling {rel}:\n{r.stderr[-3000:]}")
        objs.append(str(obj))
    r = subprocess.run(cc + extra + objs + ["-o", str(exe)], capture_output=True, text=True)
    if r.returncode != 0:
        raise BuildError(f"ERROR linking:\n{r.stderr[-3000:]}")
    return exe


def build_tool(c_file: Path, out: Path, name: str, extra_sources: tuple = ()) -> Path:
    """A small host-only test program (vectors, smoke) linked with the core helpers it needs."""
    out.mkdir(parents=True, exist_ok=True)
    exe = out / (name + EXE)
    if exe.exists():
        return exe
    cc = compiler()
    srcs = [str(c_file), str(HERE / "core" / "mas_frame.c"), str(HERE / "core" / "sha256.c")] + [str(HERE / v) for v in VENDOR]
    srcs += [str(HERE / s) for s in extra_sources]
    r = subprocess.run(cc + VENDOR_FLAGS + ["-I", str(HERE / "core"), "-I", str(HERE / "vendor" / "monocypher")] + srcs + ["-o", str(exe)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise BuildError(f"ERROR building {c_file.name}:\n{r.stderr[-3000:]}")
    return exe


AN505 = ["an505/startup.c", "an505/hal_an505.c", "an505/main_an505.c"]
ARM = ["-target", "thumb-freestanding-eabi", "-mcpu=cortex_m33", "-ffreestanding", "-fno-builtin"]


def build_an505(src: Path = HERE, out: Path | None = None) -> Path:
    """The same core for QEMU's mps2-an505 (Cortex-M33), EMULATED. Returns the ELF path."""
    src = Path(src)
    h = hashlib.sha256(b"an505")
    for p in sorted(q for q in src.rglob("*") if q.is_file() and q.suffix in (".c", ".h", ".ld")):
        h.update(p.relative_to(src).as_posix().encode() + b"\0" + p.read_bytes())
    out = Path(out) if out else ROOT / "runs" / "fw-build" / ("an505-" + h.hexdigest()[:16])
    elf = out / "keyfw_an505.elf"
    if elf.exists():
        return elf
    out.mkdir(parents=True, exist_ok=True)
    cc = compiler()
    inc = ["-I", str(src / "an505" / "include"), "-I", str(src / "core"), "-I", str(src / "vendor" / "monocypher"), "-I", str(src / "an505")]
    objs = []
    for rel, flags in [(r, CORE_FLAGS) for r in CORE + AN505] + [(r, VENDOR_FLAGS) for r in VENDOR]:
        obj = out / (rel.replace("/", "_") + ".o")
        r = subprocess.run(cc + ARM + flags + inc + ["-c", str(src / rel), "-o", str(obj)], capture_output=True, text=True)
        if r.returncode != 0:
            raise BuildError(f"ERROR compiling {rel} for an505:\n{r.stderr[-3000:]}")
        objs.append(str(obj))
    r = subprocess.run(cc + ARM + ["-nostdlib", "-T", str(src / "an505" / "link.ld")] + objs + ["-o", str(elf)], capture_output=True, text=True)
    if r.returncode != 0:
        raise BuildError(f"ERROR linking an505:\n{r.stderr[-3000:]}")
    return elf


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--src", default=str(HERE))
    ap.add_argument("--cflags", default="")
    a = ap.parse_args(argv)
    try:
        print(build(Path(a.src), Path(a.out) if a.out else None, shlex.split(a.cflags)))
    except BuildError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
