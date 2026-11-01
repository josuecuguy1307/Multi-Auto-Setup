"""NETWORK (N1-N4): the shape of the network (docs/NETWORK_CONTRACT.md, docs/HARNESS_SPEC.md). Static checks over source; nothing is imported or run, no network is touched.

There is no node agent and no coordinator in the tree yet, so N1 and N2 pass on the real tree for the code that exists (the legacy worker is checked as node code; an absent
coordinator tree has nothing to measure). Each check ALSO runs on a fixture that violates it and must fail there: that is what keeps a passing check from being vacuous.
"""
import pytest

import netrules
from conftest import ROOT

SRC = ROOT / "src"
FIX = ROOT / "conformance" / "netfixtures"
NODE_ROOTS = [SRC / "envrec_node", SRC / "envrec_worker"]  # envrec_worker is the legacy device-side code and counts as node code
COORDINATOR_ROOT = SRC / "envrec_coordinator"
GATEWAYS = [(SRC / "envrec" / "skills" / "sbx.py", "run_script"), (SRC / "envrec" / "skills" / "sbx.py", "run_exam_code")]  # the only places model-written code (a skill script, an admission answer) is started
GATEWAY_INTERNALS = ["docker_argv", "exam_argv"]  # names that start the sandbox container; used outside sbx.py they would be a way round the gateway


# ---- N1: a node opens no listening socket ------------------------------------------------------------------------------------------------------

def test_N1_node_code_opens_no_listening_socket():
    hits = [h for root in NODE_ROOTS for h in netrules.listen_sites(root)]
    assert hits == [], "\n" + "\n".join(hits)


def test_N1_the_node_check_scans_the_legacy_worker_so_it_is_not_empty_today():
    scanned = [p.name for root in NODE_ROOTS for p in netrules._files(root)]
    assert "supervisor.py" in scanned and "__main__.py" in scanned, scanned


def test_N1_fails_on_a_node_that_binds_listens_and_serves():
    hits = netrules.listen_sites(FIX / "node_bad")
    kinds = " ".join(hits)
    assert "bind(" in kinds and "listen(" in kinds and "ThreadingHTTPServer" in kinds and "serve_forever" in kinds and "0.0.0.0" in kinds, hits


def test_N1_passes_a_node_that_only_dials_out():
    assert netrules.listen_sites(FIX / "node_ok") == []


# ---- N2: the coordinator opens exactly one LAN-facing listening socket, and at most one more, bound to loopback (the owner's web screen) --------------

def test_N2_coordinator_code_opens_exactly_one_lan_facing_socket_and_at_most_one_loopback_socket():
    ok, detail = netrules.coordinator_verdict(COORDINATOR_ROOT)
    assert ok, detail


def test_N2_sees_both_of_the_real_coordinators_sockets_so_it_is_not_vacuous():
    sites = netrules.listener_sites(COORDINATOR_ROOT)
    assert sorted(k for _, k in sites) == ["loopback", "open", "udp"], sites  # the node-facing server.py site, the loopback web.py site and the UDP discovery site


def test_N2_fails_on_a_coordinator_with_two_listening_sockets():
    ok, detail = netrules.coordinator_verdict(FIX / "coordinator_two")
    assert not ok and "2 LAN-facing listening site(s)" in detail, detail


def test_N2_fails_on_a_coordinator_that_opens_none():
    ok, detail = netrules.coordinator_verdict(FIX / "coordinator_zero")
    assert not ok and "0 listening sites" in detail, detail


def test_N2_passes_a_coordinator_with_exactly_one():
    ok, detail = netrules.coordinator_verdict(FIX / "coordinator_one")
    assert ok and "1 LAN-facing listening site(s)" in detail and "0 loopback-only" in detail, detail


def test_N2_passes_one_lan_facing_socket_plus_one_loopback_web_socket():
    for fixture in ("coordinator_web_ok", "coordinator_web_const"):  # a literal in the call, and a module constant that is a literal
        ok, detail = netrules.coordinator_verdict(FIX / fixture)
        assert ok and "1 LAN-facing listening site(s)" in detail and "1 loopback-only" in detail, (fixture, detail)


def test_N2_fails_when_the_web_screen_is_bound_to_every_interface():
    ok, detail = netrules.coordinator_verdict(FIX / "coordinator_web_wildcard")
    assert not ok and "2 LAN-facing listening site(s)" in detail, detail


def test_N2_fails_when_the_web_screens_host_is_not_a_loopback_literal_in_the_code():
    ok, detail = netrules.coordinator_verdict(FIX / "coordinator_web_lan")
    assert not ok and "2 LAN-facing listening site(s)" in detail, detail


def test_N2_fails_on_a_second_loopback_socket():
    ok, detail = netrules.coordinator_verdict(FIX / "coordinator_web_two")
    assert not ok and "2 loopback-only" in detail, detail


def test_N2_passes_one_tcp_lan_socket_plus_one_udp_discovery_socket():
    ok, detail = netrules.coordinator_verdict(FIX / "coordinator_udp_ok")
    assert ok and "1 UDP discovery site(s)" in detail, detail


def test_N2_fails_on_a_second_udp_socket():
    ok, detail = netrules.coordinator_verdict(FIX / "coordinator_udp_two")
    assert not ok and "2 UDP discovery site(s)" in detail, detail


def test_N1_a_node_has_one_short_lived_udp_socket_in_its_discovery_module_and_nowhere_else():
    hits = [h for root in NODE_ROOTS for h in netrules.udp_socket_sites(root)]
    assert [h.split(":")[0] for h in hits] == ["discover.py"], hits


def test_N1_fails_on_a_node_bound_to_a_fixed_udp_port_or_answering_queries():
    hits = " ".join(netrules.listen_sites(FIX / "node_udp_bound"))
    assert "bind(" in hits and "create_datagram_endpoint" in hits, hits


def test_N1_passes_a_node_that_only_sends_a_query_and_closes_its_socket():
    assert netrules.listen_sites(FIX / "node_udp_ok") == []
    assert [h.split(":")[0] for h in netrules.udp_socket_sites(FIX / "node_udp_ok")] == ["discover.py"]


def test_N4_at_runtime_the_node_holds_no_listening_socket_and_the_coordinator_holds_exactly_one(tmp_path):
    """SIMULATED-LOCAL: the coordinator and the node are two real processes on this machine, paired through the `mas` commands, heartbeats flowing; each process's listening sockets are read.
    Without `--web` the coordinator holds ONE socket."""
    from mas_procs import run_local_network
    res = run_local_network(tmp_path)
    assert "test-node" in res["nodes_listing"] and "s ago" in res["nodes_listing"], res["nodes_listing"]  # paired, and heartbeats arrived
    assert res["coordinator_listening"] == [("127.0.0.1", res["port"])], res["coordinator_listening"]  # exactly one, the configured port
    assert res["node_listening"] == [], res["node_listening"]  # none


def test_N4_with_the_web_screen_the_coordinator_holds_one_node_socket_and_one_loopback_socket_and_the_node_none(tmp_path):
    """SIMULATED-LOCAL: the same two processes with `mas coordinator start --web`."""
    from mas_procs import run_local_network, socket_verdict
    res = run_local_network(tmp_path, web=True)
    assert "test-node" in res["nodes_listing"] and "s ago" in res["nodes_listing"], res["nodes_listing"]
    ok, detail = socket_verdict(res["coordinator_listening"], "127.0.0.1", res["port"])
    assert ok, (detail, res["coordinator_listening"])
    web_port = int(res["web_link"].split("127.0.0.1:")[1].split("/")[0])
    assert sorted(res["coordinator_listening"]) == sorted([("127.0.0.1", res["port"]), ("127.0.0.1", web_port)]), res["coordinator_listening"]
    assert res["node_listening"] == [], res["node_listening"]


def test_N4_with_discovery_the_coordinator_holds_one_udp_socket_more_and_the_node_still_none(tmp_path):
    """SIMULATED-LOCAL: `mas coordinator start --web --discovery-port N`: the node-facing TCP socket, the loopback web socket and ONE UDP discovery socket; nothing else."""
    from mas_procs import free_port, run_local_network, socket_verdict
    dport = free_port()
    res = run_local_network(tmp_path, web=True, discovery_port=dport)
    ok, detail = socket_verdict(res["coordinator_listening"], "127.0.0.1", res["port"], udp_port=dport)
    assert ok, (detail, res["coordinator_listening"])
    assert any(x[1] == dport for x in res["coordinator_listening"]), res["coordinator_listening"]  # the discovery socket exists: the check is not blind
    assert len(res["coordinator_listening"]) == 3 and res["node_listening"] == [], res


@pytest.mark.parametrize("listening,why", [
    ([("192.168.1.5", 7443), ("0.0.0.0", 7444), ("0.0.0.0", 7445)], "a second socket that is not loopback"),  # a second wildcard socket on another port
    ([("192.168.1.5", 7443), ("0.0.0.0", 7444), ("192.168.1.5", 7444)], "more than one UDP discovery socket"),
    ([("192.168.1.5", 7443), ("0.0.0.0", 9000)], "a second socket that is not loopback"),  # the web screen bound to every interface
    ([("192.168.1.5", 7443), ("::", 9000)], "a second socket that is not loopback"),
    ([("192.168.1.5", 7443), ("192.168.1.5", 9000)], "a second socket that is not loopback"),  # the web screen on the LAN address
    ([("192.168.1.5", 7443), ("127.0.0.1", 9000), ("127.0.0.1", 9001)], "more than one loopback socket"),
    ([("127.0.0.1", 9000)], "expected exactly 1 socket at 192.168.1.5:7443"),  # the node-facing socket is missing
    ([("192.168.1.5", 7443), ("192.168.1.5", 7443), ("10.0.0.1", 22)], "a second socket that is not loopback"),
])
def test_N4_the_runtime_verdict_fails_on_every_second_socket_that_is_not_the_one_loopback_web_socket(listening, why):
    from mas_procs import socket_verdict
    ok, detail = socket_verdict(listening, "192.168.1.5", 7443, udp_port=7444)
    assert not ok and why in detail, detail


def test_N4_the_runtime_verdict_passes_the_lan_socket_alone_and_with_the_loopback_web_socket():
    from mas_procs import socket_verdict
    assert socket_verdict([("192.168.1.5", 7443)], "192.168.1.5", 7443)[0]
    assert socket_verdict([("192.168.1.5", 7443), ("127.0.0.1", 9000)], "192.168.1.5", 7443)[0]
    assert socket_verdict([("192.168.1.5", 7443), ("::1", 9000)], "192.168.1.5", 7443)[0]
    assert socket_verdict([("192.168.1.5", 7443), ("0.0.0.0", 7444), ("127.0.0.1", 9000)], "192.168.1.5", 7443, udp_port=7444)[0]  # TCP node port + UDP discovery + loopback web


def test_N4_the_runtime_check_sees_a_listening_socket_when_a_process_has_one(tmp_path):
    """The fixture that must fail: a process that listens. Without this, an empty list from `listening_of` could mean the check is blind."""
    import subprocess
    import sys
    import time

    from mas_procs import listening_of
    code = "import socket,time;s=socket.socket();s.bind(('127.0.0.1',0));s.listen();print(s.getsockname()[1],flush=True);time.sleep(30)"
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        port = int(p.stdout.readline())
        time.sleep(0.3)
        assert listening_of(p.pid) == [("127.0.0.1", port)]
        quiet = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        try:
            time.sleep(0.3)
            assert listening_of(quiet.pid) == []
        finally:
            quiet.kill()
    finally:
        p.kill()


# ---- N3: model-written code runs only through a gateway that refuses without a sandbox backend ------------------------------------------------

def test_N3_no_dynamic_execution_exists_in_src():
    hits = netrules.dynamic_exec_sites(SRC)
    assert hits == [], "\n" + "\n".join(hits) + "\n(reviewed exceptions go in netrules.DYNAMIC_EXEC_ALLOW with a reason)"


@pytest.mark.parametrize("path,func", GATEWAYS, ids=[f"{p.name}:{f}" for p, f in GATEWAYS])
def test_N3_the_gateway_refuses_before_it_starts_any_process(path, func):
    assert netrules.gateway_violations(path, func) == []


@pytest.mark.parametrize("name", GATEWAY_INTERNALS)
def test_N3_nothing_outside_the_gateway_file_starts_the_sandbox_container(name):
    assert netrules.references(SRC, name, except_file="sbx.py") == []


def test_N3_fails_on_dynamic_execution_of_generated_text():
    hits = netrules.dynamic_exec_sites(FIX / "exec_bad")
    assert any("exec(" in h for h in hits) and any("compile(" in h for h in hits), hits


def test_N3_fails_on_a_gateway_that_starts_the_process_before_checking_the_backend():
    v = netrules.gateway_violations(FIX / "exec_bad" / "sandbox.py", "run_script")
    assert len(v) == 1 and "before it checks" in v[0], v


def test_N3_fails_on_a_gateway_with_no_refusal_at_all():
    v = netrules.gateway_violations(FIX / "exec_bad" / "dynamic.py", "run_model_code")
    assert len(v) == 1 and "no 'raise SandboxUnavailable'" in v[0], v


def test_N3_passes_a_gateway_that_refuses_first():
    assert netrules.gateway_violations(FIX / "exec_ok" / "sandbox.py", "run_script") == []
    assert netrules.dynamic_exec_sites(FIX / "exec_ok") == []


def test_N3_flags_a_reference_to_a_gateway_internal_outside_its_file():
    assert netrules.references(FIX / "exec_bad", "backend_ready", except_file="sandbox.py") == []  # defined and used only inside sandbox.py
    assert netrules.references(FIX / "exec_bad", "SandboxUnavailable", except_file="dynamic.py") != []  # the same scan does find a name used elsewhere


# ---- N5: inference endpoints used by node code are loopback literals ----------------------------------------------------------------------------

NODE_CONFIGS: list = [ROOT / "config" / "models.yaml"]  # configuration files that node code reads or will read (the model profiles); add any other file a node reads


def test_N5_default_and_configured_inference_endpoints_used_by_node_code_are_loopback_literals():
    hits = [h for root in NODE_ROOTS for h in netrules.endpoint_sites(root)] + [h for cfg in NODE_CONFIGS for h in netrules.config_endpoint_sites(cfg)]
    assert hits == [], "\n" + "\n".join(hits)


def test_N5_the_scan_reads_the_legacy_worker_so_it_is_not_empty_today():
    assert "supervisor.py" in [p.name for root in NODE_ROOTS for p in netrules._files(root)]


def test_N5_fails_on_python_code_with_a_non_loopback_endpoint_or_host():
    hits = " ".join(netrules.endpoint_sites(FIX / "endpoint_bad"))
    assert "'localhost'" in hits and "api.example.invalid" in hits and "host = '10.0.0.5'" in hits, hits


def test_N5_fails_on_a_configuration_with_a_non_loopback_endpoint():
    hits = netrules.config_endpoint_sites(FIX / "endpoint_bad" / "profile.yaml")
    assert len(hits) == 2 and any("localhost" in h for h in hits) and any("api.example.invalid" in h for h in hits), hits


def test_N5_passes_loopback_literals_and_a_cli_route():
    assert netrules.endpoint_sites(FIX / "endpoint_ok") == []
    assert netrules.config_endpoint_sites(FIX / "endpoint_ok" / "profile.yaml") == []


def test_N5_runtime_a_third_party_inference_server_bound_beyond_loopback_is_refused_and_one_on_loopback_is_used(tmp_path):
    """SIMULATED-LOCAL: a stand-in inference server (the real server's flags and endpoints) started by the node's own operation. Bound to a non-loopback address it is stopped and the step fails; on 127.0.0.1 it is used.
    The static half: the operation checks the address it was given AND, after the start, the sockets the process really listens on."""
    import json
    import os
    import sys
    sys.path.insert(0, str(ROOT / "tests"))
    import test_executors as TE
    from envrec_os import procs
    if TE.envrec_os.family() == "windows" or procs.listening(os.getpid()) is None:
        pytest.skip("needs a POSIX stand-in program and a system that lets a process list its own sockets (UNTESTED elsewhere)")
    src = (SRC / "envrec_node" / "executors.py").read_text()
    assert "procs.listening(pid)" in src and "server_not_loopback" in src and "_loopback(str(args[\"host\"]))" in src
    import copy
    recipe = copy.deepcopy(TE.REAL)
    home = tmp_path / "mas"
    home.mkdir()
    world = type("W", (), {"home": home, "ctx": {"recipe": recipe, "mas_home": home, "ready_timeout_s": 20}, "scope": staticmethod(lambda *d: TE.WriteScope(home, list(d)))})
    try:
        with pytest.raises(TE.X.StepFailed) as e:
            TE.run_server(world, "wide")
        assert e.value.code == "server_not_loopback"
        d = json.loads((home / "run" / "server.json").read_text())
        assert not procs.is_ours(d["pid"], d["created"])
        TE.X.undo_start_server({}, world.scope("<mas>/run/"), world.ctx)
        out, args = TE.run_server(world, "normal")
        assert all(a.startswith("127.0.0.1:") for a in out[0]["listening"]) and TE.X.minimal_call({"host": "127.0.0.1", "port": args["port"], "prompt": "hi", "max_tokens": 4}, world.scope(), world.ctx)[0]["ok"]
    finally:
        TE.X.undo_start_server({}, world.scope("<mas>/run/"), world.ctx)


# ---- R1-R3, A1: placeholders until the router and the admission exam exist -----------------------------------------------------------------------

import routerrules  # noqa: E402


def test_R1_a_confidential_task_is_never_routed_to_cloud_a_commercial_harness_or_a_third_party_machine():
    assert routerrules.r1_violations() == []


def test_R1_fails_on_a_router_that_routes_a_confidential_task_to_a_provider_or_creates_it_from_a_cloud_chat(monkeypatch):
    from envrec_coordinator.router import engine
    from envrec_coordinator.router.model import Step
    monkeypatch.setattr(engine, "sensitivity_step", lambda cands, sens, facts, tt, pol, tried: ([c for c in cands if c.route.key not in tried], {}, Step(step="sensitivity", candidates_in=[], candidates_out=[])))
    monkeypatch.setattr(engine, "creation_refusal", lambda task, effective=None: None)
    hits = " ".join(routerrules.r1_violations())
    assert "created from a cloud chat" in hits and ("routed to claude-code" in hits or "routed to codex" in hits or "routed to muse-code" in hits or "stranger=True" in hits), hits


def test_R2_escalation_never_exceeds_two_steps():
    assert routerrules.r2_violations() == []


def test_R2_fails_on_a_router_that_escalates_a_third_time():
    hits = routerrules.r2_violations(lambda m, a, **kw: routerrules._router(m, a, max_escalation=4))
    assert any("beyond the cap" in h or "escalation reached step" in h for h in hits), hits


def test_R3_a_combination_labeled_provisional_never_receives_a_confidential_task():
    assert routerrules.r3_violations() == []


def test_R3_fails_on_a_router_that_treats_an_unmeasured_combination_as_measured(monkeypatch):
    from envrec_coordinator.router import rule
    monkeypatch.setattr(rule, "measured", lambda c, facts, tt, pol: (9, 10))
    assert any("unmeasured combination" in h for h in routerrules.r3_violations())


def test_R4_the_router_never_asks_a_model_it_imports_nothing_that_drives_one():
    assert routerrules.router_import_violations(SRC / "envrec_coordinator" / "router") == []
    assert len(routerrules.router_import_violations(FIX / "router_bad")) == 2 and routerrules.router_import_violations(FIX / "router_ok") == []


def test_L1_owner_facing_strings_of_the_router_and_the_task_rejections_are_english_ascii_text():
    from envrec_net import task as T
    from envrec_coordinator.router import strings
    texts = [*strings.REFUSALS.values(), *strings.REASONS.values(), strings.OWNER_NOTICE, strings.REDACTED_LINE, *T.REJECTIONS.values()]
    assert [t for t in texts if not t.isascii()] == []


# A1 is real now: conformance/test_admission.py


# ---- P1: no OS-specific imports or calls outside the per-OS layer; H1: no OS- or tool-specific knowledge outside skills and that layer -------------------
NETWORK_ROOTS = [SRC / d for d in ("envrec_net", "envrec_node", "envrec_coordinator", "envrec_mas", "envrec_harness")]  # the per-OS layer (src/envrec_os) is the one place allowed to; the legacy `envrec` and `envrec_worker` packages predate the rule and are exempt until they are migrated


def test_P1_no_os_specific_import_or_call_exists_outside_the_per_os_layer():
    hits = [h for root in NETWORK_ROOTS for h in netrules.os_specific_sites(root)]
    assert hits == [], "\n" + "\n".join(hits)


def test_P1_the_scan_covers_real_code_so_it_is_not_empty_today():
    assert {"server.py", "agent.py", "cli.py", "identity.py"} <= {p.name for root in NETWORK_ROOTS for p in netrules._files(root)}


def test_P1_fails_on_a_module_that_imports_winreg_calls_getuid_branches_on_the_platform_and_runs_icacls():
    hits = " ".join(netrules.os_specific_sites(FIX / "p1_bad"))
    for needle in ("import winreg", "from fcntl", "os.chmod", "os.getuid", "sys.platform", "subprocess.run"):
        assert needle in hits, (needle, hits)


def test_P1_passes_portable_code_that_asks_the_layer():
    assert netrules.os_specific_sites(FIX / "p1_ok") == []


def test_P1_the_per_os_layer_itself_is_where_such_calls_live():
    assert netrules.os_specific_sites(SRC / "envrec_os"), "if the layer holds no OS-specific call any more, P1 is not testing anything about it"


def test_H1_no_install_path_service_name_registry_key_or_os_command_string_exists_outside_skills_and_the_per_os_layer():
    hits = [h for root in NETWORK_ROOTS for h in netrules.knowledge_sites(root)]
    assert hits == [], "\n" + "\n".join(hits)


def test_H1_fails_on_install_paths_registry_keys_state_folders_and_package_manager_and_service_commands():
    hits = netrules.knowledge_sites(FIX / "h1_bad")
    assert len(hits) == 6, hits


def test_H1_passes_ordinary_strings_and_docstrings():
    assert netrules.knowledge_sites(FIX / "h1_ok") == []


def test_H1_the_knowledge_the_code_needs_is_in_a_skill_the_layer_reads():
    import json
    table = json.loads((ROOT / "skills" / "os-state-paths" / "references" / "state-dirs.json").read_text())
    assert {"darwin", "linux", "windows"} <= set(table)


# ---- N4 for the node's discovery socket: ephemeral, one, and gone after the window ---------------------------------------------------------------------------------

def node_udp_verdict(before: list, during: list, after: list, discovery_port: int) -> tuple[bool, str]:
    """`before`/`during`/`after` are this process's UDP sockets as (ip, port) at the three moments. During the window exactly one new socket, on an ephemeral port that is not the discovery
    port; afterwards no new socket is left."""
    new_during = [x for x in during if x not in before]
    left = [x for x in after if x not in before]
    problems = []
    if len(new_during) != 1:
        problems.append(f"expected exactly one new UDP socket during the window, saw {new_during}")
    elif new_during[0][1] == discovery_port:
        problems.append("the node's socket sits on the fixed discovery port")
    if left:
        problems.append(f"a UDP socket was left open after the window: {left}")
    return not problems, "; ".join(problems) or "one ephemeral socket during the window, none after"


def _udp_of_this_process():
    import os

    import psutil
    return sorted((c.laddr.ip, c.laddr.port) for c in psutil.Process(os.getpid()).net_connections(kind="udp") if c.laddr)


def test_N4_a_discovering_node_has_one_ephemeral_udp_socket_during_the_window_and_none_after():
    import socket
    import threading

    from envrec_node.discover import DiscoveryFailed, discover
    before = _udp_of_this_process()
    seen: dict = {}

    def probe(sock):
        sock.sendto(b"x", ("127.0.0.1", 9))  # force the operating system to give the socket its ephemeral port
        seen["during"] = _udp_of_this_process()
        seen["sock"] = sock

    t = threading.Thread(target=lambda: seen.update(error=_run(discover, probe)))
    t.start()
    t.join(10)
    after = _udp_of_this_process()
    ok, detail = node_udp_verdict(before, seen["during"], after, discovery_port=7444)
    assert ok, detail
    assert seen["sock"].fileno() == -1  # closed
    assert isinstance(seen["error"], DiscoveryFailed)  # nobody answered on loopback port 9


def _run(discover, probe):
    from envrec_node.discover import DiscoveryFailed
    try:
        discover(7444, 0.3, targets=[("127.0.0.1", 9)], on_socket=probe)
    except DiscoveryFailed as exc:
        return exc


def test_N4_the_node_udp_verdict_fails_on_a_fixed_port_a_second_socket_and_a_socket_left_open():
    assert not node_udp_verdict([], [("0.0.0.0", 7444)], [], 7444)[0]  # bound to the fixed discovery port
    assert not node_udp_verdict([], [("0.0.0.0", 50000), ("0.0.0.0", 50001)], [], 7444)[0]  # two sockets
    assert not node_udp_verdict([], [("0.0.0.0", 50000)], [("0.0.0.0", 50000)], 7444)[0]  # still open after the window
    assert not node_udp_verdict([], [], [], 7444)[0]  # no socket at all would mean the probe saw nothing
    assert node_udp_verdict([("0.0.0.0", 5353)], [("0.0.0.0", 5353), ("0.0.0.0", 50000)], [("0.0.0.0", 5353)], 7444)[0]


def test_N4_a_fixture_that_leaves_its_discovery_socket_open_is_caught():
    import socket
    before = _udp_of_this_process()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(b"x", ("127.0.0.1", 9))
        during = _udp_of_this_process()
        after = _udp_of_this_process()  # the "node" never closed it
        ok, detail = node_udp_verdict(before, during, after, 7444)
        assert not ok and "left open" in detail, detail
    finally:
        s.close()


# ---- T1: a remote task cannot run a non-allowlisted tool or a shell command ---------------------------------------------------------------------------------------------

def test_T1_the_nodes_task_dispatcher_has_no_shell_no_dynamic_lookup_and_one_allowlist_literal():
    assert netrules.task_dispatch_violations(SRC / "envrec_node" / "tasks.py") == []


def test_T1_the_allowlist_is_eight_read_only_names_and_the_two_named_write_tasks():
    import ast
    tree = ast.parse((SRC / "envrec_node" / "tasks.py").read_text())
    allowed = next(n for n in tree.body if isinstance(n, ast.AnnAssign) and getattr(n.target, "id", "") == "ALLOWED")
    assert [k.value for k in allowed.value.keys] == ["setup_step", "environment_run", "node_report", "machine_analysis", "os_details", "installed_programs", "gpus", "model_runtimes", "ai_clis", "memory_for_models"]  # the two write types, named, first (setup_step, and environment_run from the T4a amendment); the rest only read


def test_T1_fails_on_a_dispatcher_that_runs_a_shell_command_from_a_task_or_dispatches_by_name():
    hits = " ".join(netrules.task_dispatch_violations(FIX / "t1_bad" / "tasks.py"))
    assert "import subprocess" in hits and "getattr(" in hits and "__import__(" in hits and ".run(" in hits, hits


def test_T1_passes_a_dispatcher_with_only_a_dict_literal_allowlist():
    assert netrules.task_dispatch_violations(FIX / "t1_ok" / "tasks.py") == []


def test_T1_fails_when_the_allowlist_is_not_one_dict_literal(tmp_path):
    f = tmp_path / "tasks.py"
    f.write_text("ALLOWED = dict(a=print)\n")
    assert any("allowlist must be one dict literal" in h for h in netrules.task_dispatch_violations(f))
    f.write_text("ALLOWED = {'a': print}\nALLOWED = {'b': print}\n")
    assert any("allowlist must be one dict literal" in h for h in netrules.task_dispatch_violations(f))


# ---- clock: nothing inside a session is rejected solely on wall-clock time ----------------------------------------------------------------------------------------------

SESSION_FILES = [SRC / "envrec_net" / "session.py", SRC / "envrec_coordinator" / "server.py", SRC / "envrec_node" / "agent.py", SRC / "envrec_coordinator" / "tasks.py", SRC / "envrec_node" / "tasks.py",
                 SRC / "envrec_net" / "task.py", SRC / "envrec_net" / "discovery.py"]


def test_clock_no_session_or_discovery_validation_path_rejects_on_wall_clock_time():
    hits = netrules.session_clock_sites(SESSION_FILES)
    assert hits == [], "\n" + "\n".join(hits)


def test_clock_the_check_reads_real_files_so_it_is_not_vacuous():
    assert all(p.exists() for p in SESSION_FILES) and len(SESSION_FILES) >= 7


def test_clock_fails_on_a_session_that_compares_a_timestamp_with_the_clock_and_uses_the_time_window_verifier():
    hits = " ".join(netrules.session_clock_sites([FIX / "clock_bad" / "session.py"]))
    assert "Verifier" in hits and "stale_timestamp" in hits and "abs(" in hits, hits


def test_clock_passes_a_session_that_relies_on_the_session_id_and_the_sequence_number():
    assert netrules.session_clock_sites([FIX / "clock_ok" / "session.py"]) == []


# ---- the agent cannot make a pairing offer without an OWNER pick ----------------------------------------------------------------------------------------------------------

def test_the_agents_pair_offer_checks_for_the_owners_pick_before_it_makes_a_code():
    assert netrules.owner_pick_violations(SRC / "envrec_coordinator" / "agent.py", "_offer") == []


def test_the_pick_check_fails_when_the_offer_comes_first_or_the_check_is_missing(tmp_path):
    assert any("before it checks" in h for h in netrules.owner_pick_violations(FIX / "pick_bad" / "agent.py"))
    f = tmp_path / "agent.py"
    f.write_text("class A:\n    async def _offer(self, args, ctx):\n        return self.offer_for_waiting(args['m'], 'agent')\n")
    assert any("without checking" in h for h in netrules.owner_pick_violations(f))
    f.write_text("class A:\n    async def _offer(self, args, ctx):\n        r = self.require_owner_pick(args['m'])\n        return self.offer_for_waiting(args['m'], 'agent')\n")
    assert any("early return" in h for h in netrules.owner_pick_violations(f))
    assert any("not found" in h for h in netrules.owner_pick_violations(f, "nope"))


def test_the_pick_check_passes_the_correct_shape():
    assert netrules.owner_pick_violations(FIX / "pick_ok" / "agent.py") == []


# ---- W1-W4: the one task that writes ------------------------------------------------------------------------------------------------------------------------------------------

def test_W1_the_coordinator_sends_a_write_only_after_it_found_the_owners_approval_and_the_generic_start_refuses_writes():
    assert netrules.write_send_violations(SRC / "envrec_coordinator" / "tasks.py") == []


def test_W1_fails_on_a_coordinator_that_sends_a_write_without_the_approval_lookup_or_lets_the_generic_start_send_it():
    hits = " ".join(netrules.write_send_violations(FIX / "w1_bad" / "tasks.py"))
    assert "without looking for the owner's approval" in hits and "generic start does not refuse the write types" in hits, hits
    assert netrules.write_send_violations(FIX / "w1_bad" / "tasks.py", func="start_other") and "sends before" not in " ".join(netrules.write_send_violations(FIX / "w1_ok" / "tasks.py"))


def test_W1_passes_a_coordinator_that_looks_up_the_approval_first_and_returns_when_there_is_none():
    assert netrules.write_send_violations(FIX / "w1_ok" / "tasks.py") == []


def test_W2_the_node_authorizes_a_write_before_it_accepts_it_and_before_it_runs_it():
    assert netrules.node_write_violations(SRC / "envrec_node" / "tasks.py") == []


def test_W2_fails_on_a_node_that_runs_or_accepts_a_write_without_authorizing_it():
    hits = " ".join(netrules.node_write_violations(FIX / "w2_bad" / "tasks.py"))
    assert "runs a step without authorizing it first" in hits and "does not authorize a write before it accepts it" in hits, hits


def test_W3_fails_when_a_read_only_handler_reaches_the_setup_module_and_passes_when_none_does():
    assert any("read-only handler _os_details reaches the setup module" in h for h in netrules.node_write_violations(FIX / "w2_bad" / "tasks.py"))
    assert any("_os_details runs a setup step: only _setup_step may" in h for h in netrules.node_write_violations(FIX / "w2_bad" / "tasks.py"))
    assert netrules.node_write_violations(FIX / "w2_ok" / "tasks.py") == []


def test_W3_a_read_only_type_cannot_even_carry_what_a_write_needs():
    from envrec_net import task as T
    needs = {"plan", "step", "action", "writes", "scope", "approval", "command", "path"}
    assert [t for t, m in T.PARAMS.items() if t not in T.WRITE_TYPES and needs & set(m.model_fields)] == []


def test_W4_only_an_event_tagged_owner_that_matches_the_plan_step_and_action_counts_as_the_owners_approval():
    assert netrules.approval_source_violations(SRC / "envrec_coordinator" / "server.py") == []
    hits = " ".join(netrules.approval_source_violations(FIX / "w4_bad" / "server.py"))
    assert "does not require the OWNER tag" in hits and "does not match the step" in hits and "does not match the action" in hits, hits
    assert netrules.approval_source_violations(FIX / "w4_ok" / "server.py") == []


# ---- W-env: the environment run (amends 3.9): the second task that writes -------------------------------------------------------------------------------------------------------

def test_Wenv_the_coordinator_sends_an_environment_run_only_after_it_found_the_owners_origin_of_that_task():
    assert netrules.write_send_violations(SRC / "envrec_coordinator" / "tasks.py", func="start_environment_run", lookup="origins") == []
    assert "environment_run" in __import__("envrec_net.task", fromlist=["WRITE_TYPES"]).WRITE_TYPES  # so the generic start refuses it (W1)


def test_Wenv_fails_on_a_coordinator_that_sends_without_the_origin_lookup_or_sends_before_it():
    hits = " ".join(netrules.write_send_violations(FIX / "w1env_bad" / "tasks.py", func="start_environment_run", lookup="origins"))
    assert "without looking for the owner's approval" in hits, hits
    assert "sends before" in " ".join(netrules.write_send_violations(FIX / "w1env_bad" / "tasks.py", func="start_other", lookup="origins"))
    assert netrules.write_send_violations(FIX / "w1env_ok" / "tasks.py", func="start_environment_run", lookup="origins") == []


def test_Wenv_the_node_authorizes_an_environment_run_before_it_accepts_it_and_before_it_runs_it():
    assert netrules.node_env_violations(SRC / "envrec_node" / "tasks.py") == []
    assert netrules.node_write_violations(SRC / "envrec_node" / "tasks.py") == []  # and the setup checks still hold with the second write type in the allowlist


def test_Wenv_fails_on_a_node_that_runs_or_accepts_an_environment_without_authorizing_it():
    hits = " ".join(netrules.node_env_violations(FIX / "w2env_bad" / "tasks.py"))
    assert "runs an environment without authorizing it first" in hits and "_os_details runs an environment: only _environment_run may" in hits and "does not authorize an environment run before it accepts it" in hits, hits
    assert netrules.node_env_violations(FIX / "w2env_ok" / "tasks.py") == []


def test_Wenv_only_an_event_tagged_owner_that_names_the_task_counts_as_the_owners_origin():
    assert netrules.origin_source_violations(SRC / "envrec_coordinator" / "server.py") == []
    hits = " ".join(netrules.origin_source_violations(FIX / "w4env_bad" / "server.py"))
    assert "does not require the OWNER tag" in hits and "reads another kind of event" in hits and "does not match the task" in hits, hits
    assert netrules.origin_source_violations(FIX / "w4env_ok" / "server.py") == []


def test_Wenv_a_subtask_has_an_origin_only_through_a_recorded_decomposition_rechecked_from_the_owners_decision_and_matched_by_id():
    assert netrules.decomposition_source_violations(SRC / "envrec_coordinator" / "server.py") == []
    hits = " ".join(netrules.decomposition_source_violations(FIX / "w5env_bad" / "server.py"))
    for why in ("reads another kind of record", "does not re-check the owner's decision", "does not match the subtask by its id", "lets a second task hold", "does not compare the task's type", "does not compare the task's level"):
        assert why in hits, hits
    assert netrules.decomposition_source_violations(FIX / "w5env_ok" / "server.py") == []


def test_Wenv_the_owners_decision_is_an_owner_tagged_event_from_a_human_gesture_for_exactly_that_decomposition():
    assert netrules.owner_decision_violations(SRC / "envrec_coordinator" / "server.py") == []
    hits = " ".join(netrules.owner_decision_violations(FIX / "w6env_bad" / "server.py"))
    for why in ("reads another kind of event", "does not require the OWNER tag", "does not require a human gesture", "does not compare the recorded decomposition"):
        assert why in hits, hits
    assert netrules.owner_decision_violations(FIX / "w6env_ok" / "server.py") == []


# ---- U1: an unpaired node is refused ---------------------------------------------------------------------------------------------------------------------------------

def test_U1_the_real_coordinator_removes_an_unpaired_node_first_and_refuses_unknown_certificates():
    assert netrules.unpair_violations(SRC / "envrec_coordinator" / "server.py") == []


def test_U1_fails_on_a_coordinator_that_removes_late_or_accepts_unknown_certificates():
    hits = netrules.unpair_violations(FIX / "unpair_bad" / "server.py")
    assert any("awaits" in h for h in hits) and any("does not refuse" in h for h in hits)


def test_U1_passes_the_correct_shape():
    assert netrules.unpair_violations(FIX / "unpair_ok" / "server.py") == []


def test_U1_runtime_a_node_unpaired_by_the_real_code_is_refused_and_one_unpaired_by_a_coordinator_that_does_not_remove_is_not(tmp_path, monkeypatch):
    """The failing fixture, at runtime: with `Registry.remove` neutered, the same sequence leaves the node CONNECTED, so the check below would catch a coordinator that forgot to remove."""
    import asyncio
    import sys
    sys.path.insert(0, str(ROOT / "tests"))
    from envrec_coordinator.registry import Registry
    from envrec_node.agent import NodeAgent
    from mas_world import coordinator, offer, pair_node

    async def scenario(root):
        async with coordinator(root) as c:
            offer(c, "482915")
            await pair_node(c, root, "482915")
            nid = c.registry.all()[0].node_id
            await c.unpair_node(nid)
            agent = NodeAgent(root / "node1", interval_s=0.2, clock=c.clock, base_delay_s=0.05)
            task = asyncio.create_task(agent.run())
            for _ in range(100):
                if c.log.events("node_connected") or c.log.events("connection_refused"):
                    break
                await asyncio.sleep(0.05)
            connected = bool(c.log.events("node_connected"))
            agent.stop_event.set()
            await asyncio.wait_for(task, 5)
            return connected

    assert asyncio.run(scenario(tmp_path / "real")) is False
    monkeypatch.setattr(Registry, "remove", lambda self, node_id: False)
    assert asyncio.run(scenario(tmp_path / "broken")) is True
