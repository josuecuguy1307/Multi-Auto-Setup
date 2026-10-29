#!/usr/bin/env python3
"""The OLD worker stub, kept only as a test fixture: not a language model, it echoes the request.

It answers /health and POST /v1/chat/completions with "[echo] <prompt>". With --claim-model <file> it also answers
/v1/models with that file name, to prove the verifier does not stop at identity.
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

a = sys.argv
port = int(a[a.index("--port") + 1])
claim = a[a.index("--claim-model") + 1] if "--claim-model" in a else None


class H(BaseHTTPRequestHandler):
    def log_message(self, *x):
        pass

    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        if self.path == "/v1/models" and claim:
            return self._send(200, {"data": [{"id": claim}]})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("content-length", "0"))
        prompt = json.loads(self.rfile.read(n))["messages"][-1]["content"]
        text = "[echo] " + prompt
        toks = len(text.split())
        self._send(200, {"choices": [{"message": {"role": "assistant", "content": text}}],
                         "usage": {"completion_tokens": toks}})


HTTPServer(("127.0.0.1", port), H).serve_forever()
