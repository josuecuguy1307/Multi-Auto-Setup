"""MCP over streamable HTTP on the owner's loopback screen (`POST /harness/v1/mcp`): the same adapter as stdio, in-process, so a chat whose model calls tools (Open WebUI's MCP tool servers, any MCP-over-HTTP host) reaches the core's tools. SIMULATED-LOCAL: a real coordinator over real HTTP on loopback; no real Open WebUI and no real model took part. The held-out case is dispatch itself: a core tool is called through the door by its data name, with no per-tool code on this transport."""
from __future__ import annotations

import asyncio
import http.client
import json

from mas_world import coordinator


def run(coro):
    return asyncio.run(coro)


def call(c, method, path, *, token=None, body=None, host=None):
    h = {"Host": host or f"127.0.0.1:{c.web.port}", "Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    conn = http.client.HTTPConnection("127.0.0.1", c.web.port, timeout=15)
    try:
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = conn.getresponse()
        data = r.read()
        return r.status, (json.loads(data) if data.startswith(b"{") else data)
    finally:
        conn.close()


def rpc(c, token, method, params=None, rid=1, host=None):
    return call(c, "POST", "/harness/v1/mcp", token=token, body={"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}, host=host)


def test_mcp_over_http_lists_and_calls_the_same_tools_as_stdio(tmp_path):
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            tok = c.harness.tokens["openwebui"]
            a = lambda *x, **k: asyncio.to_thread(rpc, c, *x, **k)  # noqa: E731
            assert (await asyncio.to_thread(call, c, "POST", "/harness/v1/mcp", body={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}))[0] == 401  # no token, no tools
            s, init = await a(tok, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {"elicitation": {}}, "clientInfo": {"name": "fake-http-host", "version": "0"}})
            assert s == 200 and init["result"]["serverInfo"]["name"] == "multi-auto-setup" and init["result"]["protocolVersion"] == "2025-11-25"
            s, note = await asyncio.to_thread(call, c, "POST", "/harness/v1/mcp", token=tok, body={"jsonrpc": "2.0", "method": "notifications/initialized"})
            assert s == 202  # a notification is acknowledged, never answered
            assert c.harness.caps["openwebui"]["elicitation"] is False  # this transport cannot ask the host: the handshake says so, approvals fall back
            s, listed = await a(tok, "tools/list", rid=2)
            names = {t["name"] for t in listed["result"]["tools"]}
            assert {"mas_network", "mas_answer", "mas_request_approval", "mas_refresh_models", "mas_measure_recommender", "mas_install_tool"} <= names
            s, out = await a(tok, "tools/call", {"name": "mas_network", "arguments": {}}, rid=3)
            assert s == 200 and not out["result"].get("isError") and out["result"]["content"][0]["text"]  # the entry tool answers as text that stands alone
            s, out = await a(tok, "tools/call", {"name": "mas_refresh_models", "arguments": {"environment": "no-such-env"}}, rid=4)  # held-out: dispatch by the spec's data name, no code for this tool on this transport
            assert out["result"].get("isError") and "no-such-env" in out["result"]["content"][0]["text"]  # the services door's typed refusal, carried as an MCP error result
            s, out = await a(tok, "no/such-method", rid=5)
            assert out["error"]["code"] == -32601  # JSON-RPC method not found, as the stdio adapter answers
    run(go())


def test_mcp_over_http_allows_host_docker_internal_only_on_the_mcp_route(tmp_path):
    """Open WebUI in Docker reaches the loopback screen as host.docker.internal: that one Host is allowed on /harness/v1/mcp only, the Bearer check still decides, and every other route and Host keeps its refusal."""
    async def go():
        async with coordinator(tmp_path, web=True) as c:
            tok = c.harness.tokens["openwebui"]
            a = lambda *x, **k: asyncio.to_thread(rpc, c, *x, **k)  # noqa: E731
            docker = f"host.docker.internal:{c.web.port}"
            # 1: valid token through the Docker name (with and without the web port) reaches the same tools
            for h in (docker, "host.docker.internal"):
                s, init = await a(tok, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "fake-docker-host", "version": "0"}}, host=h)
                assert s == 200 and init["result"]["serverInfo"]["name"] == "multi-auto-setup", h
                s, listed = await a(tok, "tools/list", rid=2, host=h)
                names = {t["name"] for t in listed["result"]["tools"]}
                assert {"mas_refresh_models", "mas_measure_recommender", "mas_install_tool"} <= names, h
            # 2 + 3: the Bearer check still runs exactly as on loopback (never a session or tool result without it)
            s, _ = await asyncio.to_thread(call, c, "POST", "/harness/v1/mcp", host=docker, body={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            assert s == 401
            s, _ = await a("wrong-token", "tools/list", host=docker)
            assert s == 401
            # 4: any other Host with a valid token is still refused before auth
            s, _ = await a(tok, "tools/list", host="evil.example")
            assert s == 403
            s, _ = await a(tok, "tools/list", host=f"evil.example:{c.web.port}")
            assert s == 403
            # 5: loopback behaviour unchanged
            s, init = await a(tok, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "fake-http-host", "version": "0"}})
            assert s == 200 and init["result"]["serverInfo"]["name"] == "multi-auto-setup"
            # 6 held-out: another /harness/v1 route with the Docker Host keeps its current refusal
            s, _ = await asyncio.to_thread(call, c, "GET", "/harness/v1/capabilities", token=tok, host=docker)
            assert s == 403
            s, _ = await asyncio.to_thread(call, c, "GET", "/harness/v1/tools", token=tok, host=docker)
            assert s == 403
            s, _ = await asyncio.to_thread(call, c, "GET", "/api/state", host=docker)
            assert s == 403
    run(go())
