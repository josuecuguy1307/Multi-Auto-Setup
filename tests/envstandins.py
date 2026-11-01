"""Stand-in executables that mimic the documented non-interactive interface of each environment CLI (SIMULATED-LOCAL). They prove our adapters, never the vendors' tools. Each stand-in refuses (exit 2)
an argument list that is not the documented shape of that tool, records what it was given, and behaves as `mode` says: ok | escape (writes outside its workspace) | symlink | fail."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAMES = {"claude": "claude-code", "codex": "codex", "muse": "muse-code", "opencode": "opencode", "pi": "pi"}


def main(kind: str, logfile: str) -> None:
    argv = sys.argv[1:]
    stdin = "" if sys.stdin.isatty() else sys.stdin.read()
    mode = Path(logfile + ".mode").read_text().strip() if Path(logfile + ".mode").exists() else "ok"
    rec = {"kind": kind, "argv": argv, "stdin": stdin, "cwd": os.getcwd(), "mode": mode, "env": {k: v for k, v in os.environ.items() if k in ("OPENCODE_PERMISSION", "OPENCODE_CONFIG_CONTENT", "HOME", "META_API_KEY", "SECRET_TOKEN", "PI_OFFLINE")}}
    with open(logfile, "a") as f:
        f.write(json.dumps(rec) + "\n")
    ok = {"claude": "-p" in argv and "--output-format" in argv and "--permission-mode" in argv, "codex": argv[:1] == ["exec"] and argv[-1:] == ["-"] and "-s" in argv and "-C" in argv and "-o" in argv,
          "muse": argv[:1] == ["exec"] and len(argv) == 2, "opencode": argv[:1] == ["run"] and "--dir" in argv and "--model" in argv, "pi": "-p" in argv and "--no-session" in argv and "--model" in argv}[kind]
    if not ok:
        sys.stderr.write("unexpected arguments for the documented interface\n")
        sys.exit(2)
    if mode == "fail":
        sys.stderr.write("boom\n")
        sys.exit(3)
    if mode == "escape":
        Path("..", "escape.txt").write_text("outside")
    if mode == "symlink":
        os.symlink("/etc/hosts", "leak.txt")
    if mode in ("ok", "write"):
        Path("result.py").write_text("print('hi')\n")
    answer = f"answer from {kind}"
    if kind == "claude":
        print(json.dumps({"type": "result", "result": answer}))
    elif kind == "codex":
        Path(argv[argv.index("-o") + 1]).write_text(answer)
    else:
        print(answer)


def install(bin_dir: Path, log_dir: Path) -> dict[str, str]:
    """Write one executable per tool name into bin_dir. Returns {executable name: path}."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    for name in NAMES:
        p = bin_dir / name
        p.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(HERE)!r})\nimport envstandins\nenvstandins.main({name!r}, {str(log_dir / (name + '.log'))!r})\n")
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
        out[name] = str(p)
    return out


def set_mode(log_dir: Path, name: str, mode: str) -> None:
    (log_dir / (name + ".log.mode")).write_text(mode)


def calls(log_dir: Path, name: str) -> list[dict]:
    f = log_dir / (name + ".log")
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
