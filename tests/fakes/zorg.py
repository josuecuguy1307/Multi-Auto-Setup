"""An invented operating system, "zorg", for testing the unknown ladder end to end without any real computer: a few made-up commands over an in-memory file system, and a model server that
speaks just enough of the llama-server HTTP API. It exists so that no test of the generic machinery depends on the knowledge of a real operating system."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NOT_RECOGNIZED = "'{}' is not recognized as an internal or external command.\n"


class Zorg:
    """The computer. `ssh_exec` is a RemoteRunner exec_fn: it receives the ssh argv and answers by parsing the remote command string."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = set()
        self.running = False
        self.log: list[str] = []
        self.hang_on: str | None = None

    def ssh_exec(self, argv, stdin, timeout):
        remote = argv[-1]
        self.log.append(remote)
        words = [w.strip('"') for w in re.findall(r'"[^"]*"|\S+', remote)]
        prog, args = words[0], words[1:]
        if self.hang_on and prog == self.hang_on:
            return 124, b"", b"timed out"
        data = stdin.read_bytes() if isinstance(stdin, Path) else stdin
        return getattr(self, f"cmd_{prog}", self.unknown)(prog, args, data)

    def unknown(self, prog, args, data):
        return 1, b"", NOT_RECOGNIZED.format(prog).encode()

    def cmd_echo(self, prog, args, data):
        return 0, (" ".join(args) + "\n").encode(), b""

    def cmd_zver(self, prog, args, data):
        return 0, b"ZORG 4.2 x64\n", b""

    def cmd_zdisk(self, prog, args, data):
        return 0, b"free 40\n", b""

    def cmd_zram(self, prog, args, data):
        return 0, b"ram 16\n", b""

    def cmd_zmk(self, prog, args, data):
        self.dirs.add(args[0])
        return 0, b"", b""

    def cmd_zrm(self, prog, args, data):
        p = args[0]
        self.files = {k: v for k, v in self.files.items() if k != p and not k.startswith(p + "\\")}
        self.dirs = {d for d in self.dirs if d != p and not d.startswith(p + "\\")}
        return 0, b"", b""

    def cmd_ztee(self, prog, args, data):
        self.files[args[0]] = data or b""
        return 0, b"", b""

    def cmd_zsum(self, prog, args, data):
        f = self.files.get(args[0])
        return (0, (hashlib.sha256(f).hexdigest() + "  " + args[0] + "\n").encode(), b"") if f is not None else (1, b"", b"not found")

    def cmd_zrun(self, prog, args, data):
        self.running = args[0] in self.files
        return (0, b"started\n", b"") if self.running else (1, b"", b"no such program")

    def cmd_zkill(self, prog, args, data):
        self.running = False
        return 0, b"", b""

    def cmd_zls(self, prog, args, data):
        listing = sorted(k for k in [*self.files, *self.dirs] if k == args[0] or k.startswith(args[0] + "\\"))
        return (0, ("\n".join(listing) + "\n").encode(), b"") if listing else (1, b"", b"not found")

    def cmd_znet(self, prog, args, data):
        return (0, f"127.0.0.1:{args[0]}\n".encode(), b"") if self.running else (0, b"", b"")

    def cmd_zmem(self, prog, args, data):
        return (0, b"512\n", b"") if self.running else (1, b"", b"not running")

    def cmd_zhealth(self, prog, args, data):
        return (0, b'{"status":"ok"}\n', b"") if self.running else (1, b"", b"")


class FakeModelServer:
    """Answers like llama-server for the checks the verifier makes. Started only while the Zorg computer says its server runs."""

    def __init__(self, computer: Zorg, model_file: str = "toy-model.gguf") -> None:
        self.computer, self.model_file, self.httpd = computer, model_file, None

    def open(self, port: int, ask):
        zorg, model_file = self.computer, self.model_file
        if not zorg.running:
            raise ConnectionError("nothing answers on the computer's loopback port")

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, body):
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                self._send({"data": [{"id": model_file}]} if self.path == "/v1/models" else {"model_path": "Z:\\envrec\\ws\\" + model_file})

            def do_POST(self):  # noqa: N802
                msg = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["messages"][-1]["content"]
                m = re.search(r"Repeat this token exactly: (\S+)", msg)
                text = f"Token: {m.group(1)}" if m else "5" if "boxes" in msg else "The sea is calm today."
                self._send({"choices": [{"message": {"content": text}}], "usage": {"completion_tokens": 6}, "timings": {"predicted_per_second": 11.5}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self.httpd.server_address[1], self.close

    def close(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
