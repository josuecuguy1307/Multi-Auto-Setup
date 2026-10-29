"""The owner's web screen on the coordinator: loopback only, session token, Host check, CSRF, and the same pairing protocol as the CLI. SIMULATED-LOCAL: real sockets on loopback, one machine."""
from __future__ import annotations

import asyncio
import http.client
import json
import re
import socket
from pathlib import Path

import psutil
import pytest

import envrec_coordinator.web as webmod
from envrec_coordinator.control import read_json, write_json
from envrec_coordinator.web import WebBindRefused, check_loopback
from envrec_node.agent import NodeAgent
from envrec_node.pair import PairingFailed
from mas_world import Clock, coordinator, events, pair_node, wait_event

STATIC = Path(webmod.__file__).parent / "web_static"


def run(coro):
    return asyncio.run(coro)


class Browser:
    """A minimal browser: keeps the cookie, sends what a page's script would send. Calls block, so they run in a thread (the handlers wait for the coordinator's loop)."""

    def __init__(self, c) -> None:
        self.c, self.port = c, c.web.port
        self.cookie = ""
        self.csrf = ""

    def _do(self, method, path, headers, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers)
            r = conn.getresponse()
            data = r.read()
            return r.status, {k.lower(): v for k, v in r.getheaders()}, data
        finally:
            conn.close()

    async def raw(self, method, path, *, headers=None, body=None, host=None, cookie=True):
        h = {"Host": host or f"127.0.0.1:{self.port}", **(headers or {})}
        if cookie and self.cookie:
            h["Cookie"] = self.cookie
        return await asyncio.to_thread(self._do, method, path, h, body)

    async def open(self):
        """Use the one-time link exactly as the owner would."""
        status, hdrs, _ = await self.raw("GET", "/?t=" + self.c.web.link.split("?t=")[1], cookie=False)
        assert status == 303 and hdrs["location"] == "/"
        self.cookie = hdrs["set-cookie"].split(";")[0]
        return hdrs

    async def state(self):
        status, _, data = await self.raw("GET", "/api/state")
        assert status == 200, status
        d = json.loads(data)
        self.csrf = d["csrf"]
        return d

    async def post(self, path, obj, *, origin=True, csrf=True, ctype="application/json", host=None):
        h = {"Content-Type": ctype}
        if origin is True:
            h["Origin"] = f"http://127.0.0.1:{self.port}"
        elif origin:
            h["Origin"] = origin
        if csrf is True:
            h["X-CSRF-Token"] = self.csrf
        elif csrf:
            h["X-CSRF-Token"] = csrf
        status, _, data = await self.raw("POST", path, headers=h, body=json.dumps(obj), host=host)
        return status, (json.loads(data) if data.startswith(b"{") else {})


@pytest.fixture
def web(tmp_path):
    async def make(**kw):
        return coordinator(tmp_path, web=True, **kw)
    return make


async def settle(browser, predicate, timeout=5.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        d = await browser.state()
        if predicate(d):
            return d
        await asyncio.sleep(0.05)
    raise AssertionError(f"never reached; last state {d}")


# ---- off by default, loopback only ---------------------------------------------------------------------------------------------------------

def test_the_web_screen_is_off_unless_asked_for(tmp_path):
    async def go():
        async with coordinator(tmp_path) as c:
            assert c.web is None
            assert [x for x in psutil.Process().net_connections(kind="inet") if x.status == psutil.CONN_LISTEN and x.laddr.port == c.port]  # only the node port exists
    run(go())


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "", "192.168.1.20", "10.0.0.5", "169.254.1.1", "localhost", "example.com", "224.0.0.1"])
def test_a_bind_address_that_is_not_a_loopback_literal_is_refused(host):
    with pytest.raises(WebBindRefused):
        check_loopback(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_loopback_literals_are_accepted(host):
    assert check_loopback(host) == host


def test_the_screen_listens_on_loopback_only_and_a_lan_address_of_this_computer_does_not_reach_it(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            mine = [x.laddr for x in psutil.Process().net_connections(kind="inet") if x.status == psutil.CONN_LISTEN and x.laddr.port == c.web.port]
            assert [(a.ip, a.port) for a in mine] == [("127.0.0.1", c.web.port)]
            lan = [a.address for addrs in psutil.net_if_addrs().values() for a in addrs if a.family == socket.AF_INET and not a.address.startswith("127.")]
            if not lan:
                pytest.skip("this machine has no non-loopback IPv4 address to try")

            def dial():
                with pytest.raises(OSError):
                    socket.create_connection((lan[0], c.web.port), timeout=3)
            await asyncio.to_thread(dial)
    run(go())


# ---- the session: one-time link, cookie ----------------------------------------------------------------------------------------------------

def test_every_request_without_the_session_gets_401_whatever_it_asks_for(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            b = Browser(c)
            for path in ("/", "/app.js", "/style.css", "/strings.json", "/api/state", "/favicon.ico", "/nope"):
                status, _, body = await b.raw("GET", path, cookie=False)
                assert status == 401, path
                assert b"mas_session" not in body
            status, _ = await b.post("/api/pair/start", {}, csrf=False)
            assert status == 401
            assert c.owner.view() is None  # nothing happened
    run(go())


def test_a_wrong_or_missing_token_is_401_and_sets_no_cookie(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            b = Browser(c)
            for q in ("/?t=wrong", "/?t=", "/?t=" + "A" * 43):
                status, hdrs, _ = await b.raw("GET", q, cookie=False)
                assert status == 401 and "set-cookie" not in hdrs
    run(go())


def test_the_link_opens_a_session_once_with_an_httponly_samesite_strict_cookie_and_then_is_dead(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            link_token = c.web.link.split("?t=")[1]
            b = Browser(c)
            hdrs = await b.open()
            cookie = hdrs["set-cookie"]
            assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and cookie.startswith(f"mas_session_{c.web.port}=")
            assert link_token not in cookie  # the cookie is a different secret from the link
            status, _, body = await b.raw("GET", "/")
            assert status == 200 and b"<!doctype html>" in body
            again = Browser(c)
            status, hdrs2, _ = await again.raw("GET", "/?t=" + link_token, cookie=False)
            assert status == 401 and "set-cookie" not in hdrs2  # used once: a second visitor with the same link gets nothing
            status, hdrs3, _ = await b.raw("GET", "/?t=" + link_token)  # the open session is sent home, the secret leaves the address bar
            assert status == 303 and hdrs3["location"] == "/"
    run(go())


def test_responses_carry_a_strict_content_security_policy_and_no_store(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            b = Browser(c)
            await b.open()
            _, hdrs, _ = await b.raw("GET", "/")
            csp = hdrs["content-security-policy"]
            assert "default-src 'none'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
            assert hdrs["cache-control"] == "no-store" and hdrs["x-content-type-options"] == "nosniff" and hdrs["referrer-policy"] == "no-referrer"
    run(go())


# ---- Host header (DNS rebinding) -----------------------------------------------------------------------------------------------------------

def test_a_forged_host_is_403_even_with_a_valid_session_and_even_without_one(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            b = Browser(c)
            await b.open()
            await b.state()
            for bad in ("evil.example", "evil.example:%d" % c.web.port, "127.0.0.1", "127.0.0.1:1", "localhost", "10.0.0.5:%d" % c.web.port, "127.0.0.1.evil.example:%d" % c.web.port):
                status, _, _ = await b.raw("GET", "/api/state", host=bad)
                assert status == 403, bad
                status, _, _ = await b.raw("GET", "/api/state", host=bad, cookie=False)
                assert status == 403, bad
                status, _ = await b.post("/api/pair/start", {}, host=bad)
                assert status == 403, bad
            assert c.owner.view() is None
            status, _, _ = await b.raw("GET", "/api/state", host=f"localhost:{c.web.port}")  # the other accepted name
            assert status == 200
    run(go())


# ---- POST: Origin, CSRF -------------------------------------------------------------------------------------------------------------------

def test_a_cross_origin_post_is_403_and_changes_nothing(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            b = Browser(c)
            await b.open()
            await b.state()
            for origin in ("http://evil.example", "http://127.0.0.1:1", "null", "https://127.0.0.1:%d" % c.web.port, False):
                status, _ = await b.post("/api/pair/start", {}, origin=origin)
                assert status == 403, origin
            assert c.owner.view() is None and not events(c, "pair_offer_created")
    run(go())


def test_a_post_without_or_with_a_wrong_csrf_token_is_403(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            b = Browser(c)
            await b.open()
            await b.state()
            for token in (False, "wrong", b.csrf[:-1] + ("A" if b.csrf[-1] != "A" else "B")):
                status, _ = await b.post("/api/pair/start", {}, csrf=token)
                assert status == 403
            assert c.owner.view() is None
            status, _ = await b.post("/api/pair/start", {})
            assert status == 200
    run(go())


def test_a_post_must_be_json_and_small_and_well_formed(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            b = Browser(c)
            await b.open()
            await b.state()
            status, _ = await b.post("/api/pair/start", {}, ctype="text/plain")
            assert status == 415
            status, _ = await b.post("/api/pair/decision", {"offer": 5, "accept": True})
            assert status == 400
            status, _ = await b.post("/api/pair/decision", {"offer": "x", "accept": "yes"})
            assert status == 400
            h = {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{b.port}", "X-CSRF-Token": b.csrf}
            status, _, _ = await b.raw("POST", "/api/pair/start", headers=h, body="x" * 5000)
            assert status == 413
            status, _, _ = await b.raw("POST", "/api/pair/start", headers=h, body="[1]")
            assert status == 400
            status, _, _ = await b.raw("PUT", "/api/state")
            assert status == 405
    run(go())


# ---- the same protocol as the CLI ---------------------------------------------------------------------------------------------------------

async def start_and_pair(c, b, tmp_path, name="node1"):
    """The owner presses the button; a node uses the code. Returns the node's pairing task."""
    st = await b.post("/api/pair/start", {})
    assert st[0] == 200
    d = await b.state()
    code = d["pairing"]["code"]
    assert re.fullmatch(r"\d{6}", code) and d["pairing"]["status"] == "waiting"
    return code, asyncio.create_task(pair_node(c, tmp_path, code, node=name))


def test_approve_through_the_web_pairs_the_node_and_the_decision_is_the_owners(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True, answer=None) as c:
            b = Browser(c)
            await b.open()
            await b.state()
            code, task = await start_and_pair(c, b, tmp_path)
            d = await settle(b, lambda d: d["pairing"]["status"] == "confirm")
            cand = d["pairing"]["candidate"]
            assert cand["name"] == "node1" and cand["fingerprint"].startswith("sha256:") and "code" not in d["pairing"]  # the code is gone once it is used
            status, _ = await b.post("/api/pair/decision", {"offer": d["pairing"]["offer"], "accept": True})
            assert status == 200
            await asyncio.wait_for(task, 10)
            d = await settle(b, lambda d: d["pairing"]["status"] == "accepted")
            assert [m["name"] for m in d["machines"]] == ["node1"]
            owner = events(c, "owner_decision")
            assert len(owner) == 1 and owner[0].effective_tag == "OWNER" and owner[0].payload["accepted"] is True and owner[0].payload["via"] == "web"
            req = events(c, "owner_requested_pairing")
            assert req and req[0].effective_tag == "OWNER" and req[0].payload["via"] == "web"
            states = [(e.payload["from"], e.payload["to"]) for e in events(c, "protocol_state")]
            assert states == [("offered", "exchange"), ("exchange", "owner_confirm"), ("owner_confirm", "accepted")]  # the web went through protocol-pair, not around it
            assert all(e.effective_tag in ("GUARD", "OWNER") for e in events(c))
    run(go())


def test_reject_through_the_web_stores_nothing_and_is_recorded_as_the_owners(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True, answer=None) as c:
            b = Browser(c)
            await b.open()
            await b.state()
            code, task = await start_and_pair(c, b, tmp_path)
            d = await settle(b, lambda d: d["pairing"]["status"] == "confirm")
            status, _ = await b.post("/api/pair/decision", {"offer": d["pairing"]["offer"], "accept": False})
            assert status == 200
            with pytest.raises(PairingFailed):
                await asyncio.wait_for(task, 10)
            d = await settle(b, lambda d: d["pairing"]["status"] == "refused")
            assert d["machines"] == [] and c.registry.all() == []
            owner = events(c, "owner_decision")
            assert owner[0].effective_tag == "OWNER" and owner[0].payload["accepted"] is False and owner[0].payload["via"] == "web"
    run(go())


def test_a_decision_for_nothing_pending_is_refused_and_the_first_answer_wins(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True, answer=None) as c:
            b = Browser(c)
            await b.open()
            await b.state()
            status, body = await b.post("/api/pair/decision", {"offer": "nope", "accept": True})
            assert status == 409 and body["error"] == "nothing_to_decide"
            code, task = await start_and_pair(c, b, tmp_path)
            d = await settle(b, lambda d: d["pairing"]["status"] == "confirm")
            oid = d["pairing"]["offer"]
            assert (await b.post("/api/pair/decision", {"offer": oid, "accept": False}))[0] == 200
            assert (await b.post("/api/pair/decision", {"offer": oid, "accept": True}))[0] == 409  # too late: the refusal stands
            with pytest.raises(PairingFailed):
                await asyncio.wait_for(task, 10)
            assert c.registry.all() == []
    run(go())


def test_a_second_attempt_with_the_same_code_is_refused_through_the_web_too(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True, answer=None) as c:
            b = Browser(c)
            await b.open()
            await b.state()
            code, task = await start_and_pair(c, b, tmp_path)
            d = await settle(b, lambda d: d["pairing"]["status"] == "confirm")
            await b.post("/api/pair/decision", {"offer": d["pairing"]["offer"], "accept": True})
            await asyncio.wait_for(task, 10)
            with pytest.raises(PairingFailed) as exc:
                await pair_node(c, tmp_path, code, node="node2")  # the right code, once more
            assert exc.value.code == "no_active_code"
            assert len(c.registry.all()) == 1
    run(go())


def test_a_wrong_code_kills_the_offer_the_web_showed(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True, answer=None) as c:
            b = Browser(c)
            await b.open()
            await b.state()
            code, task = await start_and_pair(c, b, tmp_path)
            task.cancel()
            with pytest.raises(PairingFailed):
                await pair_node(c, tmp_path, "000000" if code != "000000" else "111111", node="node3")
            d = await settle(b, lambda d: d["pairing"]["status"] == "failed")
            assert "code" not in d["pairing"] and c.registry.all() == []
            with pytest.raises(PairingFailed) as exc:
                await pair_node(c, tmp_path, code, node="node4")
            assert exc.value.code == "no_active_code"
    run(go())


def test_an_expired_code_is_shown_as_expired_and_cannot_be_used(tmp_path):
    clock = Clock()

    async def go():
        async with coordinator(tmp_path, web=True, answer=None, clock=clock) as c:
            b = Browser(c)
            await b.open()
            await b.state()
            await b.post("/api/pair/start", {})
            d = await b.state()
            code = d["pairing"]["code"]
            assert d["pairing"]["status"] == "waiting" and d["pairing"]["expires_at"] == clock.t + 120
            clock.advance(121)
            d = await b.state()
            assert d["pairing"]["status"] == "expired" and "code" not in d["pairing"]
            assert events(c, "pair_expired")
            with pytest.raises(PairingFailed) as exc:
                await pair_node(c, tmp_path, code)
            assert exc.value.code == "no_active_code"
            assert [(e.payload["from"], e.payload["to"]) for e in events(c, "protocol_state")] == [("offered", "expired")]
    run(go())


def test_a_second_offer_while_a_node_is_pairing_is_refused(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True, answer=None) as c:
            b = Browser(c)
            await b.open()
            await b.state()
            code, task = await start_and_pair(c, b, tmp_path)
            d = await settle(b, lambda d: d["pairing"]["status"] == "confirm")
            status, body = await b.post("/api/pair/start", {})
            assert status == 409 and body["error"] == "pairing_in_progress"
            await b.post("/api/pair/decision", {"offer": d["pairing"]["offer"], "accept": True})
            await asyncio.wait_for(task, 10)
    run(go())


def test_the_cli_and_the_web_are_two_front_ends_of_the_same_offer(tmp_path):
    """The offer made with `mas pair` (a control file) shows on the web screen, and the owner can answer it from either."""
    async def go():
        async with coordinator(tmp_path, web=True, answer=None) as c:
            b = Browser(c)
            await b.open()
            await b.state()
            write_json(c.layer, c.watcher.dir / "pair-offer.json", {"id": "cli1", "code": "135790"})
            d = await settle(b, lambda d: d["pairing"] is not None)
            assert d["pairing"]["offer"] == "cli1" and d["pairing"]["code"] == "135790"
            task = asyncio.create_task(pair_node(c, tmp_path, "135790"))
            d = await settle(b, lambda d: d["pairing"]["status"] == "confirm")
            assert read_json(c.watcher.dir / "confirm-cli1.json")["name"] == "node1"  # the terminal is asked too
            write_json(c.layer, c.watcher.dir / "decision-cli1.json", {"accept": True})  # ...and answers first
            await asyncio.wait_for(task, 10)
            d = await settle(b, lambda d: d["pairing"]["status"] == "accepted")
            assert d["pairing"]["offer"] == "cli1"
            assert events(c, "owner_decision")[0].payload["via"] == "cli"
            assert (await b.post("/api/pair/decision", {"offer": "cli1", "accept": True}))[0] == 409
            assert not (c.watcher.dir / "confirm-cli1.json").exists()
    run(go())


# ---- machines ---------------------------------------------------------------------------------------------------------------------------------

def test_machines_show_online_while_the_node_is_connected_and_offline_after_it_stops(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            c.desk.open_offer("o1", "482915")
            await pair_node(c, tmp_path, "482915")
            b = Browser(c)
            await b.open()
            d = await b.state()
            assert [(m["name"], m["status"], m["last_heartbeat"]) for m in d["machines"]] == [("node1", "offline", None)]
            agent = NodeAgent(tmp_path / "node1", interval_s=0.2, clock=c.clock)
            task = asyncio.create_task(agent.run())
            d = await settle(b, lambda d: d["machines"][0]["status"] == "online")
            m = d["machines"][0]
            assert m["os"] and m["arch"] and m["fingerprint"].startswith("sha256:") and m["last_heartbeat"] == c.clock()
            agent.stop_event.set()
            await asyncio.wait_for(task, 5)
            await settle(b, lambda d: d["machines"][0]["status"] == "offline")
    run(go())


# ---- secrets stay out of the log, and the page cannot be turned against the owner -------------------------------------------------

def test_no_secret_of_the_screen_reaches_an_event_or_stderr(tmp_path, capfd):
    async def go():
        async with coordinator(tmp_path, web=True, answer=None) as c:
            token = c.web.link.split("?t=")[1]
            b = Browser(c)
            await b.open()
            await b.state()
            await b.raw("GET", "/?t=" + token, cookie=False)  # a refused request that carries the token in its query
            await b.raw("GET", "/api/state", host="evil.example")
            await b.post("/api/pair/start", {})
            secrets_ = [token, b.csrf, b.cookie.split("=", 1)[1]]
            await asyncio.sleep(0.1)
            blob = json.dumps([e.payload for e in events(c)])
            for s in secrets_:
                assert s not in blob
            err = capfd.readouterr()
            for s in secrets_:
                assert s not in err.out + err.err
            refused = events(c, "web_refused")
            assert {e.payload["reason"] for e in refused} >= {"link_invalid", "host"} and all(e.effective_tag == "GUARD" for e in refused)
            assert events(c, "web_started") and events(c, "web_session_opened")
            assert not any("?" in e.payload.get("path", "") for e in refused)
    run(go())


def test_refusals_are_logged_at_most_once_per_reason_per_few_seconds(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            b = Browser(c)
            for _ in range(30):
                await b.raw("GET", "/api/state", cookie=False)
            assert len(events(c, "web_refused")) == 1
    run(go())


def test_the_page_never_builds_html_from_text_and_loads_nothing_from_elsewhere():
    js = (STATIC / "app.js").read_text()
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "setTimeout(\""):
        assert banned not in js, banned
    for name in ("index.html", "app.js", "style.css"):
        text = (STATIC / name).read_text()
        assert not re.search(r"https?://|//cdn|@import|googleapis|gstatic", text), name  # no CDN, no remote font, no external link
    assert "<script>" not in (STATIC / "index.html").read_text() and " style=" not in (STATIC / "index.html").read_text()  # nothing inline: the CSP forbids it


def test_every_string_the_page_asks_for_exists_and_no_copy_is_hardcoded():
    strings = json.loads((STATIC / "strings.es.json").read_text())
    html = (STATIC / "index.html").read_text()
    js = (STATIC / "app.js").read_text()
    wanted = set(re.findall(r'data-i18n="([\w.]+)"', html)) | set(re.findall(r'\bt\("([\w.]+)"', js)) - {"pair.result."}  # (that one is built from the status below)
    wanted |= {f"pair.result.{s}" for s in ("accepted", "refused", "failed", "expired", "rejected")} | {"error.session", "error.generic"}
    assert wanted - set(strings) == set(), wanted - set(strings)
    visible = re.sub(r"<(script|style|title|code)\b.*?</\1>|<[^>]+>", "", html, flags=re.S)  # (the title and the command to type are not copy)
    assert re.sub(r"[^\wáéíóúñ]", "", visible) == "", visible  # every visible word of the page comes from the strings file
    assert not re.search(r"[áéíóúñ¿¡]", js)


def _lum(h):
    h = h.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def test_text_and_background_colours_reach_4_5_to_1():
    css = (STATIC / "style.css").read_text()
    v = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-f]{6})", css))
    pairs = [("ink", "cream"), ("muted", "cream"), ("ink", "card"), ("muted", "card"), ("navy-text", "navy"), ("navy-sub", "navy"), ("teal-text", "teal"), ("teal-text", "teal-link"),
             ("teal-link", "cream"), ("teal-link", "card"), ("navy", "cream"), ("navy", "card"), ("online-ink", "online-bg"), ("offline-ink", "offline-bg"), ("bad-ink", "bad-bg"),
             ("ink", "warn-bg"), ("teal-lite", "navy"), ("teal", "cream"), ("navy", "online-bg"), ("navy", "bad-bg")]
    for fg, bg in pairs:
        hi, lo = sorted((_lum(v[fg]), _lum(v[bg])), reverse=True)
        assert (hi + 0.05) / (lo + 0.05) >= 4.5, (fg, bg)
    assert ":focus-visible" in css and "max-width: 30rem" in css  # visible focus; a phone-width rule
    assert 'name="viewport"' in (STATIC / "index.html").read_text()
