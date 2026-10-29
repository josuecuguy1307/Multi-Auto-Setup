#!/usr/bin/env python3
"""A tiny fake of llama-server's HTTP API, for unit tests that need no model and no real binary.

Same flags as the real server uses (-m, --host, --port, -c, -t, --parallel). Behaviour is chosen with FAKE_MODE:
  ok (default) | crash_after=<seconds> | never_healthy | exit_now | leak=<MiB> | bind_all | canned | wrong_model
  | empty | mirror | bad_usage | no_usage
"""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer


def arg(name, default=None):
    a = sys.argv
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default


mode = os.environ.get("FAKE_MODE", "ok")
model = arg("-m", "")
host, port = arg("--host", "127.0.0.1"), int(arg("--port", "0"))
model_id = "some-other-model.gguf" if mode == "wrong_model" else os.path.basename(model)

if mode == "exit_now":
    print("fatal: cannot load model", file=sys.stderr)
    sys.exit(3)
if mode.startswith("leak="):
    _hog = bytearray(int(mode.split("=")[1]) * 1024 * 1024)
    for i in range(0, len(_hog), 4096):
        _hog[i] = 1
if mode.startswith("crash_after="):
    threading.Timer(float(mode.split("=")[1]), lambda: os._exit(7)).start()
if mode == "bind_all":
    host = "0.0.0.0"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok"}) if mode != "never_healthy" else self._send(503, {"status": "loading"})
        if self.path == "/v1/models":
            return self._send(200, {"data": [{"id": model_id, "object": "model"}]})
        if self.path == "/props":
            return self._send(200, {"model_path": model, "total_slots": 1})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            return self._send(404, {"error": "not found"})
        n = int(self.headers.get("content-length", "0"))
        msgs = json.loads(self.rfile.read(n))["messages"]
        prompt = msgs[-1]["content"]
        if mode == "canned":
            text = "Hello! I am running fine."
        elif mode == "empty":
            text = ""
        elif mode == "mirror":
            text = "[echo] " + prompt
        elif "Repeat this token exactly: " in prompt:
            text = prompt.split("Repeat this token exactly: ", 1)[1].strip()
        else:
            text = "Here is a short answer."
        toks = max(1, len(text.split()))
        body = {"model": model_id, "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": max(1, len(prompt.split())), "completion_tokens": toks, "total_tokens": toks + 1},
                "timings": {"predicted_per_second": 50.0, "predicted_n": toks}}
        if mode == "bad_usage":
            body["usage"]["completion_tokens"] = 5000
        if mode == "no_usage":
            del body["usage"], body["timings"]
        self._send(200, body)


HTTPServer((host, port), H).serve_forever()
