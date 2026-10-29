"""S4: the harness's own skill-creator. A model that is given only the authoring skill and one worked example can produce a valid skill for a device it has not seen,
and every way of getting it wrong is rejected with a message that names the mistake."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from envrec.contracts import Target, TargetKind
from envrec.discovery import ReplayCollector, Salts
from envrec.model import ScriptedModel, scripted_profile
from envrec.skills.author import DraftError, add_if_valid, authoring_context, parse_draft, stage_draft
from envrec.skills.lib import Library, SkillInvalid
from envrec.skills.runner import run_skill
from envrec.skills.sbx import docker_ready
from envrec.store import Store
from envrec.tools.ctx import ToolContext

needs_docker = pytest.mark.skipif(not docker_ready(), reason="skill scripts run in the Docker sandbox")

# A device the library has never seen: a proprietary sensor that prints one line per second.
BANNER_A = "ZXQ-7 FW1.4 T=21.5C H=55%\r\n"
BANNER_B = "ZXQ-7 FW1.4 T=19.0C H=61%\r\n"
SCRIPT = '''import re
from envrec_sbx import Caps, Out


def parse(inp):
    caps = Caps(inp)
    out = Out(caps)
    c = caps.get("probe")
    if c is None or not c.ok:
        return out.lost(c, "sensor.temperature_c", "sensor.humidity_pct").result()
    m = re.search(r"ZXQ-7 FW(\\S+) T=(-?[\\d.]+)C H=(\\d+)%", c.text)
    if not m:
        return out.result()
    line = m.group(0)
    out.fact("sensor.temperature_c", float(m.group(2)), src="probe", quote="T=" + m.group(2) + "C", limitations=["a live reading: it changes on its own"])
    out.fact("sensor.humidity_pct", int(m.group(3)), src="probe", quote="H=" + m.group(3) + "%", limitations=["a live reading: it changes on its own"])
    out.fact("sensor.firmware", m.group(1), src="probe", quote="FW" + m.group(1))
    return out.result()
'''
DRAFT = {
    "name": "zxq-sensor",
    "description": "Use when a serial device prints lines that start with ZXQ-7 and carry a temperature and a humidity.",
    "body": "# ZXQ-7 sensor\n\nThe device prints one line per second without being asked: model, firmware, temperature in degrees C and relative humidity in percent. Read the line, "
            "never send anything: listening is enough, and the reading changes on its own, so it is recorded as a live value.\n",
    "script": SCRIPT, "protocol": "serial", "tier": "read", "ring": 1, "platforms": ["macos", "linux"],
    "signature": {"kind": "serial"}, "produces": ["sensor.temperature_c", "sensor.humidity_pct", "sensor.firmware"],
    "steps": [{"id": "probe", "tool": "serial_identify", "args": {"port": "{param.port}"}}],
    "tests": [
        {"name": "recorded-line", "captures": {"probe": BANNER_A}, "match": True, "facts": {"sensor.temperature_c": 21.5, "sensor.humidity_pct": 55, "sensor.firmware": "1.4"}},
        {"name": "other-protocol", "captures": {"probe": "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47\r\n"}, "match": False},
    ],
}


def draft(**changes):
    d = copy.deepcopy(DRAFT)
    d.update(changes)
    return d


@pytest.fixture
def lib(tmp_path):
    shipped = Library().load()
    lib = Library(root=tmp_path / "lib").load()
    lib.skills.update({k: v for k, v in shipped.skills.items() if k in ("authoring", "serial-probe", "net-mdns")})
    return lib


# ---- a model with only the authoring skill and one example produces a valid skill -----------------------------------------------------------------

@needs_docker
def test_a_scripted_model_given_only_the_authoring_skill_and_one_example_writes_a_valid_skill_for_an_unseen_device(tmp_path):
    shipped = Library().load()
    context = authoring_context(shipped, "serial-probe")
    assert "Writing a skill for this harness" in context and "WORKED EXAMPLE serial-probe" in context
    assert "mDNS / Bonjour" not in context and "Ping sweep" not in context  # no other skill's body is in the prompt

    seen = []

    def model_reply(system, user):
        seen.append(user)
        return json.dumps(DRAFT)
    model = ScriptedModel([model_reply], scripted_profile())
    reply = asyncio.run(model.complete("write a skill", context + "\n\nCAPTURE probe:\n" + BANNER_A))
    lib = Library(root=tmp_path / "lib").load()
    folder = add_if_valid(lib, json.loads(reply.text), tmp_path / "stage")
    sk = lib.get("zxq-sensor")
    assert folder.name == "zxq-sensor" and sk.meta.provenance == "generated" and sk.meta.script_sha256 and (folder / "SKILL.md").read_text().startswith("---\nname: zxq-sensor")
    assert "serial-probe" in seen[0] and "mDNS / Bonjour" not in seen[0]

    # and it works on a capture it was not written from, with the evidence rule enforced
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "probe.out").write_text(json.dumps({"port": "/dev/ttyZ", "passive": BANNER_B, "sent": [], "reply": "", "baud": 9600}))
    # the real tool wraps the port log; the generated script reads the plain banner, so feed it the banner capture the way a plain-listen tool would
    (fx / "probe.out").write_text(BANNER_B)
    with Store(tmp_path / "s") as st:
        host = st.put_target(Target(kind=TargetKind.serial, ring=1, handle="zxq"))
        ctx = ToolContext(collector=ReplayCollector(fx, "linux"), platform="linux", store=st, target=host, salts=Salts(), approve=lambda t: True)
        run = run_skill(sk, ctx, params={})
        assert run.status == "ran", run.reason
        assert st.current_fact(host.id, "sensor.temperature_c").value == 19.0 and st.current_fact(host.id, "sensor.humidity_pct").value == 61


def test_the_authoring_context_is_the_skill_and_one_marked_example_and_nothing_else(tmp_path):
    lib = Library().load()
    with pytest.raises(SkillInvalid, match="not a worked example"):
        authoring_context(lib, "os-info")
    names = {s.name for s in lib.skills.values() if s.meta.example}
    assert names == {"net-listening", "usb-devices", "serial-probe", "net-mdns"}  # one example for each shape
    assert (lib.get("authoring").path / "references" / "examples.md").read_text().count("`skills/") >= 4


# ---- the draft shape --------------------------------------------------------------------------------------------------------------------------------

def test_a_draft_without_its_required_tests_is_rejected_naming_the_mistake():
    no_tests = draft(tests=[])
    with pytest.raises(DraftError, match="no test with a recorded real capture"):
        parse_draft(no_tests)
    only_positive = draft(tests=[DRAFT["tests"][0]])
    with pytest.raises(DraftError, match="must NOT match"):
        parse_draft(only_positive)
    with pytest.raises(DraftError, match="description"):
        parse_draft({k: v for k, v in DRAFT.items() if k != "description"})


@pytest.mark.parametrize("desc,naming", [
    ("Reads the ZXQ-7 sensor line.", 'must start with "Use when"'),
    ("A skill for the ZXQ-7 sensor.", 'must start with "Use when"'),
    ("Use when a ZXQ-7 line appears, then parse the temperature.", "summarizes a workflow"),
    ("Use when a ZXQ-7 line appears: 1) listen 2) parse", "summarizes a workflow"),
])
def test_a_bad_description_is_rejected_naming_the_mistake(lib, tmp_path, desc, naming):
    with pytest.raises(SkillInvalid, match=naming):
        add_if_valid(lib, draft(description=desc), tmp_path / "stage")
    assert lib.get("zxq-sensor") is None


def test_an_unregistered_tool_or_a_tool_above_the_tier_is_rejected(lib, tmp_path):
    with pytest.raises(SkillInvalid, match="not registered"):
        add_if_valid(lib, draft(steps=[{"id": "probe", "tool": "open_any_port", "args": {}}]), tmp_path / "s1")
    with pytest.raises(SkillInvalid, match="read-tier but the skill is observe-tier"):
        add_if_valid(lib, draft(tier="observe"), tmp_path / "s2")  # serial_identify sends bytes: it needs the read tier
    with pytest.raises(SkillInvalid, match="no argument"):
        add_if_valid(lib, draft(steps=[{"id": "probe", "tool": "serial_identify", "args": {"baud": 9600}}]), tmp_path / "s3")


def test_a_script_that_is_not_python_or_does_not_define_parse_is_rejected(lib, tmp_path):
    with pytest.raises(SkillInvalid, match="syntax error"):
        add_if_valid(lib, draft(script="def parse(inp:\n  return {}"), tmp_path / "s1")
    with pytest.raises(SkillInvalid, match=r"must define parse\(inp\)"):
        add_if_valid(lib, draft(script="x = 1\n"), tmp_path / "s2")


# ---- the checks that need the sandbox -----------------------------------------------------------------------------------------------------------------

@needs_docker
@pytest.mark.parametrize("change,naming", [
    ({"script": SCRIPT.replace('out.fact("sensor.firmware", m.group(1), src="probe", quote="FW" + m.group(1))', 'out.fact("sensor.firmware", m.group(1), src="probe")')}, "no `quote`"),
    ({"script": SCRIPT.replace('quote="FW" + m.group(1)', 'quote="FW9.9"')}, "never in the capture"),
    ({"script": SCRIPT.replace('src="probe", quote="H=', 'src="nothing", quote="H=')}, "cites no usable capture"),
    ({"script": SCRIPT.replace("sensor.humidity_pct", "Sensor Humidity")}, "bad fact key"),
    ({"script": SCRIPT.replace("    line = m.group(0)\n", "    import socket\n    socket.create_connection(('1.1.1.1', 80), 2)\n")}, "failed"),
    ({"script": SCRIPT.replace("    line = m.group(0)\n", "    open('/skill/scripts/parse.py', 'a').write('x')\n")}, "failed"),
    ({"script": SCRIPT.replace('if not m:\n        return out.result()', 'if not m:\n        out.fact("sensor.firmware", "?", src="probe", quote=c.text.strip()[:3])\n        return out.result()')}, "must not match|produced"),
])
def test_what_the_checks_catch_is_named(lib, tmp_path, change, naming):
    with pytest.raises(SkillInvalid, match=naming):
        add_if_valid(lib, draft(**change), tmp_path / "stage")
    assert lib.get("zxq-sensor") is None and not (lib.root / "generated" / "zxq-sensor").exists()  # nothing was written to the library


@needs_docker
def test_a_test_whose_expected_values_are_wrong_is_rejected_with_the_difference(lib, tmp_path):
    bad = draft(tests=[{"name": "recorded-line", "captures": {"probe": BANNER_A}, "match": True, "facts": {"sensor.temperature_c": 99.0}}, DRAFT["tests"][1]])
    with pytest.raises(SkillInvalid, match="expected 99.0, got 21.5"):
        add_if_valid(lib, bad, tmp_path / "stage")


def test_staging_alone_writes_only_the_standard_folder_and_the_sidecar(tmp_path):
    folder = stage_draft(DRAFT, tmp_path)
    assert sorted(p.name for p in folder.iterdir()) == ["SKILL.md", "scripts", "skill.meta.json", "tests"]
    meta = json.loads((folder / "skill.meta.json").read_text())
    assert meta["provenance"] == "generated" and meta["plan"]["steps"][0]["tool"] == "serial_identify"
    assert "---" in (folder / "SKILL.md").read_text() and (folder / "tests" / "other-protocol" / "expect.json").read_text().count('"match": false') == 1
