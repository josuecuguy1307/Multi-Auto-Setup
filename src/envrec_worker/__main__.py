"""Worker entry point: probe, serve, uninstall."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from envrec_worker import __version__

LOCALHOST = "127.0.0.1"


def _arg(argv: list[str], name: str, default: str | None = None) -> str | None:
    return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default


def probe() -> dict:
    ram = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                ram = round(int(line.split()[1]) * 1024 / 2**30, 2)
    except OSError:
        pass
    limit = None
    for p in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            t = Path(p).read_text().strip()
            if t.isdigit() and int(t) < 2**50:
                limit = round(int(t) / 2**30, 2)
            break
        except OSError:
            continue
    return {"worker_version": __version__, "arch": platform.machine(), "os": platform.system(),
            "ram_reported_gib": ram, "ram_limit_gib": limit, "disk_free_gb": round(shutil.disk_usage("/").free / 1e9, 2)}


def make_handler(model: str, model_sha: str | None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep stdout/stderr quiet; the controller reads the log file on demand
            pass

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"status": "ok", "worker_version": __version__, "model": model, "model_sha256": model_sha})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/v1/chat/completions":
                return self._send(404, {"error": "not found"})
            try:
                n = int(self.headers.get("content-length", "0"))
                msgs = json.loads(self.rfile.read(n))["messages"]
                last = next(m["content"] for m in reversed(msgs) if m.get("role") == "user")
            except (ValueError, KeyError, StopIteration, TypeError):
                return self._send(400, {"error": "expected {'messages': [... a user message ...]}"})
            # TEST RUNTIME: echoes the request. It is not a language model.
            self._send(200, {"model": model, "choices": [{"message": {"role": "assistant",
                                                                       "content": f"[stub-runtime:{model}] {last}"}}]})
    return Handler


def serve(argv: list[str]) -> int:
    host = _arg(argv, "--host", LOCALHOST)
    if host != LOCALHOST:
        print(f"refusing to bind {host!r}: this worker listens on {LOCALHOST} only", file=sys.stderr)
        return 2
    port, model = int(_arg(argv, "--port", "0")), _arg(argv, "--model", "")
    root = Path(sys.argv[0]).resolve().parent
    mf = root / "models" / (model or "")
    sha = hashlib.sha256(mf.read_bytes()).hexdigest() if model and mf.is_file() else None
    HTTPServer((LOCALHOST, port), make_handler(model or "", sha)).serve_forever()
    return 0


def uninstall(argv: list[str]) -> int:
    root = Path(_arg(argv, "--root", "")).resolve()
    if not str(root).startswith("/opt/envrec") or str(root) in ("/", "/opt"):
        print("refusing: root must be inside /opt/envrec", file=sys.stderr)
        return 2
    pid = root / "server.pid"
    if pid.is_file():
        try:
            os.kill(int(pid.read_text().strip()), 15)
        except (OSError, ValueError):
            pass
    shutil.rmtree(root, ignore_errors=True)
    print("removed")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    if cmd == "probe":
        print(json.dumps(probe()))
        return 0
    if cmd == "serve":
        return serve(argv)
    if cmd == "uninstall":
        return uninstall(argv)
    print("usage: probe | serve --host 127.0.0.1 --port N --model NAME | uninstall --root DIR", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
