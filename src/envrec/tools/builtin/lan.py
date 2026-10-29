"""Local-network hands: mDNS browse, ping sweep, SSDP search, TCP connect check. Every one stays inside this machine's own subnet (scope guard); sweeps and checks are read tier."""

from __future__ import annotations

import json
import re
import socket
import ssl
import time
from concurrent.futures import ThreadPoolExecutor

import yaml

from envrec.discovery import tables
from envrec.discovery.commands import data_dir
from envrec.discovery.sanitize import clean_text, mask_ids
from envrec.discovery.scope import RateLimiter, ScopeError, allow_multicast, scope_for_interface, validate_target
from envrec.discovery.types import Capture, Cmd, Reason
from envrec.tools.builtin._parse import neighbor_rows
from envrec.tools.ctx import ToolContext, ToolResult
from envrec.tools.registry import tool

SSDP_GROUP, SSDP_PORT = "239.255.255.250", 1900
TXT_KEYS = ("model", "md", "ty", "product", "manufacturer", "mfg", "am")  # TXT keys worth keeping; ids, names and addresses are dropped
_SAFE_NAME = re.compile(r"^[^\x00-\x1f\-][^\x00-\x1f]{0,100}$")


def load_rules() -> dict:
    return yaml.safe_load((data_dir() / "lan_rules.yaml").read_text(encoding="utf-8")) or {}


def load_ports() -> list[dict]:
    return (yaml.safe_load((data_dir() / "ports.yaml").read_text(encoding="utf-8")) or {}).get("ports", [])


def _arp_cmd(platform: str) -> Cmd:
    return Cmd("arp", ("arp", "-an"), 8) if platform == "macos" else Cmd("ip_neigh", ("ip", "neigh"), 8)


def _randomized(mac: str) -> bool:
    try:
        return bool(int(mac.split(":")[0], 16) & 0b10)
    except ValueError:
        return False


def _hosts_from(cap: Capture, platform: str) -> list[dict]:
    if not cap.ok:
        return []
    oui = tables.oui()
    rows, _ = neighbor_rows(cap.text, platform)
    return [{"ip": r["ip"], "mac": r["mac"], "vendor": tables.name_of(oui, r["mac"]), "oui": r["mac"].replace(":", "")[:6], "randomized_mac": _randomized(r["mac"]), "interface": r["iface"]} for r in rows]


# ---- mDNS through the zeroconf library (the fallback when `dns-sd` is not installed) ---------------------------------------------------------

@tool(tags=("network", "ambient", "passive", "generic_read"))
def mdns_zeroconf(ctx: ToolContext, seconds: float = 6.0) -> ToolResult:
    """Browse mDNS services with the zeroconf library and capture what was announced.

    Args:
        seconds: how long to listen for announcements.
    Returns:
        A capture named zeroconf: JSON with the service types and, per service, type, instance, host, port, addresses and the whitelisted TXT model fields.
    Tier: observe
    Platforms: linux
    Notes: passive listening; TXT records are reduced to model fields; for macOS the skill uses `dns-sd` through run_command instead.
    """
    window = float(ctx.params.get("seconds", seconds))
    if ctx.collector.origin == "remote":
        return ToolResult.fail(Reason.not_applicable, "mDNS is browsed from this machine's own network only")
    if ctx.collector.origin == "replay":
        return ToolResult.from_capture(ctx.collector.read_file("zeroconf", "x"))
    try:
        import zeroconf
    except ImportError:
        return ToolResult.fail(Reason.unavailable, "the zeroconf library is not available (pip install 'envrec[discovery]')")
    found: dict[str, dict] = {}
    zc = zeroconf.Zeroconf()
    try:
        types = zeroconf.ZeroconfServiceTypes.find(zc=zc, timeout=window)

        class L:
            def add_service(self, z, t, name): found.setdefault(name, {"type": t, "name": name})

            def update_service(self, z, t, name): pass

            def remove_service(self, z, t, name): pass
        browsers = [zeroconf.ServiceBrowser(zc, t, L()) for t in types]
        time.sleep(2.0)
        out = []
        for name, rec in list(found.items())[:80]:
            info = zc.get_service_info(rec["type"], name, timeout=1500)
            if info:
                props = {(k.decode("utf-8", "replace") if isinstance(k, bytes) else str(k)).lower(): (v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)) for k, v in (info.properties or {}).items()}
                out.append({"type": rec["type"].rstrip(".").replace(".local", ""), "instance": name.split("." + rec["type"])[0], "host": (info.server or "").rstrip("."),
                            "port": info.port, "ips": [a for a in info.parsed_addresses() if "." in a], "txt": {k: v[:40] for k, v in props.items() if k in TXT_KEYS}})
        for b in browsers:
            b.cancel()
    finally:
        zc.close()
    body = json.dumps({"types": [t.replace(".local.", "").rstrip(".") for t in types], "services": out}).encode()
    return ToolResult(captures=[Capture("zeroconf", "python-zeroconf browse", body, 0, "local", ctx.platform)])


# ---- ping sweep ----------------------------------------------------------------------------------------------------------------

def _sweep_approval(ctx: ToolContext, rate: int = 50, cap_prefix: int = 24) -> str:
    try:
        scope = ctx.params.get("scope") or scope_for_interface(cap_prefix=int(ctx.params.get("cap_prefix", cap_prefix)))
    except ScopeError as exc:
        return f"ping this machine's own subnet (refused: {exc})"
    n = scope.host_count
    return (f"ping each of the {n} other hosts of {scope.network} (interface {scope.interface}) ONCE, at most {rate} per second "
            f"(about {n / rate:.0f} s), then read the ARP table. Nothing leaves {scope.network}.")


@tool(tags=("network", "active", "lan", "generic_read"), approval=_sweep_approval)
def ping_sweep(ctx: ToolContext, rate: int = 50, cap_prefix: int = 24) -> ToolResult:
    """Ping each host of this machine's subnet once; read neighbors.

    Args:
        rate: most pings per second.
        cap_prefix: smallest prefix length allowed (24 means up to a /24).
    Returns:
        Two captures: sweep (JSON: network, addresses pinged, those that answered, rate) and the neighbor table (arp or ip_neigh).
    Tier: read
    Platforms: macos, linux
    Notes: scope guard allows only RFC1918 or link-local addresses on this machine's own interface subnets, capped at a /22; rate limited.
    """
    arp_cmd = _arp_cmd(ctx.platform)
    if ctx.collector.origin == "replay":
        sw, arp = ctx.collector.read_file("sweep", "x"), ctx.collector.run(arp_cmd)
        caps = [sw, arp]
    elif ctx.collector.origin != "local":
        return ToolResult.fail(Reason.not_applicable, "sweeps run from this machine's own network only")
    else:
        try:
            scope = ctx.params.get("scope") or scope_for_interface(cap_prefix=int(ctx.params.get("cap_prefix", cap_prefix)))
        except ScopeError as exc:
            return ToolResult.fail(Reason.denied, str(exc))
        rate = int(ctx.params.get("rate", rate))
        rl = RateLimiter(int(rate))
        wait_arg = ("-W", "700") if ctx.platform == "macos" else ("-W", "1")

        def one(ip):
            rl.wait()
            c = ctx.collector.run(Cmd("ping", ("ping", "-c", "1", *wait_arg, str(ip)), 3.0))
            return str(ip) if c.error is None else None
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=24) as ex:
            alive = [x for x in ex.map(one, scope.hosts) if x]
        arp = ctx.collector.run(arp_cmd)
        sw = Capture("sweep", f"ping sweep of {scope.network}", json.dumps({"network": str(scope.network), "scanned": scope.host_count, "alive": alive, "rate_per_s": int(rate),
                                                                          "seconds": round(time.monotonic() - t0, 1)}).encode(), 0, "local", ctx.platform)
        caps = [sw, arp]
    if not sw.ok:
        return ToolResult.fail(sw.error or Reason.unavailable, sw.detail, caps)
    return ToolResult(captures=caps)


# ---- SSDP ----------------------------------------------------------------------------------------------------------------------

_XML_FIELDS = ("friendlyName", "manufacturer", "modelName", "modelNumber", "deviceType")


def parse_ssdp_description(xml: str) -> dict:
    """Known fields out of a UPnP description WITHOUT an XML parser: devices are untrusted and entity expansion must never run. Serial numbers and UDN are not read."""
    if re.search(r"<!(DOCTYPE|ENTITY)", xml, re.I):
        return {}
    out = {}
    for f in _XML_FIELDS:
        m = re.search(rf"<{f}>([^<]{{1,120}})</{f}>", xml)
        if m:
            out[f] = clean_text(m.group(1), 80)
    return out


def _ssdp_approval(ctx: ToolContext, seconds: float = 3.0, cap_prefix: int = 24) -> str:
    try:
        scope = ctx.params.get("scope") or scope_for_interface(cap_prefix=int(ctx.params.get("cap_prefix", cap_prefix)))
        net = scope.network
    except ScopeError as exc:
        return f"send one SSDP search (refused: {exc})"
    return (f"send ONE multicast M-SEARCH to {SSDP_GROUP}:{SSDP_PORT} for {seconds:g} s, then GET the description document of each device that answered, "
            f"only from addresses inside {net}. Nothing else is sent.")


@tool(tags=("network", "active", "lan", "generic_read"), approval=_ssdp_approval)
def ssdp_search(ctx: ToolContext, seconds: float = 3.0, cap_prefix: int = 24) -> ToolResult:
    """Ask the local network which UPnP devices exist and read their descriptions.

    Args:
        seconds: how long to wait for answers.
        cap_prefix: smallest prefix length allowed (24 means up to a /24).
    Returns:
        A capture named ssdp: JSON list of devices that answered (ip, server, search targets, the description fields the device published).
    Tier: read
    Platforms: macos, linux
    Notes: one multicast search; descriptions are fetched only from the answering device inside the scope; answers from outside it are ignored.
    """
    if ctx.collector.origin == "replay":
        c = ctx.collector.read_file("ssdp", "x")
    elif ctx.collector.origin != "local":
        return ToolResult.fail(Reason.not_applicable, "SSDP runs from this machine's own network only")
    else:
        try:
            scope = ctx.params.get("scope") or scope_for_interface(cap_prefix=int(ctx.params.get("cap_prefix", cap_prefix)))
            allow_multicast(SSDP_GROUP, SSDP_PORT)
        except ScopeError as exc:
            return ToolResult.fail(Reason.denied, str(exc))
        c = _ssdp_exchange(scope, float(seconds), ctx.platform)
    return ToolResult.from_capture(c)


def _ssdp_exchange(scope, seconds: float, platform: str) -> Capture:
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\nST: ssdp:all\r\n\r\n").encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.5)
    try:
        sock.bind((str(scope.own_address), 0))
        sock.sendto(msg, (SSDP_GROUP, SSDP_PORT))
    except OSError as exc:
        sock.close()
        reason = Reason.permission if "ermission" in str(exc) or "not permitted" in str(exc) else Reason.unavailable
        return Capture("ssdp", "udp", b"", None, "local", platform, reason, f"{type(exc).__name__}: {exc}"[:160])
    found: dict[str, dict] = {}
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            data, addr = sock.recvfrom(4096)
        except OSError:
            continue
        ip = addr[0]
        try:
            validate_target(ip, cap_prefix=scope.network.prefixlen)
        except ScopeError:
            continue  # an answer from outside the scope is ignored, never followed
        rec = found.setdefault(ip, {"ip": ip, "server": None, "st": [], "location": None})
        for ln in data.decode("utf-8", "replace").splitlines():
            k, _, v = ln.partition(":")
            k = k.strip().lower()
            if k == "server":
                rec["server"] = clean_text(v, 80)
            elif k == "st" and clean_text(v, 80) not in rec["st"]:
                rec["st"].append(clean_text(v, 80))
            elif k == "location":
                rec["location"] = v.strip()
    sock.close()
    for rec in found.values():
        rec["fields"] = {}
        loc = rec.pop("location")
        if loc and loc.startswith("http://"):
            try:
                host = re.match(r"http://([\d.]+)(?::\d+)?/", loc)
                if host and host.group(1) == rec["ip"]:  # the description must come from the device that answered
                    import urllib.request
                    with urllib.request.urlopen(loc, timeout=2) as r:  # noqa: S310
                        rec["fields"] = parse_ssdp_description(r.read(65536).decode("utf-8", "replace"))
            except Exception:
                pass
    return Capture("ssdp", f"M-SEARCH {SSDP_GROUP}:{SSDP_PORT} {seconds:g}s", json.dumps(list(found.values())).encode(), 0, "local", platform)


# ---- TCP connect check ---------------------------------------------------------------------------------------------------------

def _tls_subject(sock: socket.socket) -> str | None:
    try:
        c = ssl.create_default_context()
        c.check_hostname, c.verify_mode = False, ssl.CERT_NONE
        with c.wrap_socket(sock, server_hostname=None) as s:
            der = s.getpeercert(binary_form=True)
    except Exception:
        return None
    try:
        from cryptography import x509
        return clean_text(x509.load_der_x509_certificate(der).subject.rfc4514_string(), 80)
    except Exception:
        return None


def check_port(ip: str, port: int, banner: str, limiter: RateLimiter, connect_timeout: float = 0.6) -> dict | None:
    limiter.wait()
    try:
        s = socket.create_connection((ip, port), timeout=connect_timeout)
    except OSError:
        return None
    rec: dict = {"ip": ip, "port": port, "banner": None, "server": None, "status": None, "tls_subject": None}
    try:
        s.settimeout(0.8)
        if banner == "passive":
            try:
                rec["banner"] = clean_text(s.recv(200).decode("latin-1"), 100)
            except OSError:
                pass
        elif banner == "http":
            s.sendall(f"HEAD / HTTP/1.0\r\nHost: {ip}\r\nUser-Agent: envrec-discovery\r\n\r\n".encode())
            try:
                head = s.recv(2048).decode("latin-1")
            except OSError:
                head = ""
            m = re.match(r"HTTP/\d\.\d (\d{3})", head)
            rec["status"] = int(m.group(1)) if m else None
            sm = re.search(r"^Server:\s*(.+)$", head, re.M | re.I)
            rec["server"] = clean_text(sm.group(1), 60) if sm else None
        elif banner == "tls":
            rec["tls_subject"] = _tls_subject(s)
            s = None  # wrapped and closed by _tls_subject
    finally:
        if s is not None:
            s.close()
    return rec


def _ports_approval(ctx: ToolContext, rate: int = 50, cap_prefix: int = 24) -> str:
    try:
        scope = ctx.params.get("scope") or scope_for_interface(cap_prefix=int(ctx.params.get("cap_prefix", cap_prefix)))
    except ScopeError as exc:
        return f"connect to the short port list (refused: {exc})"
    hosts = ctx.params.get("hosts") or []
    ports = ", ".join(str(p["port"]) for p in load_ports())
    n = max(len(hosts), 1) * len(load_ports())
    return (f"connect to {len(hosts) or 'the neighbor-table'} host(s) of {scope.network} on ports {ports} ({n} connections, at most {rate} per second, about {n / rate:.0f} s), "
            "read the first bytes a server volunteers, send one fixed `HEAD /` to web ports, and read TLS certificate subjects. No credentials, nothing else.")


@tool(tags=("network", "active", "lan", "generic_read"), approval=_ports_approval)
def tcp_scan(ctx: ToolContext, rate: int = 50, cap_prefix: int = 24) -> ToolResult:
    """Connect to a short fixed port list on subnet hosts; banners only.

    Args:
        rate: most connections per second.
        cap_prefix: smallest prefix length allowed (24 means up to a /24).
    Returns:
        A capture named ports: JSON with the network, the hosts checked, the ports tried and, per open port, the banner, HTTP status and server header, or TLS subject.
    Tier: read
    Platforms: macos, linux
    Notes: connect only; no credentials; targets default to the neighbor table and must pass the scope guard.
    """
    if ctx.collector.origin == "replay":
        c = ctx.collector.read_file("ports", "x")
    elif ctx.collector.origin != "local":
        return ToolResult.fail(Reason.not_applicable, "port checks run from this machine's own network only")
    else:
        try:
            scope = ctx.params.get("scope") or scope_for_interface(cap_prefix=int(ctx.params.get("cap_prefix", cap_prefix)))
            hosts = ctx.params.get("hosts")
            if hosts is None:
                arp = ctx.collector.run(_arp_cmd(ctx.platform))
                hosts = [r["ip"] for r in neighbor_rows(arp.text, ctx.platform)[0]] if arp.ok else []
            allowed = []
            for h in hosts:
                try:
                    validate_target(h, cap_prefix=scope.network.prefixlen)
                    allowed.append(h)
                except ScopeError:
                    continue
        except ScopeError as exc:
            return ToolResult.fail(Reason.denied, str(exc))
        rl = RateLimiter(int(ctx.params.get("rate", rate)))
        plan = [(h, p["port"], p["banner"]) for h in allowed for p in load_ports()]
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=24) as ex:
            res = [r for r in ex.map(lambda a: check_port(a[0], a[1], a[2], rl), plan) if r]
        c = Capture("ports", f"tcp connect check of {len(allowed)} host(s)", json.dumps({"network": str(scope.network), "hosts": allowed, "ports": [p["port"] for p in load_ports()], "open": res,
                                                                                     "seconds": round(time.monotonic() - t0, 1)}).encode(), 0, "local", ctx.platform)
    return ToolResult.from_capture(c)
