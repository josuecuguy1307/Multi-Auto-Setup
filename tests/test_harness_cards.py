"""The Harness App's intents and shared cards (envrec_harness): what an intent must carry, and what a card may and may not contain. Pure: no coordinator, no network."""
from __future__ import annotations

import re
from html.parser import HTMLParser

import pytest

from envrec_harness import cards, host, icons
from envrec_harness.intents import Intent, IntentInvalid, numbered, validate

HASH = "sha256:" + "ab" * 32
OPTS = [{"id": "local", "label": "Local model"}, {"id": "api", "label": "API key", "detail": "cloud"}]
HOSTILE = '<img src=x onerror=alert(1)>"\'</script><script>alert(2)</script>'


def intent(kind="text", payload=None, text="hello", **kw):
    return Intent(id="i1", kind=kind, run_id="r1", actor=kw.pop("actor", "GUARD"), sensitivity=kw.pop("sensitivity", "internal"), payload=payload or {}, text_fallback=text, **kw)


def confirm_payload(**over):
    return {"writes": True, "summary": "Install llama.cpp", "command": "curl -L ... | tar x", "undo": "rm -r ~/mas/llama", "plan_hash": HASH, **over}


# ---- intents -------------------------------------------------------------------------------------------------------------------------------

def test_a_well_formed_intent_of_each_kind_passes():
    validate(intent("text"))
    validate(intent("progress", {"step": 1}))
    validate(intent("show_data", {"data_kind": "network", "data": {}}))
    validate(intent("ask_choice", {"writes": False, "options": OPTS}, text="Which?\n" + numbered(OPTS)))
    validate(intent("confirm", confirm_payload(), text="plan"))


@pytest.mark.parametrize("bad,why", [
    (dict(kind="screen"), "unknown kind"),
    (dict(actor="ROUTERBOT"), "unknown actor"),
    (dict(sensitivity="secret"), "unknown sensitivity"),
    (dict(text=""), "text fallback"),
    (dict(text="   "), "text fallback"),
    (dict(text="x" * 17000), "too long"),
])
def test_an_intent_without_its_basics_is_refused(bad, why):
    kw = {"kind": "text", "text": "hello", **bad}
    with pytest.raises(IntentInvalid, match=why):
        validate(intent(kw.pop("kind"), **kw) if "text" not in kw else Intent(id="i", kind=kw.pop("kind"), run_id=None, actor=kw.pop("actor", "GUARD"), sensitivity=kw.pop("sensitivity", "internal"), payload={}, text_fallback=kw.pop("text")))


def test_show_data_must_name_what_it_shows():
    with pytest.raises(IntentInvalid, match="data_kind"):
        validate(intent("show_data", {"data_kind": "hologram"}))


def test_a_choice_must_say_whether_it_writes_and_list_its_options_in_the_text():
    with pytest.raises(IntentInvalid, match="writes"):
        validate(intent("ask_choice", {"options": OPTS}, text=numbered(OPTS)))
    with pytest.raises(IntentInvalid, match="options"):
        validate(intent("ask_choice", {"writes": False, "options": []}, text="Which?"))
    with pytest.raises(IntentInvalid, match="numbered line"):
        validate(intent("ask_choice", {"writes": False, "options": OPTS}, text="Pick one of the options on screen"))  # a text-only chat could not answer this


@pytest.mark.parametrize("missing", ["summary", "command", "undo", "plan_hash"])
def test_a_confirmation_that_writes_shows_its_exact_action_its_undo_and_its_plan_hash(missing):
    p = confirm_payload()
    p.pop(missing)
    with pytest.raises(IntentInvalid):
        validate(intent("confirm", p, text="plan"))


def test_a_plan_hash_must_look_like_one():
    with pytest.raises(IntentInvalid, match="plan hash"):
        validate(intent("confirm", confirm_payload(plan_hash="trust-me"), text="plan"))


# ---- cards load nothing ---------------------------------------------------------------------------------------------------------------------

NET = {"coordinator": {"name": "Mac"}, "clouds": [{"name": "Claude"}], "nodes": [
    {"id": "a1", "name": "Lenovo", "os": "Windows 11", "kind": "laptop", "status": "online", "active": True, "facts": [{"label": "RAM", "value": 19.7, "status": "tested"}]},
    {"id": "b2", "name": "Old PC", "os": "Linux", "kind": "oldpc", "status": "busy", "route": "cloud", "confidential": True},
    {"id": "c3", "name": "Box", "os": "Debian", "kind": "server", "status": "offline"},
    {"id": "d4", "name": "Desk", "os": "macOS", "kind": "desktop", "status": "online"}]}
MACHINE = {"name": "Lenovo", "os": "Windows 11", "label": "agent analysis", "groups": {"system": [{"label": "OS", "value": "Windows 11", "status": "tested"}],
           "hardware": [{"label": "RAM", "value": 19.7, "status": "tested"}, {"label": "GPU", "value": [], "status": "unknown"}], "ai": [{"label": "Runtimes", "value": ["ollama"], "status": "inferred"}]},
           "findings": [{"text": "CPU only", "status": "hypothesis"}]}
OPTIONS = {"intent_id": "r1:pick", "pickable": True, "options": [{"id": "o1", "title": "llama.cpp + local model", "kind": "local", "cloud": False, "memory_basis": "needs 8 GiB", "limits": ["slow"], "explanation": "fits"},
           {"id": "o2", "title": "Claude CLI", "kind": "cli", "cloud": True}, {"id": "o3", "title": "API", "kind": "api", "cloud": True}]}
PLAN = {"plan_id": "p1", "plan_hash": HASH, "machine_id": "m1", "option_id": "o1", "sensitivity": "internal", "fit": {"fits": True}, "steps": [
        {"n": 1, "title": "Download", "kind": "download", "exact_command": "curl -L X", "writes": ["~/mas/a"], "bytes": 5_000_000, "undo": "rm ~/mas/a", "needs_owner_approval": True, "status": "pending"}],
        "verification": {"endpoint": "127.0.0.1:8080", "ok": False}}
RUN = {"run": {"id": "r1", "kind": "analysis", "node": "Lenovo", "status": "done"}, "events": [{"tag": "MODEL", "text": "chose probe"}, {"tag": "GUARD", "text": "ran it"}], "actors": {"MODEL": 16, "GUARD": 33, "SCRIPT": 1, "OWNER": 1}}
ALL = [("network", NET), ("machine", MACHINE), ("options", OPTIONS), ("plan", PLAN), ("run", RUN)]


class Scan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.loads: list[str] = []
        self.scripts = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("link", "iframe", "embed", "object", "audio", "video", "source", "img"):
            self.loads.append(tag)
        if tag == "script":
            self.scripts += 1
            if "src" in a:
                self.loads.append("script src")
        for k, v in a.items():
            if k in ("href", "src", "action", "poster", "srcset", "data") and v and not v.startswith("#"):
                self.loads.append(f"{tag}[{k}]")
            if k.startswith("on"):
                self.loads.append(f"{tag}[{k}] inline handler")


@pytest.mark.parametrize("h", ["panel", "openwebui", "mcp", "plain"])
@pytest.mark.parametrize("kind,data", ALL)
def test_a_card_in_any_host_loads_nothing_from_outside(kind, data, h):
    doc = host.document(cards.render(kind, data, "en", True), h)
    scan = Scan()
    scan.feed(doc)
    assert scan.loads == []
    assert not re.search(r"https?://|//[a-z0-9.-]+\.[a-z]{2,}|@import|url\(|fetch\(|XMLHttpRequest|WebSocket|importScripts|import\(", doc)  # no URL, no network call in any script
    assert "default-src 'none'" in doc and "connect-src" not in doc  # the document's own policy forbids what it does not do


def test_the_mcp_shell_speaks_only_the_apps_protocol():
    doc = host.document("", "mcp")
    for token in ("ui/initialize", "ui/notifications/initialized", "ui/notifications/tool-result", "ui/notifications/size-changed", "tools/call", "mas_answer"):
        assert token in doc
    assert "e.source!==parent" in doc  # it listens to its host only


def test_openwebui_embeds_report_their_height_and_mcp_embeds_report_their_size():
    assert "iframe:height" in host.document("<p>x</p>", "openwebui")
    assert "iframe:height" not in host.document("<p>x</p>", "mcp")
    assert "size-changed" in host.document("<p>x</p>", "mcp")


def test_an_unknown_host_is_refused():
    with pytest.raises(ValueError):
        host.document("x", "teams")


# ---- hostile text is only ever text ------------------------------------------------------------------------------------------------------------

def test_names_facts_and_findings_that_come_from_machines_and_models_are_escaped_everywhere():
    net = {"coordinator": {"name": HOSTILE}, "nodes": [{"id": HOSTILE, "name": HOSTILE, "os": HOSTILE, "kind": HOSTILE, "status": "online", "facts": [{"label": HOSTILE, "value": HOSTILE, "status": HOSTILE}]}]}
    machine = {"name": HOSTILE, "os": HOSTILE, "groups": {"system": [{"label": HOSTILE, "value": {HOSTILE: HOSTILE}, "status": HOSTILE}]}, "findings": [{"text": HOSTILE, "status": HOSTILE}]}
    opts = {"intent_id": HOSTILE, "pickable": True, "options": [{"id": HOSTILE, "title": HOSTILE, "kind": HOSTILE, "explanation": HOSTILE, "limits": [HOSTILE], "label": HOSTILE}]}
    plan = {"steps": [{"n": HOSTILE, "title": HOSTILE, "exact_command": HOSTILE, "writes": [HOSTILE], "undo": HOSTILE, "status": HOSTILE}], "option_id": HOSTILE, "machine_id": HOSTILE}
    run = {"run": {"kind": HOSTILE, "node": HOSTILE, "status": HOSTILE, "reason": HOSTILE}, "events": [{"tag": HOSTILE, "text": HOSTILE}], "actors": {}}
    for kind, data in (("network", net), ("machine", machine), ("options", opts), ("plan", plan), ("run", run)):
        frag = cards.render(kind, data, "en", True)
        assert "<img" not in frag and "<script" not in frag and "&lt;" in frag
        scan = Scan()
        scan.feed(frag)
        assert scan.loads == [] and scan.scripts == 0


def test_an_unknown_device_kind_is_drawn_as_a_plain_computer_never_guessed():
    d = {"nodes": [{"id": "x", "name": "X", "kind": "toaster", "status": "online"}]}
    assert cards.pix("desktop", 6) in cards.network(d)


# ---- the map ------------------------------------------------------------------------------------------------------------------------------------

def test_the_map_draws_each_machine_as_its_own_icon_and_marks_status_activity_and_locks():
    m = cards.network(NET, "en")
    for kind in ("laptop", "oldpc", "server", "desktop"):
        assert cards.pix(kind, 6) in m  # each machine is its own pixel symbol of the style set
    assert m.count('class="mas-node"') == 4 and m.count('<rect class="st-dot online"') == 2
    assert 'st-dot busy' in m and 'st-dot offline' in m
    assert 'mas-link route-local active' in m  # a running task animates its line
    assert 'route-cloud' in m and 'lock-badge' in m  # amber: it goes to the cloud; lock: confidential
    assert m.count("data-for=") == 4 and m.count(" hidden>") == 4  # each machine has its own card, closed until it is clicked
    assert "Lenovo" in m and "Windows 11" in m


def test_an_empty_network_says_so():
    assert "No paired machines" in cards.network({"nodes": []})


def test_a_machine_card_shows_the_evidence_state_of_every_fact():
    m = cards.machine(MACHINE, "en")
    assert m.count("st-tested") == 2 and "st-unknown" in m and "st-inferred" in m and "st-hypothesis" in m
    assert "tested" in m and "unknown" in m


def test_the_options_card_compares_with_symbols_and_labels_what_was_not_measured():
    o = cards.options(OPTIONS, "en")
    assert cards.pix("chip", 4) in o and cards.pix("terminal", 4) in o and cards.pix("key", 4) in o
    assert o.count("provisional, not measured here") == 3
    assert o.count('class="sym cloud"') == 2 and o.count('class="sym local"') == 1  # teal stays here, amber leaves
    assert o.count("data-choose=") == 3 and cards.options(OPTIONS, "en", actions=False).count("data-choose=") == 0  # a host that cannot call back shows no button


def test_the_plan_card_shows_the_exact_command_the_writes_the_size_and_the_undo():
    p = cards.plan(PLAN, "en")
    assert "curl -L X" in p and "~/mas/a" in p and "4.8 MB" in p and "rm ~/mas/a" in p and "Approved in the owner panel" in p


def test_the_run_card_counts_who_decided():
    r = cards.run(RUN, "en")
    assert "MODEL 16" in r and "GUARD 33" in r and "OWNER 1" in r and "CLAUDE-CODE 0" in r


@pytest.mark.parametrize("kind,data", ALL)
def test_every_card_has_a_text_fallback_that_names_what_the_card_shows(kind, data):
    txt = cards.fallback_text(kind, data, "en")
    assert txt.strip()
    must = {"network": ["Lenovo", "Old PC", "Box", "Desk"], "machine": ["Windows 11", "19.7", "CPU only"], "options": ["llama.cpp + local model", "Claude CLI", "API", "id: o1"], "plan": ["curl -L X", "rm ~/mas/a"], "run": ["MODEL 16", "analysis"]}[kind]
    for s in must:
        assert s in txt


def test_the_network_text_stands_alone_when_no_card_is_shown():
    """A chat may show no card at all: the words alone must say each machine's role, system, state, what it is doing and who acted, its resources and its AI, each with how it is known."""
    analysed = {**MACHINE, "groups": {**MACHINE["groups"], "hardware": [*MACHINE["groups"]["hardware"], {"key": "machine.model_path", "label": "Modelos", "value": "/Users/someone/models", "status": "tested"}]}}
    net = {"coordinator": {"name": "Mac"}, "nodes": [
        {"id": "a1", "name": "Lenovo", "os": "Windows 11", "arch": "x86_64", "status": "busy", "seen_s": 4, "activity": {"text": "lee la memoria", "actor": "MODEL"}, "machine": analysed, "analysis_at": "2026-10-30 18:00"},
        {"id": "b2", "name": "Box", "os": "Debian", "status": "offline"}]}
    txt = cards.fallback_text("network", net, "en")
    lenovo, box = txt.split("■ Lenovo")[1].split("■ Box")
    for s in ("paired machine", "Windows 11 (x86_64)", "busy", "4 s ago", "lee la memoria", "acted: MODEL", "RAM = 19.7 [tested]", "Runtimes = ollama [inferred]", "CPU only", "2026-10-30 18:00"):
        assert s in lenovo, s
    assert "Coordinator «Mac»" in txt and "keeps their record" in txt
    assert "offline" in box and "no signal yet" in box and "no stored analysis" in box and "idle" in box
    assert "/Users/someone" not in txt and "file paths are not included" in txt


def test_the_run_text_names_the_machine_and_the_latest_steps_with_who_did_them():
    txt = cards.fallback_text("run", RUN, "en")
    assert "Lenovo" in txt and "[MODEL] chose probe" in txt and "[GUARD] ran it" in txt


# ---- accessible colors -------------------------------------------------------------------------------------------------------------------------

def _lum(hex_: str) -> float:
    r, g, b = (int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5))
    f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4  # noqa: E731
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


@pytest.mark.parametrize("fg,bg", cards.CONTRAST_PAIRS)
def test_every_text_and_background_pair_of_the_cards_is_at_least_4_5_to_1(fg, bg):
    a, b = sorted((_lum(cards.PALETTE[fg]), _lum(cards.PALETTE[bg])), reverse=True)
    assert (a + 0.05) / (b + 0.05) >= 4.5, (fg, bg)


def test_the_scanner_does_catch_a_card_that_loads_something():
    for bad in ('<img src="x">', '<script src="a.js"></script>', '<a href="https://x.example">x</a>', '<div onclick="go()">x</div>', '<link rel="stylesheet" href="s.css">'):
        scan = Scan()
        scan.feed(bad)
        assert scan.loads, bad


@pytest.mark.parametrize("kind,data", ALL)
def test_a_card_uses_no_inline_style_so_it_also_works_under_the_owner_panels_strict_policy(kind, data):
    assert 'style="' not in cards.render(kind, data, "en", True) and "style='" not in cards.render(kind, data, "en", True)


def test_a_list_of_things_reads_as_their_names_not_as_their_fields():
    assert cards.fmt_value([{"id": "ollama", "name": "Ollama", "running": True}, {"name": "LM Studio"}]) == "Ollama, LM Studio"
