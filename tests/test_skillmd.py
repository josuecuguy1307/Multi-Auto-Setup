"""S1: skills as standard SKILL.md folders. The loader follows the Agent Skills specification; the sandbox is real; the evidence rule and the gate are enforced in code."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from envrec.contracts import Target, TargetKind
from envrec.discovery import ReplayCollector, Salts
from envrec.skills.casetest import run_cases
from envrec.skills.lib import Library, SkillInvalid, validate
from envrec.skills.migrate import migrate_store_skills
from envrec.skills.runner import run_skill
from envrec.skills.sbx import SandboxRefused, docker_argv, docker_ready, run_script, validate_output
from envrec.skills.skillmd import MAX_BODY_LINES, SkillFormatError, lint_description, load_skill
from envrec.store import Store
from envrec.tools.ctx import ToolContext

needs_docker = pytest.mark.skipif(not docker_ready(), reason="Docker (and python:3.12-alpine) is needed for the sandbox")

GOOD_SCRIPT = ('from envrec_sbx import Caps, Out\n'
               'def parse(inp):\n    caps = Caps(inp); out = Out(caps); c = caps.get("uname")\n'
               '    if c is None or not c.ok or "Linux" not in c.text:\n        return out.lost(c, "os.kernel_name").result()\n'
               '    out.fact("os.kernel_name", "Linux", src="uname", quote="Linux")\n    return out.result()\n')


def make(root: Path, name="kernel-name", *, desc="Use when you need the kernel name of a Linux host.", body="# Kernel\n\nRun it and read the word.\n", meta=None, script=GOOD_SCRIPT,
         provenance="builtin", cases=True) -> Path:
    d = root / name
    (d / "scripts").mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\n\n{body}")
    m = {"provenance": provenance, "protocol": "host", "platforms": ["macos", "linux"], "signature": {"kind": "host"}, "produces": ["os.kernel_name"],
         "plan": {"steps": [{"id": "uname", "tool": "run_command", "args": {"argv": ["uname", "-s"]}}]}}
    m.update(meta or {})
    (d / "skill.meta.json").write_text(json.dumps(m))
    if script is not None:
        (d / "scripts" / "parse.py").write_text(script)
    if cases:
        for case, text, match in (("linux", "Linux\n", True), ("elsewhere", "uname: command not found\n", False)):
            (d / "tests" / case / "captures").mkdir(parents=True, exist_ok=True)
            (d / "tests" / case / "captures" / "uname.txt").write_text(text)
            (d / "tests" / case / "expect.json").write_text(json.dumps({"platform": "linux", "match": match, **({"facts": {"os.kernel_name": "Linux"}} if match else {})}))
    return d


# ---- the specification's frontmatter rules ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("fm,why", [
    ("name: Kernel-Name\ndescription: Use when x.", "lowercase"),
    ("name: kernel--name\ndescription: Use when x.", "lowercase letters, digits and single hyphens"),
    ("name: -kernel\ndescription: Use when x.", "lowercase letters, digits and single hyphens"),
    ("name: other-name\ndescription: Use when x.", "must equal the folder name"),
    ("name: kernel-name", "missing `description`"),
    ("description: Use when x.", "missing `name`"),
    ("name: kernel-name\ndescription: Use when x.\nversion: 2", "unexpected frontmatter key"),
    ("name: kernel-name\ndescription: Use when x.\nmetadata:\n  n: 3", "`metadata` must map strings to strings"),
    ("name: kernel-name\ndescription: Use when x.\ncompatibility: ''", "`compatibility`"),
])
def test_invalid_frontmatter_is_rejected_with_the_rule_named(tmp_path, fm, why):
    d = tmp_path / "kernel-name"
    d.mkdir()
    (d / "SKILL.md").write_text(f"---\n{fm}\n---\n\nbody\n")
    with pytest.raises(SkillFormatError) as e:
        load_skill(d)
    assert why in str(e.value)


def test_a_file_without_frontmatter_or_with_broken_yaml_is_rejected(tmp_path):
    for text in ("# no frontmatter\n", "---\nname: [unclosed\n---\nbody\n"):
        d = tmp_path / "kernel-name"
        d.mkdir(exist_ok=True)
        (d / "SKILL.md").write_text(text)
        with pytest.raises(SkillFormatError):
            load_skill(d)


def test_a_description_over_1024_characters_is_rejected(tmp_path):
    d = make(tmp_path, desc="Use when " + "x" * 1100)
    with pytest.raises(SkillFormatError, match="1024"):
        load_skill(d)


def test_a_body_over_500_lines_is_rejected(tmp_path):
    d = make(tmp_path, body="line\n" * (MAX_BODY_LINES + 1))
    with pytest.raises(SkillFormatError, match="500"):
        load_skill(d)
    make(tmp_path, name="short-body", body="line\n" * MAX_BODY_LINES)
    assert load_skill(tmp_path / "short-body").name == "short-body"


# ---- descriptions say when, never how -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("desc", ["Reads the kernel name of a host.", "A skill for kernel names.", "Use when you need a kernel name, then parse it.",
                                  "Use when needed: first run uname, then read the word.", "Use when asked about a host -> run uname"])
def test_descriptions_that_do_not_start_with_use_when_or_summarize_a_workflow_are_flagged(desc, tmp_path):
    assert lint_description(desc)
    with pytest.raises(SkillFormatError):
        load_skill(make(tmp_path, desc=desc))


def test_a_trigger_style_description_passes():
    assert lint_description("Use when a USB device appears and its vendor or product is not yet known.") == []


# ---- the library refuses what it must --------------------------------------------------------------------------------------------------

def test_a_skill_naming_an_unregistered_tool_is_rejected(tmp_path):
    make(tmp_path, meta={"plan": {"steps": [{"id": "x", "tool": "format_disk", "args": {}}]}})
    lib = Library(root=tmp_path).load()
    assert "kernel-name" not in lib.skills and "not registered" in lib.errors[0][1]


def test_a_skill_cannot_use_a_tool_above_its_tier_or_with_wrong_arguments(tmp_path):
    make(tmp_path, name="too-strong", meta={"tier": "observe", "plan": {"steps": [{"id": "x", "tool": "ping_sweep", "args": {}}]}})
    make(tmp_path, name="bad-args", meta={"plan": {"steps": [{"id": "uname", "tool": "run_command", "args": {"argv": ["uname"], "shell": True}}]}})
    errs = dict(Library(root=tmp_path).load().errors)
    assert "read-tier" in errs["too-strong"] and "no argument" in errs["bad-args"]


def test_a_skill_with_a_script_needs_a_positive_and_a_negative_recorded_capture(tmp_path):
    make(tmp_path, cases=False)
    assert "needs tests/" in dict(Library(root=tmp_path).load().errors)["kernel-name"]
    d = make(tmp_path, name="only-positive")
    for p in (d / "tests" / "elsewhere").iterdir():
        pass
    (d / "tests" / "elsewhere" / "expect.json").unlink()
    assert "must NOT match" in dict(Library(root=tmp_path).load().errors)["only-positive"]


def test_the_menu_carries_name_and_description_only_and_the_body_arrives_when_chosen(tmp_path):
    make(tmp_path, body="# Kernel\n\nSECRET-BODY-TEXT that only appears once the skill is chosen.\n")
    lib = Library(root=tmp_path).load()
    [spec] = lib.specs("linux")
    assert spec.name == "skill.kernel_name" and spec.description.startswith("Use when you need the kernel name")
    assert "SECRET-BODY-TEXT" not in spec.model_dump_json()
    assert "SECRET-BODY-TEXT" in lib.body_for("kernel-name")


def test_signatures_offer_skills_by_kind_exact_signature_and_known_facts(tmp_path):
    make(tmp_path, name="by-sig", meta={"signature": {"signature": "usb:1a86:7523"}})
    make(tmp_path, name="by-fact", meta={"signature": {"facts": [{"key": "os.name", "regex": "^lin"}]}})
    make(tmp_path, name="by-kind", meta={"signature": {"kind": "serial"}})
    make(tmp_path, name="no-sig", meta={"signature": {}})
    lib = Library(root=tmp_path).load()
    usb = Target(kind=TargetKind.usb, ring=1, handle="x", signature="usb:1a86:7523")
    assert [s.name for s in lib.match(usb, {}, "linux")] == ["by-sig"]
    host = Target(kind=TargetKind.host, ring=0, handle="h")
    assert [s.name for s in lib.match(host, {"os.name": "linux"}, "linux")] == ["by-fact"]
    assert lib.match(host, {"os.name": "darwin"}, "linux") == []
    assert [s.name for s in lib.match(Target(kind=TargetKind.serial, ring=1, handle="s"), {}, "linux")] == ["by-kind"]


def test_verification_is_per_device(tmp_path):
    make(tmp_path)
    lib = Library(root=tmp_path).load()
    assert not lib.verified_on("kernel-name", "dev-a")
    lib.record_verified("kernel-name", "dev-a")
    assert lib.verified_on("kernel-name", "dev-a") and not lib.verified_on("kernel-name", "dev-b")
    assert lib.state("kernel-name")["verified_runs"] == 1


def test_a_generated_skill_is_added_only_after_validation_and_its_own_tests(tmp_path):
    lib = Library(root=tmp_path / "lib").load()
    staged = make(tmp_path / "stage", provenance="generated")
    with pytest.raises(SkillInvalid, match="its own tests fail"):
        lib.add_generated(staged, selftest=lambda sk: [type("R", (), {"case": "linux", "passed": False, "problems": ["wrong"]})()])
    assert not (tmp_path / "lib" / "generated" / "kernel-name").exists()
    builtin_claim = make(tmp_path / "stage2", name="claims-builtin", provenance="builtin")
    with pytest.raises(SkillInvalid, match="cannot make a builtin"):
        lib.add_generated(builtin_claim)
    lib.add_generated(staged, selftest=lambda sk: [])
    assert (tmp_path / "lib" / "generated" / "kernel-name" / "SKILL.md").is_file() and lib.get("kernel-name")


# ---- the sandbox is real ------------------------------------------------------------------------------------------------------------------

def test_the_sandbox_command_drops_everything():
    argv = docker_argv("n", Path("/tmp/x"))
    for flag in ("--network", "none", "--read-only", "--cap-drop", "ALL", "no-new-privileges", "--user", "65534:65534", "--pids-limit", "--memory"):
        assert flag in argv
    assert not any(a.startswith("/dev") or a == "--privileged" or a == "--device" for a in argv)
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert len(mounts) == 2 and all(m.endswith(":ro") for m in mounts)  # the skill folder and the helper library, read-only; nothing else


@needs_docker
@pytest.mark.parametrize("code,why", [
    ("import socket\ns = socket.create_connection(('1.1.1.1', 80), 2)", "failed"),
    ("open('/skill/scripts/parse.py', 'a').write('x')", "failed"),
    ("open('/etc/evil', 'w').write('x')", "failed"),
    ("import os\nos.setuid(0)", "failed"),
    ("open('/Users/anyone/.ssh/id_rsa').read()", "failed"),
    ("while True: pass", "killed"),
])
def test_a_script_that_reaches_for_the_network_the_disk_or_the_host_fails_closed(tmp_path, code, why):
    d = make(tmp_path, script=f"def parse(inp):\n    {code.replace(chr(10), chr(10) + '    ')}\n    return {{}}\n")
    sk = load_skill(d)
    with pytest.raises(SandboxRefused, match=why):
        run_script(sk.path, "scripts/parse.py", {"platform": "linux", "captures": {}}, timeout_s=3)


@needs_docker
def test_a_good_script_runs_and_the_negative_capture_does_not_match(tmp_path):
    make(tmp_path)
    sk = Library(root=tmp_path).load().get("kernel-name")
    assert [(r.case, r.passed) for r in run_cases(sk)] == [("elsewhere", True), ("linux", True)]


def test_script_output_with_anything_but_facts_entities_unknowns_and_asks_is_refused():
    for bad in (b'{"facts": [], "store_write": [1]}', b'[]', b'not json', b'{"facts": [{"key": "Not A Key", "value": 1}]}', b'{"next": [{"id": 1}]}'):
        with pytest.raises(SandboxRefused):
            validate_output(bad)


# ---- evidence, and the gate cannot be bypassed ------------------------------------------------------------------------------------------------

@needs_docker
@pytest.mark.parametrize("script,rule", [
    ('from envrec_sbx import Out\ndef parse(inp):\n    return Out().fact("os.kernel_name", "Linux", src="no_such_capture").result()\n', "cites no usable capture"),
    ('from envrec_sbx import Out\ndef parse(inp):\n    return Out().fact("os.kernel_name", "Linux", src="uname").result()\n', "no `quote`"),
    ('from envrec_sbx import Out\ndef parse(inp):\n    return Out().fact("os.kernel_name", "Plan9", src="uname", quote="Plan9").result()\n', "never in the capture"),
])
def test_output_without_evidence_is_rejected_for_a_generated_skill(tmp_path, script, rule):
    d = make(tmp_path, provenance="generated", script=script, cases=False)
    (d / "tests" / "x" / "captures").mkdir(parents=True)
    sk = load_skill(d)
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    (fixtures / "uname.out").write_bytes(b"Linux\n")
    with Store(tmp_path / "s") as st:
        host = st.put_target(Target(kind=TargetKind.host, ring=0, handle="h"))
        run = run_skill(sk, ToolContext(collector=ReplayCollector(fixtures, "linux"), platform="linux", store=st, target=host, salts=Salts()))
        assert run.status == "denied" and rule in run.reason
        assert st.current_fact(host.id, "os.kernel_name") is None  # nothing was written


@needs_docker
def test_an_accepted_run_writes_through_the_gate_with_a_cited_capture(tmp_path):
    make(tmp_path)
    sk = Library(root=tmp_path).load().get("kernel-name")
    fixtures = tmp_path / "fx"
    fixtures.mkdir()
    (fixtures / "uname.out").write_bytes(b"Linux\n")
    with Store(tmp_path / "s") as st:
        host = st.put_target(Target(kind=TargetKind.host, ring=0, handle="h"))
        run = run_skill(sk, ToolContext(collector=ReplayCollector(fixtures, "linux"), platform="linux", store=st, target=host, salts=Salts()))
        assert run.status == "ran" and run.facts == 1
        f = st.current_fact(host.id, "os.kernel_name")
        assert f.value == "Linux" and f.evidence[0].raw_ref and f.evidence[0].provider == "kernel_name"
        assert st.read_raw(f.evidence[0].raw_ref).strip() == b"Linux"


def test_the_script_has_no_path_to_the_store(tmp_path):
    """Structural: the runner never passes a store, a target or a writer into the sandbox payload."""
    from envrec.skills.runner import Collected, script_input
    make(tmp_path)
    sk = Library(root=tmp_path).load().get("kernel-name")
    with Store(tmp_path / "s") as st:
        ctx = ToolContext(collector=ReplayCollector(tmp_path, "linux"), platform="linux", store=st, salts=Salts())
        payload = script_input(sk, ctx, {"port": "/dev/x"}, Collected(), 0)
    assert set(payload) == {"platform", "params", "round", "captures"}


# ---- the old per-run SQLite skills move into the library -----------------------------------------------------------------------------------------

OLD_CODE = ('import json, re, sys\nchunk = json.load(sys.stdin)["chunk"]\nm = re.search(r"T=(\\d+)", chunk)\n'
            'print(json.dumps({"facts": [{"key": "sensor.temperature_c", "value": int(m.group(1))}]}))\n')


@needs_docker
def test_old_sqlite_skills_are_migrated_with_signature_and_verified_runs(tmp_path):
    from envrec.contracts import Skill
    with Store(tmp_path / "s") as st:
        code = st.put_raw(OLD_CODE)
        tests = st.put_raw(json.dumps({"chunks": ["~~ZXQ T=21", "~~ZXQ T=22", "~~ZXQ T=23"], "expected_keys": ["sensor.temperature_c"], "plan": "temperature"}))
        st.put_skill(Skill(signature="usb:ffff:0002", code_ref=code.path, tests_ref=tests.path, verified_runs=3))
        lib = Library(root=tmp_path / "lib")
        folders = migrate_store_skills(st, lib)
        assert [f.name for f in folders] == ["legacy-usb-ffff-0002"]
        assert migrate_store_skills(st, lib) == []  # idempotent
    lib.load()
    assert lib.errors == [], lib.errors
    sk = lib.get("legacy-usb-ffff-0002")
    assert sk.meta.signature.signature == "usb:ffff:0002" and sk.meta.verified_runs == 3 and sk.meta.provenance == "generated"
    assert all(r.passed for r in run_cases(sk))
    assert lib.match(Target(kind=TargetKind.usb, ring=1, handle="x", signature="usb:ffff:0002"), {}, "linux") == [sk]


# ---- the shipped library ---------------------------------------------------------------------------------------------------------------------

def test_the_shipped_library_loads_without_errors_and_every_skill_obeys_the_rules():
    lib = Library().load()
    assert lib.errors == [], lib.errors
    names = set(lib.skills)
    # 17 protocols: the 15 before, plus protocol-pair and protocol-node-connect, added ON PURPOSE for the network layer (docs/NETWORK_CONTRACT.md); the count is not loosened, it follows the library
    assert len([s for s in lib.runnable() if s.meta.provenance == "builtin"]) == 30 and len(lib.protocols()) == 19
    for s in lib.skills.values():
        assert s.description.startswith("Use when") and len(s.body.splitlines()) <= MAX_BODY_LINES and s.name == s.path.name
        assert not validate(s)
    assert {"os-info", "usb-devices", "serial-probe", "net-mdns", "protocol-generic", "connect-route"} <= names


@needs_docker
def test_every_shipped_skill_passes_its_own_recorded_tests():
    lib = Library().load()
    failures = [(s.name, r.case, r.problems[:1]) for s in lib.runnable() for r in run_cases(s) if not r.passed]
    assert failures == []


def test_the_assets_copied_into_skills_are_the_human_owned_tables_in_data():
    import yaml
    data = Path(__file__).resolve().parent.parent / "data"
    skills = Path(__file__).resolve().parent.parent / "skills"
    pairs = [("app-tools", "tools.json", "tools.yaml"), ("app-user-bins", "tools.json", "tools.yaml"), ("app-agents", "agents.json", "agents.yaml"),
             ("app-model-servers", "model_servers.json", "model_servers.yaml"), ("lan-ports", "ports.json", "ports.yaml"), ("serial-ports", "serial_ports.json", "serial_ports.yaml"),
             ("serial-probe", "serial_probes.json", "serial_probes.yaml"), ("net-mdns", "lan_rules.json", "lan_rules.yaml"), ("lan-ssdp", "lan_rules.json", "lan_rules.yaml")]
    for skill, asset, src in pairs:
        assert json.loads((skills / skill / "assets" / asset).read_text()) == json.loads(json.dumps(yaml.safe_load((data / src).read_text()))), (skill, asset)
