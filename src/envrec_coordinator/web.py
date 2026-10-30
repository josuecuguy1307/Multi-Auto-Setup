"""The owner's web screen: one OPTIONAL extra listening socket of the coordinator, bound to loopback and nothing else (docs/NETWORK_CONTRACT.md section 1.7, conformance N2 and N4).

It is a thin translation between HTTP and the coordinator's own front-end methods (`snapshot`, `begin_pairing`, `decide_pairing`); the pairing protocol, the owner's decision and the log all
stay in the coordinator. Plain HTML, CSS and JavaScript from `web_static/`, served from memory by this process; nothing is fetched from anywhere else.

Every request passes, in this order: the Host check (DNS rebinding) -> the session (a cookie made from the one-time link) -> for a POST, the Origin check, the CSRF token and the content
type. The one-time token, the session ids and the CSRF tokens are never written to the log, to an event or to a response other than the one that must carry them."""

from __future__ import annotations

import concurrent.futures
import hmac
import http.cookies
import ipaddress
import json
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from envrec_coordinator.pairing import PairingBusy
from envrec_coordinator.waiting import Waiting  # noqa: F401

WEB_HOST = "127.0.0.1"  # the only address the screen can bind to
STATIC = Path(__file__).parent / "web_static"
SESSION_MAX_AGE_S = 8 * 3600
MAX_SESSIONS = 8
MAX_BODY = 4096
REQUEST_TIMEOUT_S = 10.0
LOOP_TIMEOUT_S = 5.0
REFUSAL_LOG_EVERY_S = 5.0
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}
ASSETS = {"/": ("index.html", "text/html; charset=utf-8"), "/style.css": ("style.css", "text/css; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8")}


class WebBindRefused(ValueError):
    pass


def check_loopback(host: str) -> str:
    """The screen answers on this computer only: 127.0.0.1 or ::1. Anything else (every interface, a LAN address, a name) is refused."""
    try:
        a = ipaddress.ip_address(host)
    except ValueError:
        raise WebBindRefused(f"{host!r} is not an IP address: the web screen binds to a loopback literal only") from None
    if not a.is_loopback:
        raise WebBindRefused(f"{host} is not allowed: the web screen binds to loopback (127.0.0.1 or ::1) only")
    return str(a)


def load_strings(lang: str = "es") -> dict[str, str]:
    return json.loads((STATIC / f"strings.{lang}.json").read_text(encoding="utf-8"))


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]  # lower-case names
    body: bytes = b""


@dataclass
class Response:
    status: int
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Session:
    csrf: str
    expires: float


def _json(status: int, obj: dict, headers: list[tuple[str, str]] | None = None) -> Response:
    return Response(status, json.dumps(obj).encode(), headers=headers or [])


class WebScreen:
    def __init__(self, front, *, port: int = 0, clock=None) -> None:
        """`front` is the coordinator: `snapshot()`, `begin_pairing(via)`, `decide_pairing(offer, accept, via)` and `web_event(type, payload)`, all run on its event loop."""
        self.front, self.port, self.clock = front, port, clock or time.time
        self._lock = threading.Lock()
        self.strings = load_strings()
        self.assets = {p: ((STATIC / f).read_bytes(), ct) for p, (f, ct) in ASSETS.items()}
        self._token: str | None = secrets.token_urlsafe(32)  # the one-time link's secret
        self._sessions: dict[str, Session] = {}
        self._last_refusal: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self.loop = None
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    # ---- life ---------------------------------------------------------------------------------------------------------------------------------

    def start(self, loop) -> None:
        self.loop = loop
        self.server = ThreadingHTTPServer((WEB_HOST, self.port), make_handler(self))  # THE web socket: a loopback literal, no other address is ever passed
        self.server.daemon_threads = True
        try:
            check_loopback(self.server.server_address[0])  # belt and braces: what the OS bound is loopback
        except WebBindRefused:
            self.server.server_close()
            raise
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2}, name="mas-web", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(5)

    @property
    def link(self) -> str:
        """The one-time link the owner opens. It carries the secret: print it once, to the owner's terminal, and nowhere else."""
        return f"http://{WEB_HOST}:{self.port}/?t={self._token}" if self._token else f"http://{WEB_HOST}:{self.port}/"

    @property
    def cookie_name(self) -> str:
        return f"mas_session_{self.port}"  # cookies are not isolated by port: two coordinators on this computer must not share one

    # ---- the pipeline -------------------------------------------------------------------------------------------------------------------------

    def handle(self, req: Request) -> Response:
        res = self._route(req)
        res.headers.extend(SECURITY_HEADERS.items())
        return res

    def _on_loop(self, fn, *args):
        """Run `fn` on the coordinator's event loop (its state and its log belong to that thread) and return what it returns."""
        fut: concurrent.futures.Future = concurrent.futures.Future()

        def run() -> None:
            try:
                fut.set_result(fn(*args))
            except BaseException as exc:  # handed back to the request thread, which decides what the client sees
                fut.set_exception(exc)

        self.loop.call_soon_threadsafe(run)
        return fut.result(LOOP_TIMEOUT_S)

    def _refuse(self, req: Request, status: int, reason: str, message_key: str) -> Response:
        now = self.clock()
        if now - self._last_refusal.get(reason, -1e9) >= REFUSAL_LOG_EVERY_S:
            skipped = self._suppressed.pop(reason, 0)
            self._last_refusal[reason] = now
            payload = {"reason": reason, "method": req.method[:10], "path": req.path[:80], "suppressed": skipped}  # never the query string: that is where the one-time token travels
            try:
                self._on_loop(self.front.web_event, "web_refused", payload)
            except Exception:
                pass
        else:
            self._suppressed[reason] = self._suppressed.get(reason, 0) + 1
        text = self.strings.get(message_key, "")
        if req.path.startswith("/api/") or req.method == "POST":
            return _json(status, {"error": reason, "message": text})
        return Response(status, text.encode(), "text/plain; charset=utf-8")

    def _session(self, req: Request) -> Session | None:
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(req.headers.get("cookie", ""))
        except http.cookies.CookieError:
            return None
        morsel = jar.get(self.cookie_name)
        sess = self._sessions.get(morsel.value) if morsel else None
        if sess is None or sess.expires < self.clock():
            return None
        return sess

    def _open_session(self) -> tuple[str, Session]:
        now = self.clock()
        for sid in [s for s, v in self._sessions.items() if v.expires < now]:
            del self._sessions[sid]
        while len(self._sessions) >= MAX_SESSIONS:
            del self._sessions[next(iter(self._sessions))]
        sid = secrets.token_urlsafe(32)
        self._sessions[sid] = Session(secrets.token_urlsafe(32), now + SESSION_MAX_AGE_S)
        return sid, self._sessions[sid]

    def _route(self, req: Request) -> Response:
        allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if req.headers.get("host") not in allowed_hosts:  # DNS rebinding: a page on another name that resolves to 127.0.0.1 still sends ITS name here
            return self._refuse(req, 403, "host", "server.forbidden")

        sess = self._session(req)
        if sess is None:
            want = (req.query.get("t") or [""])[0]
            if req.method == "GET" and req.path == "/" and want:
                with self._lock:  # two visitors with the link at the same instant: one wins
                    token = self._token
                    ok = token is not None and hmac.compare_digest(want.encode(), token.encode())
                    if ok:
                        self._token = None  # one time: this link is dead from now on
                        sid, _ = self._open_session()
                if ok:
                    self._on_loop(self.front.web_event, "web_session_opened", {})
                    cookie = f"{self.cookie_name}={sid}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_MAX_AGE_S}"
                    return Response(303, b"", "text/plain; charset=utf-8", [("Location", "/"), ("Set-Cookie", cookie)])
                return self._refuse(req, 401, "link_invalid", "server.link_used")
            return self._refuse(req, 401, "unauthenticated", "server.unauthorized")

        if req.method == "GET" and req.query.get("t"):
            return Response(303, b"", "text/plain; charset=utf-8", [("Location", "/")])  # an already open session never leaves the secret in the address bar

        if req.method == "GET":
            return self._get(req, sess)
        if req.method == "POST":
            origins = {f"http://127.0.0.1:{self.port}", f"http://localhost:{self.port}"}
            if req.headers.get("origin") not in origins or req.headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
                return self._refuse(req, 403, "origin", "server.forbidden")
            if not hmac.compare_digest(req.headers.get("x-csrf-token", "").encode(), sess.csrf.encode()):
                return self._refuse(req, 403, "csrf", "server.forbidden")
            if req.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                return self._refuse(req, 415, "content_type", "server.bad_request")
            return self._post(req, sess)
        return Response(405, b"", "text/plain; charset=utf-8", [("Allow", "GET, POST")])

    def _state(self, sess: Session) -> dict:
        return {**self._on_loop(self.front.snapshot), "csrf": sess.csrf}

    def _get(self, req: Request, sess: Session) -> Response:
        if req.path in self.assets:
            body, ct = self.assets[req.path]
            return Response(200, body, ct)
        if req.path == "/strings.json":
            return _json(200, self.strings)
        if req.path == "/api/state":
            return _json(200, self._state(sess))
        if req.path == "/api/run":
            data = self._on_loop(self.front.run_view, (req.query.get("id") or [""])[0][:40])
            return _json(200, data) if data is not None else _json(404, {"error": "not_found"})
        if req.path == "/api/node/analysis":
            nid = (req.query.get("id") or [""])[0][:80]
            data = self._on_loop(self.front.analysis_of, nid)
            return _json(200, data) if data is not None else _json(404, {"error": "not_found"})
        if req.path == "/favicon.ico":
            return Response(204, b"", "image/x-icon")
        return _json(404, {"error": "not_found", "message": self.strings.get("server.not_found", "")})

    def _post(self, req: Request, sess: Session) -> Response:
        try:
            body = json.loads(req.body.decode("utf-8") or "{}")
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return self._refuse(req, 400, "bad_body", "server.bad_request")
        if req.path == "/api/pair/start":
            try:
                self._on_loop(self.front.begin_pairing, "web")
            except PairingBusy:
                return _json(409, {"error": "pairing_in_progress"})
            return _json(200, self._state(sess))
        if req.path == "/api/pair/offer-for":
            wid = body.get("waiting")
            if not isinstance(wid, str) or not 0 < len(wid) <= 40:
                return self._refuse(req, 400, "bad_body", "server.bad_request")
            try:
                self._on_loop(self.front.offer_for_waiting, wid, "web")
            except PairingBusy:
                return _json(409, {"error": "pairing_in_progress"})
            except Exception as exc:
                if type(exc).__name__ == "WaitingGone":
                    return _json(409, {"error": "waiting_gone"})
                raise
            return _json(200, self._state(sess))
        if req.path == "/api/analysis/start":
            node = body.get("node")
            if not isinstance(node, str) or not 0 < len(node) <= 80:
                return self._refuse(req, 400, "bad_body", "server.bad_request")
            view = self._on_loop(self.front.start_analysis, node, "claude-code" if body.get("by") == "claude-code" else "web")  # `by` is a label for the log, not an authority: the screen is the owner's, and a start that is not the owner's click says so
            return _json(200, {**self._state(sess), "run": view})
        if req.path == "/api/options/start":
            node = body.get("node")
            if not isinstance(node, str) or not 0 < len(node) <= 80:
                return self._refuse(req, 400, "bad_body", "server.bad_request")
            view = self._on_loop(self.front.start_options_run, node, "claude-code" if body.get("by") == "claude-code" else "web")
            return _json(200, {**self._state(sess), "run": view})
        if req.path == "/api/lan/start":
            view = self._on_loop(self.front.start_lan_run, "claude-code" if body.get("by") == "claude-code" else "web")
            return _json(200, {**self._state(sess), "run": view})
        if req.path == "/api/run/answer":
            run, q, opt = body.get("run"), body.get("question"), body.get("option")
            if not all(isinstance(x, str) and 0 < len(x) <= 80 for x in (run, q, opt)):
                return self._refuse(req, 400, "bad_body", "server.bad_request")
            if not self._on_loop(self.front.answer_run, run, q, opt, "claude-code" if body.get("by") == "claude-code" else "owner"):  # a label for the log, as for the start: an answer that is not the owner's says so
                return _json(409, {"error": "nothing_to_answer"})
            return _json(200, self._state(sess))
        if req.path == "/api/run/pick":
            run, opt = body.get("run"), body.get("option")
            if not all(isinstance(x, str) and 0 < len(x) <= 120 for x in (run, opt)):
                return self._refuse(req, 400, "bad_body", "server.bad_request")
            if not self._on_loop(self.front.pick_option, run, opt):
                return _json(409, {"error": "nothing_to_pick"})
            return _json(200, self._state(sess))
        if req.path == "/api/task/run":
            node, ttype = body.get("node"), body.get("type")
            if not isinstance(node, str) or not 0 < len(node) <= 80 or ttype not in ("node_report", "machine_analysis"):
                return self._refuse(req, 400, "bad_body", "server.bad_request")
            self._on_loop(self.front.run_task, node, ttype, "web")
            return _json(200, self._state(sess))
        if req.path == "/api/pair/decision":
            offer, accept = body.get("offer"), body.get("accept")
            if not isinstance(offer, str) or not 0 < len(offer) <= 64 or not isinstance(accept, bool):
                return self._refuse(req, 400, "bad_body", "server.bad_request")
            if not self._on_loop(self.front.decide_pairing, offer, accept, "web"):
                return _json(409, {"error": "nothing_to_decide"})
            return _json(200, self._state(sess))
        return _json(404, {"error": "not_found", "message": self.strings.get("server.not_found", "")})


def make_handler(screen: WebScreen):
    class Handler(BaseHTTPRequestHandler):
        server_version = "mas"
        sys_version = ""
        timeout = REQUEST_TIMEOUT_S  # a client that stalls does not hold a thread forever

        def log_message(self, format, *args):  # noqa: A002
            pass  # the request line carries the one-time token: nothing is printed here, ever

        def _serve(self) -> None:
            parts = urllib.parse.urlsplit(self.path)
            headers = {k.lower(): v for k, v in self.headers.items()}
            body = b""
            if self.command == "POST":
                if "transfer-encoding" in headers:
                    return self._send(Response(400, b"", "text/plain; charset=utf-8"))
                try:
                    n = int(headers.get("content-length", "0"))
                except ValueError:
                    return self._send(Response(400, b"", "text/plain; charset=utf-8"))
                if n < 0 or n > MAX_BODY:
                    return self._send(Response(413, b"", "text/plain; charset=utf-8"))
                body = self.rfile.read(n) if n else b""
            try:
                res = screen.handle(Request(self.command, parts.path, urllib.parse.parse_qs(parts.query), headers, body))
            except Exception:  # one bad request never takes the screen down, and says nothing about why
                res = Response(500, b'{"error":"server"}')
            self._send(res)

        def _send(self, res: Response) -> None:
            self.send_response(res.status)
            self.send_header("Content-Type", res.content_type)
            self.send_header("Content-Length", str(len(res.body)))
            for k, v in res.headers:
                self.send_header(k, v)
            self.end_headers()
            if res.body:
                self.wfile.write(res.body)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _serve

    return Handler
