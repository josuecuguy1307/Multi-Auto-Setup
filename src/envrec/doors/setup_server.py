"""LEGACY (retired by docs/NETWORK_CONTRACT.md 2.5 and section 1.5; kept until Phase 1 replaces it, NOT reachable from the `mas` commands; conformance and tests/test_mas_legacy.py check that): The one-time setup link: serves the door file ONCE, to a client on the Mac's own private network, under a random token, for five minutes, and takes ONE report back from that file
(who the other computer says it is). The address of the other computer is the address of the connection that came in, never something typed. Every step is reported to `emit` (the caller
writes it as an event). The server stops when the report has arrived or at expiry, whichever comes first."""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN_BYTES = 24  # 192 bits; the requirement is at least 128
TTL_S = 300.0
Emit = Callable[[str, dict], None]


def private_lan_address(address: str) -> ipaddress.IPv4Address:
    """The address to bind: it must be a private LAN address of this machine (never 0.0.0.0, never public, never loopback)."""
    a = ipaddress.ip_address(address)
    if not isinstance(a, ipaddress.IPv4Address) or not (a.is_private and not a.is_loopback and not a.is_unspecified) and not a.is_link_local:
        raise ValueError(f"{address} is not a private LAN address; the setup link is served on the owner's own network only")
    return a


class SetupLink:
    """`start()` returns the URL to open on the other computer. `done` is set when the file was downloaded once or the link expired."""

    def __init__(self, address: str, door_bytes: "bytes | Callable[[str], bytes]", *, ttl_s: float = TTL_S, filename: str = "d.cmd", emit: Emit | None = None, port: int = 0,
                 prefix: int = 24, check: Callable[[str], ipaddress.IPv4Address] = private_lan_address) -> None:
        self.addr = check(address)  # `check` is the scope guard; only a test replaces it (to bind loopback)
        self.net = ipaddress.ip_network(f"{self.addr}/{prefix}", strict=False)  # clients must come from the same /24 as the Mac
        self.token = secrets.token_urlsafe(TOKEN_BYTES)
        self._door_source, self.door, self.ttl_s, self.filename, self.emit, self.port = door_bytes, b"", ttl_s, filename, emit or (lambda kind, info: None), port
        self.done = threading.Event()
        self.served = False
        self.fetched_by: str | None = None  # the address that downloaded the file
        self.report: dict | None = None  # {address, user, computer, os, host_key}: what the file said, with the address of the connection that said it
        self.reported = threading.Event()
        self.steps: list[str] = []  # the labels of the steps the file says it reached, in order (a progress beacon: lets the Mac see how far it got when its window closes)
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._deadline = 0.0
        self.outcome = "not started"

    def start(self) -> str:
        link = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # nothing is printed: the events carry the record
                pass

            def do_GET(self):  # noqa: N802
                client = self.client_address[0]
                try:
                    inside = ipaddress.ip_address(client) in link.net
                except ValueError:
                    inside = False
                if not inside:
                    link.emit("setup_link_refused", {"reason": "client outside the private network", "client": client})
                    return self._send(403, b"refused")
                if time.monotonic() > link._deadline:
                    link.emit("setup_link_refused", {"reason": "expired"})
                    return self._send(410, b"expired")
                m = re.fullmatch(rf"/{re.escape(link.token)}/step/([a-z0-9_]{{1,12}})", self.path)
                if m and link.fetched_by is not None and len(link.steps) < 60:
                    link.steps.append(m.group(1))
                    link.emit("setup_link_step", {"step": m.group(1), "client": client})
                    return self._send(200, b"ok")
                if self.path != f"/{link.token}/{link.filename}":
                    link.emit("setup_link_refused", {"reason": "wrong token or path", "client": client})
                    return self._send(404, b"not found")
                with link._lock:
                    if link.served:
                        link.emit("setup_link_refused", {"reason": "already used", "client": client})
                        return self._send(410, b"already used")
                    link.served = True
                self._send(200, link.door, filename=link.filename)
                link.fetched_by = client
                link.emit("setup_link_served", {"client": client, "bytes": len(link.door)})
                link.outcome = "served"

            def do_POST(self):  # noqa: N802
                client = self.client_address[0]
                try:
                    inside = ipaddress.ip_address(client) in link.net
                except ValueError:
                    inside = False
                if not inside or self.path != f"/{link.token}/report" or time.monotonic() > link._deadline or link.report is not None or link.fetched_by is None or client != link.fetched_by:
                    link.emit("setup_link_refused", {"reason": "report refused", "client": client})
                    return self._send(403, b"refused")
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(min(n, 4096)))
                except (ValueError, OSError):
                    return self._send(400, b"bad request")
                clean = lambda v, cap=80: re.sub(r"[^A-Za-z0-9 ._\-\[\]():,/+=]", "", str(v))[:cap]  # noqa: E731
                link.report = {"address": client, "user": clean(body.get("user", "")), "computer": clean(body.get("computer", "")), "os": clean(body.get("os", ""), 120),
                               "host_key": clean(body.get("host_key", ""), 400)}
                link.emit("setup_link_reported", {"client": client})
                self._send(200, b"ok")
                link.outcome = "reported"
                link.reported.set()
                link.done.set()
                threading.Thread(target=link.stop, daemon=True).start()

            def _send(self, code: int, body: bytes, filename: str | None = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", "application/octet-stream" if filename else "text/plain")
                if filename:
                    self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer((str(self.addr), self.port), H)
        self.port = self._httpd.server_address[1]
        self.report_url = f"http://{self.addr}:{self.port}/{self.token}/report"
        self.door = self._door_source(self.report_url) if callable(self._door_source) else self._door_source  # the file carries the address it reports back to
        self._deadline = time.monotonic() + self.ttl_s
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        threading.Thread(target=self._expire, daemon=True).start()
        self.emit("setup_link_started", {"address": str(self.addr), "port": self.port, "ttl_s": self.ttl_s})  # the token is NOT in the event: it is a bearer secret
        return f"http://{self.addr}:{self.port}/{self.token}/{self.filename}"

    def _expire(self) -> None:
        while not self.done.wait(0.2):
            if time.monotonic() > self._deadline:
                self.outcome = "expired"
                self.emit("setup_link_expired", {})
                self.done.set()
                self.stop()
                return

    def shutdown(self, reason: str) -> bool:
        """Stop a link that is still serving because the run is over, and leave a record of it. Returns whether it was still serving (a link that already reported or
        expired has recorded its own end)."""
        if self._httpd is None:
            return False
        self.outcome = f"{self.outcome} (shutdown: {reason})"
        self.emit("setup_link_shutdown", {"reason": reason, "served": self.served, "reported": self.report is not None})
        self.done.set()
        self.stop()
        return True

    def stop(self) -> None:
        if self._httpd is not None:
            httpd, self._httpd = self._httpd, None
            httpd.shutdown()
            httpd.server_close()
