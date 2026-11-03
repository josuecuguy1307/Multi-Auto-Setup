"""The fourteen tasks of docs/ROUTER_ACCEPTANCE.md (frozen) and example 2, run SIMULATED-LOCAL through the MCP tools. Same rules as test_acceptance_run: the reasoning is checked (candidates, what each
filter cut and why, what the written rule chose), the code checks are the fixtures', and the code that the played models return runs only in the Docker sandbox of the coordinator (LINUX-CONTAINER)."""
from __future__ import annotations

import json

from acceptance_world import build, owner_yes, wait_job
from test_acceptance_run import E1, E2, E3A, E3B, actors, answers, create, docker, execute, filt, fx, measure, no_secret_anywhere, record, run, sc, text

PI = [{"id": "opencode", "name": "OpenCode", "set_up": True}, {"id": "pi", "name": "Pi", "set_up": True}]


def cut(d, step="sensitivity"):
    return filt(d, step)["values"]


# ============================================================ example 2: a public code task, graded in the sandbox of the coordinator ============================================================

@docker
def test_ex2_the_patch_comes_back_as_text_and_the_tests_run_only_in_the_coordinators_sandbox(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path, clis=PI)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "code", 9)
            answers(w, "ex2_cli_dryrun", "Add a --dry-run flag")
            r = await create(host, "ex2_cli_dryrun", "code", "public")
            d = sc(r)
            assert d["chosen"] == E1 + nid and "sandbox of this computer" in text(r) and f"no model-written code runs on lenovo" in text(r)
            assert d["left_out"][f"pi@{nid}"] == "needs a sandbox this computer does not have"  # an environment that must run code in a sandbox is not used where there is none
            j = await execute(host, r)
            assert sc(j)["state"] == "done" and sc(j)["attempts"][0]["graded"] is True, text(j)
            grades = [e for e in c.log.events("grade")]
            assert grades and grades[-1].payload["grader"] == "project_tests" and grades[-1].payload["passed"] is True
            runs = [e for e in c.log.events("task_created") if e.payload.get("type") == "environment_run"]
            assert all(e.payload["environment"] in ("llamacpp-local",) for e in runs[-1:])  # the Lenovo ran only the model's HTTP answer: no sandbox-requiring mode, no code
            record("ex2", explain=text(r), job=text(j), actors=actors(c), grade=grades[-1].payload)
        finally:
            await teardown()
    run(go())


# ============================================================ finance ================================================================================================================================

@docker
def test_f1_bank_reconciliation_is_confidential_so_only_the_measured_local_environment_can_take_it(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "numeric-analysis", 9)
            answers(w, "f1_bank", "Reconcile the bank statement")
            r = await create(host.local, "f1_bank", "numeric-analysis", "confidential")
            d = sc(r)
            assert cut(d)[E2 + nid] == "cloud" and cut(d)[E3B + nid] == "cloud" and cut(d)[E3A + nid] == "provisional" and d["chosen"] == E1 + nid
            j = await execute(host.local, r)
            assert sc(j)["state"] == "done" and sc(j)["attempts"][0]["graded"] is True
            assert not [x for x in w.script.calls if x[0] == "meta-model-api"]
            record("f1", explain=text(r), job=text(j), actors=actors(c))
        finally:
            await teardown()
    run(go())


@docker
def test_f1_variant_with_no_measured_local_route_is_refused_provisional_only(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            r = await create(host.local, "f1_bank", "numeric-analysis", "confidential")
            assert sc(r)["refusal"]["code"] == "provisional_only" and "intent_id" not in sc(r) and not w.script.calls
            record("f1_variant", explain=text(r))
        finally:
            await teardown()
    run(go())


@docker
def test_f2_internal_cash_flow_cuts_the_unapproved_providers_and_the_approval_opens_one_without_changing_the_winner(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "numeric-analysis", 9)
            answers(w, "f2_cashflow", "From this 12-month table")
            r = await create(host, "f2_cashflow", "numeric-analysis", "internal")
            d = sc(r)
            assert cut(d)[E2 + nid] == "provider_not_approved" and cut(d)[E3B + nid] == "provider_not_approved" and d["chosen"] == E1 + nid
            j = await execute(host, r)
            assert sc(j)["state"] == "done" and sc(j)["attempts"][0]["graded"] is True
            c.services.policy.approved_providers_for_internal = {"meta-model-api"}  # the owner approves the provider for internal work
            r2 = await create(host, "f2_cashflow", "numeric-analysis", "internal", check=False)
            d2 = sc(r2)
            assert E2 + nid in filt(d2, "sensitivity")["candidates_out"] and E3B + nid not in filt(d2, "sensitivity")["candidates_out"] and d2["chosen"] == E1 + nid
            record("f2", explain=text(r), approved_variant=text(r2), job=text(j), actors=actors(c))
        finally:
            await teardown()
    run(go())


@docker
def test_f3_journal_entries_balance_and_use_only_accounts_of_the_chart_and_stay_local(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "numeric-analysis", 9)
            answers(w, "f3_journal", "Turn these 20 transactions")
            r = await create(host.local, "f3_journal", "numeric-analysis", "confidential")
            assert sc(r)["chosen"] == E1 + nid
            j = await execute(host.local, r)
            assert sc(j)["state"] == "done" and sc(j)["attempts"][0]["graded"] is True
            entries = json.loads(sc(j)["output"])
            chart = json.loads(fx("f3_journal")["chart.json"])  # the independent verifier: debit = credit by construction, accounts from the chart, positive amounts
            assert len(entries) == 20 and all(e["debit"] in chart and e["credit"] in chart and e["debit"] != e["credit"] and e["amount"] > 0 for e in entries)
            assert not [x for x in w.script.calls if x[0] == "meta-model-api"]
            record("f3", explain=text(r), job=text(j), actors=actors(c))
        finally:
            await teardown()
    run(go())


@docker
def test_f4_loan_amortization_public_follows_the_rule_and_a_failed_exam_changes_the_choice(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "numeric-analysis", 9)
            answers(w, "f4_loan", "A loan of $10,000")
            r = await create(host, "f4_loan", "numeric-analysis", "public")
            assert sc(r)["chosen"] == E1 + nid
            j = await execute(host, r)
            assert sc(j)["state"] == "done" and sc(j)["attempts"][0]["graded"] is True and json.loads(sc(j)["output"])["payment"] == 470.73
            await measure(w, host, "llamacpp-local", "local-hardware", "numeric-analysis", 6)  # the same environment measured again, now failing
            r2 = await create(host, "f4_loan", "numeric-analysis", "public", check=False)
            assert cut(sc(r2), "admission_filter")[E1 + nid] == "not_admitted" and sc(r2)["chosen"] != E1 + nid
            record("f4", explain=text(r), job=text(j), after_failed_exam=text(r2), actors=actors(c))
        finally:
            await teardown()
    run(go())


@docker
def test_f5_ols_runs_only_in_the_coordinators_sandbox_and_matches_statsmodels(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "numeric-analysis", 9)
            answers(w, "f5_ols", "Sales on price and ads")
            r = await create(host, "f5_ols", "numeric-analysis", "public")
            j = await execute(host, r)
            assert sc(j)["state"] == "done" and sc(j)["attempts"][0]["graded"] is True, text(j)
            assert json.loads(fx("f5_ols")["expected.json"])["statsmodels"]
            bad = await create(host, "f5_ols", "numeric-analysis", "public")  # a regression that returns zeros fails the same check
            answers(w, "f5_ols", "Sales on price and ads", good="bad.txt")
            jb = await execute(host, bad)
            assert sc(jb)["attempts"][0]["graded"] is False
            record("f5", explain=text(r), job=text(j), actors=actors(c), grade=[e.payload for e in c.log.events("grade")][0])
        finally:
            await teardown()
    run(go())


# ============================================================ code ===================================================================================================================================

@docker
def test_c7_the_leap_year_fix_passes_the_tests_in_the_sandbox(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "code", 9)
            answers(w, "c7_leap", "test_dates.py has failing tests")
            r = await create(host, "c7_leap", "code", "public")
            j = await execute(host, r)
            assert sc(j)["state"] == "done" and sc(j)["attempts"][0]["graded"] is True
            record("c7", explain=text(r), job=text(j), actors=actors(c))
        finally:
            await teardown()
    run(go())


@docker
def test_c8_format_csv_passes_the_old_and_the_new_tests(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "code", 9)
            answers(w, "c8_csv", "Add --format csv")
            r = await create(host, "c8_csv", "code", "public")
            j = await execute(host, r)
            assert sc(j)["state"] == "done" and sc(j)["attempts"][0]["graded"] is True
            record("c8", explain=text(r), job=text(j), actors=actors(c))
        finally:
            await teardown()
    run(go())


@docker
def test_c9_a_failing_local_option_escalates_up_to_two_steps_and_a_third_failure_needs_the_owner(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "code", 9)
            await measure(w, host, "opencode", "opencode:local", "code", 8)
            await measure(w, host, "meta-model-api", "meta-model-api:api-key", "code", 8)
            answers(w, "c9_split", "Split this 600-line module", good="bad.txt", env="llamacpp-local")
            answers(w, "c9_split", "Split this 600-line module", good="bad.txt", env="opencode")
            answers(w, "c9_split", "Split this 600-line module", good="good.txt", env="meta-model-api")
            r = await create(host, "c9_split", "code", "public")
            assert sc(r)["chosen"] == E1 + nid
            j = await execute(host, r, timeout=240)
            at = sc(j)["attempts"]
            assert sc(j)["state"] == "done" and [a["route"] for a in at] == [E1 + nid, E3A + nid, E2 + nid] and [a["graded"] for a in at] == [False, False, True]
            assert len(c.log.events("escalation")) == 2
            # and when the last route fails too, there is no third escalation and the owner is warned
            answers(w, "c9_split", "Split this 600-line module", good="bad.txt", env="meta-model-api")
            r2 = await create(host, "c9_split", "code", "public")
            j2 = await execute(host, r2, timeout=240)
            assert sc(j2)["state"] == "exhausted" and len(sc(j2)["attempts"]) == 3 and "needs you" in c.log.events("owner_notice")[-1].payload["text_fallback"]
            record("c9", explain=text(r), job=text(j), attempts=at, exhausted=text(j2), actors=actors(c))
        finally:
            await teardown()
    run(go())


@docker
def test_c10_a_confidential_script_goes_to_a_measured_local_route_and_the_sandbox_less_lenovo_is_not_asked_to_run_code(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path, clis=PI)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "code", 9)
            answers(w, "c10_invoices", "invoices.py counts")
            r = await create(host.local, "c10_invoices", "code", "confidential")
            d = sc(r)
            assert d["left_out"][f"pi@{nid}"] == "needs a sandbox this computer does not have"
            assert cut(d)[E2 + nid] == "cloud" and cut(d)[E3B + nid] == "cloud" and cut(d)[E3A + nid] == "provisional" and d["chosen"] == E1 + nid
            assert "the sandbox of this computer" in text(r) and "no model-written code runs on lenovo" in text(r)
            j = await execute(host.local, r)
            assert sc(j)["state"] == "done" and sc(j)["attempts"][0]["graded"] is True
            assert not [x for x in w.script.calls if x[0] == "meta-model-api"]
            record("c10", explain=text(r), job=text(j), actors=actors(c))
        finally:
            await teardown()
    run(go())


# ============================================================ data and documents ======================================================================================================================

def test_d11_extraction_internal_stays_local_and_matches_the_schema_and_values(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "extraction", 9)
            answers(w, "d11_extraction", "Extract supplier, date")
            r = await create(host, "d11_extraction", "extraction", "internal")
            d = sc(r)
            assert cut(d)[E2 + nid] == "provider_not_approved" and d["chosen"] == E1 + nid
            j = await execute(host, r)
            assert sc(j)["state"] == "done" and sc(j)["attempts"][0]["graded"] is True and "output" not in sc(j)  # internal content does not go to a cloud chat in full: only the summary does
            local_view = await host.local.tool("mas_job", job_id=sc(r)["task_id"])
            assert len(json.loads(sc(local_view)["output"])) == 15
            record("d11", explain=text(r), job=text(j), actors=actors(c))
        finally:
            await teardown()
    run(go())


def test_d12_cleaning_gives_the_expected_rows_with_no_duplicates_and_iso_dates(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "data-cleaning", 9)
            answers(w, "d12_cleaning", "Clean this CSV")
            r = await create(host, "d12_cleaning", "data-cleaning", "public")
            j = await execute(host, r)
            rows = sc(j)["output"].splitlines()
            assert sc(j)["state"] == "done" and sc(j)["attempts"][0]["graded"] is True
            assert len(rows) == 31 and len({x.split(",")[1] for x in rows[1:]}) == 30 and all(x.split(",")[2][4] == "-" for x in rows[1:])
            record("d12", explain=text(r), job=text(j), actors=actors(c))
        finally:
            await teardown()
    run(go())


def test_d13_a_long_public_report_overflows_the_local_context_and_escalates_to_the_api(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "summarize", 9)
            await measure(w, host, "meta-model-api", "meta-model-api:api-key", "summarize", 8)
            answers(w, "ex4_long", "Write a one-page brief")
            r = await create(host, "ex4_long", "summarize", "public")
            assert sc(r)["chosen"] == E1 + nid
            j = await execute(host, r)
            at = sc(j)["attempts"]
            assert sc(j)["state"] == "done" and at[0]["state"] == "failed" and at[1]["route"] == E2 + nid and at[1]["graded"] is True
            nxt = await host.tool("mas_explain_route", task_id=sc(r)["task_id"])
            assert sc(nxt)["chosen"].startswith(("llamacpp-local", "meta-model-api", "opencode"))
            record("d13", explain=text(r), job=text(j), attempts=at, actors=actors(c))
        finally:
            await teardown()
    run(go())


# ============================================================ boards: example 9 and task 6 =============================================================================================================

def director_for(plan):
    """A scripted director (the MODEL of the board): it reads the view and answers one action from the menu it is shown. `plan` lists the subtasks to post; the rest follows the menu."""
    state = {"posted": 0, "verified": set()}

    def menu_ids(view, name):
        line = next((ln for ln in view.splitlines() if ln.startswith(f"- {name}(")), None)
        return line.split("one of ")[1].rstrip("]").split(", ") if line and "one of " in line else []

    def director(view):
        if state["posted"] < len(plan) and "- post_subtask(" in view:
            state["posted"] += 1
            return {"action": "post_subtask", "args": {k: v for k, v in plan[state["posted"] - 1].items() if k != "verify"}}  # "verify" is this script's own note, not an argument the board knows
        if "- submit_plan" in view:
            return {"action": "submit_plan", "args": {}}
        for tid in menu_ids(view, "request_verification"):
            if tid not in state["verified"] and plan[int(tid.split("-")[1]) - 2].get("verify"):
                state["verified"].add(tid)
                return {"action": "request_verification", "args": {"subtask_id": tid}}
        acc = menu_ids(view, "accept_result")
        if acc:
            return {"action": "accept_result", "args": {"subtask_id": acc[0]}}
        if "- finish(" in view:
            return {"action": "finish", "args": {"summary": "done"}}
        return {"action": "escalate_to_owner", "args": {"question": "I am stuck"}}
    return director


PHOTOS_CHECK = "check(rename_target('IMG_1.jpg', '2026-03-14'), '2026-03-14_IMG_1.jpg')\ncheck(rename_target('a.png', '2025-01-02'), '2025-01-02_a.png')\n"
PHOTOS_GOOD = "```python\ndef rename_target(name, date):\n    return f'{date}_{name}'\n```"


async def board_flow(host, c, w, goal, sensitivity, plan, *, via=None, timeout=240):
    via = via or host
    c.services.director_factory = lambda bid: director_for(plan)
    r = await via.tool("mas_board_start", goal=goal, sensitivity=sensitivity)
    assert sc(r)["ok"], text(r)
    bid = sc(r)["board_id"]
    shown = None
    for _ in range(200):
        shown = await via.tool("mas_board_show", board_id=bid)
        if sc(shown)["pending"] or sc(shown)["state"] not in ("running",):
            break
        await asyncio.sleep(0.1)
    return bid, shown


import asyncio  # noqa: E402


def test_ex9_a_board_plans_routes_checks_and_asks_the_owner_once(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path, second=True)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "code", 9, machine="lenovo")
            await measure(w, host, "llamacpp-local", "local-hardware", "text", 9, machine="lenovo")
            w.script.answers.insert(0, (lambda e, a, p: "Do not rewrite it" in p or "VERIFIED or REJECTED" in p or p.startswith("You are verifying"), "VERIFIED: the result is correct"))
            w.script.answers.append((lambda e, a, p: "rename" in p.lower() and "function" in p.lower(), PHOTOS_GOOD))
            w.script.answers.append((lambda e, a, p: "tests" in p.lower(), "def test_rename(): assert True"))
            plan = [{"task_type": "code", "input_ref": "Write a function rename_target(name, date) that returns date_name. Reply with python code only (a function).", "expected": "a function", "test_ref": "run_tests:" + PHOTOS_CHECK, "verify": True},
                    {"task_type": "text", "input_ref": "Write tests for the photo renamer. Reply with test functions.", "expected": "tests", "test_ref": "contains:def test_"}]
            bid, shown = await board_flow(host, c, w, "Build a small CLI that renames photos by date, with tests", "public", plan)
            assert sc(shown)["pending"] and sc(shown)["pending"][0]["kind"] == "decompose" and "Waiting for you" in text(shown)
            did = sc(shown)["pending"][0]["decision_id"]
            r = await host.tool("mas_board_approve", board_id=bid, decision_id=did)
            await owner_yes(host, sc(r)["intent_id"])
            for _ in range(600):
                shown = await host.tool("mas_board_show", board_id=bid)
                if sc(shown)["state"] not in ("running", "waiting_for_owner"):
                    break
                await asyncio.sleep(0.2)
            b = c.services.boards[bid]["board"]
            subs = {t["task_id"]: t for t in b.tasks.values() if t["task_id"] != c.services.boards[bid]["root"]}
            code_t = next(t for t in subs.values() if t["task_type"] == "code" and not t["verifies"])
            ver = [t for t in subs.values() if t["verifies"]]
            assert code_t["status"] == "done" and code_t["route"] is not None
            assert len([e for e in b.log.entries() if e.type == "board.decision_answered" and e.payload.get("kind", "decompose") == "decompose"]) == 1  # one approval of the plan
            assert ver and ver[0]["route"] and any(s["step"] == "exclusion" for s in ver[0]["route"]["rule_trace"])  # verified elsewhere: the producer's machine is excluded by the board
            assert ver[0]["route"]["route"]["machine_id"] == w.second and code_t["route"]["route"]["machine_id"] == nid
            grades = [e.payload for e in c.log.events("grade")]
            assert any(g["grader"] == "run_tests" and g["passed"] for g in grades)  # the code check ran in the sandbox of the coordinator
            # a write that is not in the approved plan is blocked by the guard, with a typed reason
            entry = c.services.envs["llamacpp-local"]
            from envrec_net import envrun as E
            rec = c.tasks.start_environment_run(nid, "board-not-in-the-plan", entry, {"environment_id": "llamacpp-local", "task_type": "text", "sensitivity": "public", "mode": "chat", "workspace_id": "w9", "recipe_sha256": E.recipe_sha256(entry),
                                                                                            "input_text": "x", "endpoint": "127.0.0.1:8080", "model_id": "m", "access_id": "local-hardware"}, {})
            assert rec.status == "rejected" and rec.reason == "no_owner_origin"
            record("ex9", board=text(shown), actors=actors(c), verification_trace=[s["step"] for s in ver[0]["route"]["rule_trace"]])
        finally:
            await teardown()
    run(go())


async def finish_board(host, c, bid, *, owner_host=None, rounds=600):
    """Approve what waits for the owner (each in his own dialog) until the board stops or nothing waits."""
    owner_host = owner_host or host
    approved = []
    for _ in range(rounds):
        shown = await owner_host.tool("mas_board_show", board_id=bid)
        d = sc(shown)
        if d["pending"]:
            p = d["pending"][0]
            r = await owner_host.tool("mas_board_approve", board_id=bid, decision_id=p["decision_id"])
            await owner_yes(owner_host, sc(r)["intent_id"])
            approved.append(p)
            await asyncio.sleep(0.3)
            continue
        if d["state"] not in ("running",):
            return shown, approved
        await asyncio.sleep(0.2)
    raise AssertionError("the board never stopped: " + text(shown))


@docker
def test_f6_the_month_end_close_board_stays_local_every_subtask_is_checked_and_the_report_is_consistent(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "numeric-analysis", 9)
            await measure(w, host, "llamacpp-local", "local-hardware", "text", 9)
            bank, cash, jour = fx("f1_bank"), fx("f2_cashflow"), fx("f3_journal")
            net_total = sum(json.loads(cash["expected.json"])["net"])
            n_diffs, n_entries = len(json.loads(bank["expected.json"])), len(json.loads(jour["expected.json"]))
            report = f"Month-end close. Net cash flow {net_total}. {n_diffs} reconciliation differences. {n_entries} journal entries."
            w.script.answers.append((lambda e, a, p: p.startswith("Reconcile the bank statement"), bank["good.txt"]))
            w.script.answers.append((lambda e, a, p: p.startswith("Turn these 20 transactions"), jour["good.txt"]))
            w.script.answers.append((lambda e, a, p: p.startswith("From this 12-month table"), cash["good.txt"]))
            w.script.answers.append((lambda e, a, p: p.startswith("Write the one-page"), report))
            plan = [{"task_type": "numeric-analysis", "input_ref": bank["input.txt"], "expected": "the differences", "test_ref": "equals:" + json.loads(bank["check.json"])["value"]},
                    {"task_type": "numeric-analysis", "input_ref": jour["input.txt"], "expected": "the entries", "test_ref": "equals:" + json.loads(jour["check.json"])["value"]},
                    {"task_type": "numeric-analysis", "input_ref": cash["input.txt"], "expected": "the cash flow", "test_ref": "equals:" + json.loads(cash["check.json"])["value"]},
                    {"task_type": "text", "input_ref": "Write the one-page report of the close", "expected": "a report", "test_ref": f"regex:(?=.*{net_total})(?=.*{n_diffs} reconciliation)(?=.*{n_entries} journal)"}]
            bid, shown = await board_flow(host.local, c, w, "Month-end close", "confidential", plan)
            shown, approved = await finish_board(host.local, c, bid)
            b = c.services.boards[bid]["board"]
            subs = [t for t in b.tasks.values() if t["task_id"] != c.services.boards[bid]["root"] and not t["verifies"]]
            assert len(subs) == 4 and all(t["status"] == "done" and t["sensitivity"] == "confidential" for t in subs), text(shown)
            assert [p["kind"] for p in approved] == ["decompose"]  # one approval for the whole plan
            decisions = [e.payload["route"]["environment_id"] for e in c.log.events("route_decision")]
            assert decisions and set(decisions) == {"llamacpp-local"} and not [x for x in w.script.calls if x[0] == "meta-model-api"]  # every route of every subtask stayed on a measured local environment
            got = [json.loads(c.services.outputs.of_task(t["task_id"]).text) for t in subs[:3]]  # the verified outputs of reconcile, journal, cash flow
            derived = {str(len(got[0])), str(len(got[1])), str(sum(got[2]["net"]))}  # what a checker computes from them: the report's three figures
            written = {x for x in __import__("re").findall(r"-?\d+", report)}
            assert written == derived, (written, derived)  # every number in the report is computed from a verified subtask output
            assert text(await host.tool("mas_board_show", board_id=bid)).lstrip("◈ ").startswith("This board is confidential")  # a cloud chat is shown nothing of it
            record("f6", board=text(shown), actors=actors(c))
        finally:
            await teardown()
    run(go())


@docker
def test_ex9_variant_after_two_failures_the_board_asks_the_owner_before_the_next_escalation(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            c.services.board_preapproved = 1  # the owner's draft allows one escalation without asking: the second one is the warning
            await measure(w, host, "llamacpp-local", "local-hardware", "code", 9)
            await measure(w, host, "opencode", "opencode:local", "code", 8)
            await measure(w, host, "meta-model-api", "meta-model-api:api-key", "code", 8)
            w.script.answers.append((lambda e, a, p: "rename_target" in p and e == "meta-model-api", PHOTOS_GOOD))
            w.script.answers.append((lambda e, a, p: "rename_target" in p, "```python\ndef rename_target(name, date):\n    return name\n```"))
            plan = [{"task_type": "code", "input_ref": "Write a function rename_target(name, date) that returns date_name. Reply with python code only.", "expected": "a function", "test_ref": "run_tests:" + PHOTOS_CHECK}]
            bid, shown = await board_flow(host, c, w, "Rename photos", "public", plan)
            shown, approved = await finish_board(host, c, bid)
            assert "Escalation limit: a failed subtask may move to another route 1 time(s) without asking you" in text(shown)
            kinds = [p["kind"] for p in approved]
            assert kinds == ["decompose", "escalate_beyond_preapproved"], kinds
            assert "step 2" in approved[1]["summary"] or "2" in approved[1]["summary"]
            b = c.services.boards[bid]["board"]
            sub = next(t for t in b.tasks.values() if t["task_id"] != c.services.boards[bid]["root"])
            assert sub["status"] == "done" and sub["escalation_step"] == 2
            record("ex9_warning", board=text(shown), approved=approved, actors=actors(c))
        finally:
            await teardown()
    run(go())


# ============================================================ multi-model: task 14 =====================================================================================================================

def test_m14_three_local_models_are_measured_one_at_a_time_and_the_recommendation_uses_measured_numbers_only(tmp_path):
    async def go():
        from acceptance_world import set_server
        w, c, nid, host, teardown = await build(tmp_path, server=False)
        try:
            models = {"model-a-8b-q4": 9, "model-b-3b-q4": 7, "model-c-13b-q4": 10}  # three models the node serves one at a time, with different skill
            for m, k in models.items():
                set_server(c, nid, up=True, model=m)
                w.script.model_accuracy[(m, "text")] = k
                r = await host.tool("mas_run_admission", environment="llamacpp-local", task_type="text", access="local-hardware")
                assert sc(r)["ok"], text(r)
                await owner_yes(host, sc(r)["intent_id"])
                j = await wait_job(host, sc(r)["job_id"], until=("done", "aborted", "refused", "declined"), timeout=120)
                assert sc(j)["state"] == "done", text(j)
            set_server(c, nid, up=True, model="model-d-7b-q4")  # a fourth model is being served now and has never been measured
            m = await host.tool("mas_measurements", task_type="text")
            d = sc(m)
            ranked = [r["route"].split("|")[1] for r in d["rows"]]
            assert ranked == ["model-c-13b-q4", "model-a-8b-q4", "model-b-3b-q4"]  # highest measured pass rate first; the 7/10 model is below the mark
            assert d["recommendation"].split("|")[1] == "model-c-13b-q4"
            assert [r["state"] for r in d["rows"]] == ["measured", "measured", "not_admitted"] and not any(r["fresh"] is False for r in d["rows"])
            assert any("model-d-7b-q4" in k for k in d["unmeasured"]) and "Not measured (listed, never ranked)" in text(m)
            seen = {n for n in __import__("re").findall(r"\d+/\d+|\d+\.\d+ s", text(m))}
            assert {f"{p}/10" for p in (10, 9, 7)} <= seen  # every figure in the answer is a measured one
            record("m14", measurements=text(m), actors=actors(c))
        finally:
            await teardown()
    run(go())


def test_a_board_on_a_single_machine_cannot_verify_elsewhere_and_says_so_with_a_typed_refusal(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)  # one paired machine
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "code", 9)
            w.script.answers.append((lambda e, a, p: "rename_target" in p, PHOTOS_GOOD))
            plan = [{"task_type": "code", "input_ref": "Write a function rename_target(name, date) that returns date_name. Reply with python code only.", "expected": "a function", "test_ref": "run_tests:" + PHOTOS_CHECK, "verify": True}]
            bid, shown = await board_flow(host, c, w, "Rename photos", "public", plan)
            shown, approved = await finish_board(host, c, bid)
            b = c.services.boards[bid]["board"]
            ver = [t for t in b.tasks.values() if t["verifies"]]
            assert ver and ver[0]["route"] is None and ver[0]["route_refusal"]["code"] == "no_candidate"  # not a second environment on the same machine: the producer's machine and environment are both excluded
            assert "excluded" in json.dumps(ver[0]["route_refusal"]["details"])
        finally:
            await teardown()
    run(go())
