"""The one formatter for owner-facing text (envrec_harness/formatter.py): golden texts per intent kind and verbosity, the limits a user-facing result keeps, and what only the detailed view may show.
Pure: no coordinator, no network. The goldens are in tests/golden/formatter.json; regenerate them with `FORMATTER_GOLDEN=write pytest tests/test_formatter.py` and read the diff before committing."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from envrec_harness import cards, formatter as F
from envrec_harness.intents import numbered
from test_harness_cards import HASH, MACHINE, NET, OPTIONS, PLAN, RUN

GOLDEN = Path(__file__).parent / "golden" / "formatter.json"
OPTS = [{"id": "local", "label": "Local model"}, {"id": "api", "label": "API key", "detail": "cloud"}]
CMD = "curl -L --fail -o MAS_HOME/dl/x.tar.gz http://127.0.0.1:1/x && shasum -a 256 MAS_HOME/dl/x.tar.gz  (must print " + "0b" * 32 + ")"
RAW = "analysis stored\n{\"facts\": [1, 2, 3]}\nfingerprint " + "ab" * 32 + "\ndone"


def data(kind, d, **kw):
    return {"id": f"i-{kind}", "kind": "show_data", "actor": "GUARD", "sensitivity": "internal", "payload": {"data_kind": kind, "data": d}, "text_fallback": cards.fallback_text(kind, d, "en"), **kw}


def intents() -> dict[str, dict]:
    return {
        "network": data("network", NET),
        "machine": data("machine", MACHINE),
        "options": data("options", OPTIONS),
        "plan": data("plan", PLAN),
        "run": data("run", RUN),
        "ask_choice": {"id": "q1", "kind": "ask_choice", "actor": "GUARD", "sensitivity": "internal", "state": "pending", "payload": {"writes": False, "options": OPTS, "question": "q"},
                       "text_fallback": "Which model do you want here?\n" + numbered(OPTS) + "\nReply with the number or the id.\nintent_id for mas_answer: q1"},
        "confirm": {"id": "c1", "kind": "confirm", "actor": "GUARD", "sensitivity": "internal", "state": "pending",
                    "payload": {"writes": True, "summary": "Run step 1", "command": CMD, "undo": "rm MAS_HOME/dl/x.tar.gz", "plan_hash": HASH},
                    "text_fallback": f"Run step 1 of 2 on node1\nExact command: {CMD}\nHow to undo: rm MAS_HOME/dl/x.tar.gz\nplan {HASH}\nintent_id for mas_request_approval: c1"},
        "progress": {"id": "p1", "kind": "progress", "actor": "MODEL", "sensitivity": "internal", "payload": {}, "text_fallback": RAW},
        "text": {"id": "t1", "kind": "text", "actor": "GUARD", "sensitivity": "internal", "payload": {}, "text_fallback": "It could not start: the machine is offline."},
    }


def test_goldens_per_intent_kind_and_verbosity():
    got = {f"{k}/{lv}": F.render(i, lv) for k, i in intents().items() for lv in F.LEVELS}
    if os.environ.get("FORMATTER_GOLDEN") == "write":
        GOLDEN.parent.mkdir(exist_ok=True)
        GOLDEN.write_text(json.dumps(got, indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    want = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert len(got) == 27 and set(got) == set(want)
    assert {k: v for k, v in got.items() if v != want[k]} == {}


@pytest.mark.parametrize("level", ["brief", "normal"])
@pytest.mark.parametrize("kind", list(intents()))
def test_a_user_facing_result_has_no_raw_json_hash_or_escape_code_and_keeps_its_line_limit(kind, level):
    i = intents()[kind]
    text = F.render(i, level)
    assert F.violations(text, i, level) == [], text
    assert "\x1b" not in text and '{"' not in text


def test_the_detailed_view_keeps_the_original_text_with_its_hash_and_ids():
    i = intents()["confirm"]
    d = F.render(i, "detailed")
    assert HASH in d and "intent_id for mas_request_approval: c1" in d and d.endswith(i["text_fallback"])
    p = F.render(intents()["progress"], "detailed")
    assert '{"facts"' in p and "ab" * 32 in p


def test_outside_the_detailed_view_a_hash_is_its_first_eight_characters_and_a_line_of_json_is_left_out():
    p = F.render(intents()["progress"], "normal")
    assert "abababab…" in p and "ab" * 16 not in p and '"facts"' not in p and "analysis stored" in p and "done" in p


def test_an_approval_keeps_its_exact_command_even_when_a_checksum_is_in_it():
    for level in ("brief", "normal"):
        c = F.render(intents()["confirm"], level)
        assert CMD in c and "rm MAS_HOME/dl/x.tar.gz" in c  # what the owner approves is what runs
        assert HASH not in c and HASH.removeprefix("sha256:")[:8] + "…" in c  # only the plan's own hash is shortened


def test_a_decision_shows_numbered_options_without_ids_a_next_line_and_the_rule_for_relaying_it():
    t = F.render(intents()["ask_choice"], "normal")
    assert "1. Local model" in t and "2. API key (cloud)" in t and "[id:" not in t
    assert "Next: reply with the number of your choice." in t and "intent_id for mas_answer: q1" in t
    assert t.splitlines()[0].endswith("A decision for you")


def test_a_pending_approval_says_nothing_runs_until_the_owner_answers_and_a_resolved_one_says_nothing_of_the_kind():
    i = intents()["confirm"]
    assert "nothing runs until you do" in F.render(i, "normal")
    assert "Next:" not in F.render({**i, "state": "approved"}, "normal")


def test_a_view_that_is_cut_says_how_many_lines_it_cut_and_a_question_or_a_plan_is_never_cut():
    many = {"coordinator": {"name": "Mac"}, "nodes": [{"id": str(n), "name": f"M{n}", "os": "Linux", "status": "online"} for n in range(40)]}
    n = data("network", many)
    normal = F.render(n, "normal")
    assert "more line(s)" in normal and F.violations(normal, n, "normal") == []
    assert len(normal.splitlines()) < len(F.render(n, "detailed").splitlines())
    big = {"writes": False, "options": [{"id": str(k), "label": f"Option {k}"} for k in range(1, 41)]}
    q = {"id": "q", "kind": "ask_choice", "actor": "GUARD", "sensitivity": "internal", "state": "pending", "payload": big, "text_fallback": "Which?\n" + numbered(big["options"])}
    assert "40. Option 40" in F.render(q, "brief")
    steps = [{"n": k, "title": f"S{k}", "exact_command": f"cmd{k}", "writes": [], "undo": f"undo{k}", "needs_owner_approval": True, "status": "pending"} for k in range(1, 30)]
    plan = data("plan", {**PLAN, "steps": steps})
    assert "cmd29" in F.render(plan, "brief") and "undo29" in F.render(plan, "brief")


def test_the_checker_does_catch_a_text_that_breaks_the_rules():
    i = intents()["text"]
    assert F.violations("a " + "ab" * 20, i, "normal") == ["a hash outside the detailed view"]
    assert F.violations('x\n{"a": 1}', i, "normal") == ["raw JSON outside the detailed view"]
    assert F.violations("x\x1b[31mred", i, "brief") == ["an ANSI escape in owner-facing text"]
    assert len(F.violations("\n".join(f"line {n}" for n in range(60)), i, "normal")) == 1
    assert F.violations("a " + "ab" * 20, i, "detailed") == []


def test_every_title_has_a_symbol_and_the_confidential_ones_say_so_in_shape_and_word():
    for k, i in intents().items():
        if k == "text":
            continue  # one line of activity stays that line (the activity feed adds the actor itself)
        first = F.render(i, "normal").splitlines()[0]
        assert first and not first[0].isalnum(), (k, first)  # a mark leads the line
    lock = F.render({**intents()["network"], "sensitivity": "confidential"}, "normal").splitlines()[0]
    assert "▣ confidential" in lock
    withheld = F.render({"id": "w", "kind": "text", "actor": "GUARD", "sensitivity": "confidential", "payload": {"withheld": True}, "text_fallback": "Withheld: confidential"}, "normal")
    assert withheld.splitlines()[0].startswith("▣ confidential")


def test_one_line_of_activity_stays_one_line():
    assert F.render({"id": "a", "kind": "progress", "actor": "MODEL", "sensitivity": "internal", "payload": {}, "text_fallback": "node1 · node_gpu: ok, 2 facts"}, "normal") == "node1 · node_gpu: ok, 2 facts"


def test_a_service_tool_text_gets_a_mark_clean_lines_and_the_rule_for_relaying_but_a_failure_is_marked_as_one():
    ok = F.render_tool(True, "Profile saved.\n1. a\n2. b\nintent_id for mas_answer: q9", "normal")
    assert ok.splitlines()[0].endswith("Profile saved.") and "Next: reply with the number of your choice." in ok and "intent_id for mas_answer: q9" in ok
    bad = F.render_tool(False, "That is not one of the paired computers.", "normal")
    assert bad.startswith("✗ ") and F.render_tool(True, "x " + HASH, "detailed") == "x " + HASH
    assert F.level_of("loud") == "normal" and F.level_of("brief") == "brief"


# ---- the owner's setting, through the coordinator -----------------------------------------------------------------------------------------------

def test_the_owners_verbosity_changes_how_much_the_network_text_shows_and_nothing_else(tmp_path):
    import asyncio
    from envrec_coordinator.harness import CHANNELS
    from mas_world import coordinator
    CLAUDE = CHANNELS["claude-desktop"]

    async def go():
        async with coordinator(tmp_path) as c:
            prof = c.services.profile
            assert prof.get("verbosity") == "normal" and "verbosity" not in prof.missing()  # a presentation setting is not a first-run question
            seen = {}
            for lv in F.LEVELS:
                prof.set("verbosity", lv)
                i = c.harness.network_intent(CLAUDE)
                seen[lv] = i
            assert seen["normal"]["text_fallback"].startswith("∷ Your network") and "No paired machines yet." in seen["normal"]["text_fallback"]
            assert seen["detailed"]["text_fallback"].startswith("∷ Your network") and "Your network: 0" in seen["detailed"]["text_fallback"]  # the original text, under the same title
            assert {k: v["payload"] for k, v in seen.items()}["brief"] == seen["detailed"]["payload"]  # the data is the same at every level: only the words change
            r = c.harness.tool(CLAUDE, "profile_tool", {"action": "set", "field": "verbosity", "value": "loud"})
            assert r["ok"] is False and "one of: brief, normal, detailed" in r["text"]
            q = c.harness.tool(CLAUDE, "profile_tool", {"action": "ask", "field": "verbosity"})
            assert "1. Brief" in q["text"] and "2. Normal" in q["text"] and "3. Detailed" in q["text"]  # in chat it is a question with options
    asyncio.run(go())


def test_mas_config_shows_sets_and_refuses_the_verbosity_and_the_coordinator_reads_the_change(tmp_path, capsys):
    from envrec_coordinator.profile import Profile
    from envrec_mas import cli
    state = tmp_path / "s"
    assert cli.main(["--state-dir", str(state), "config"]) == 0 and "Verbosity: normal" in capsys.readouterr().out
    running = Profile(state / "coordinator" / "profile.json")  # a coordinator that is already up
    assert running.get("verbosity") == "normal"
    assert cli.main(["--state-dir", str(state), "config", "verbosity", "brief"]) == 0
    out = capsys.readouterr().out
    assert "Verbosity is now brief." in out and "Undo: mas config verbosity normal" in out
    assert running.get("verbosity") == "brief"  # no restart: the file changed, so it is read again
    assert cli.main(["--state-dir", str(state), "config", "verbosity", "loud"]) == 2 and "brief, normal, detailed" in capsys.readouterr().err
    assert cli.main(["--state-dir", str(state), "config", "volume", "3"]) == 2


def test_a_time_since_a_machine_was_seen_is_said_the_way_a_person_says_it_and_the_network_text_stays_compact():
    assert [F._ago(n) for n in (40, 600, 7200, 227443)] == ["40 s", "10 min", "2 h", "3 days"]
    net = {"coordinator": {"name": "Mac"}, "nodes": [{"id": "a", "name": "Lenovo", "os": "Windows 11", "status": "offline", "seen_s": 227443, "machine": MACHINE}]}
    text = F.render(data("network", net), "normal")
    assert "last seen 3 days ago" in text and "System" not in text and "227443" not in text and "**Hardware**" in text
    assert "**System**" in F.render(data("machine", MACHINE), "normal")  # the machine's own card keeps every group
