"""Root cause of the two domains with no attempt in the v2 measurement (found in their run events): in one a search returned nothing that passes the checks and the model finished after that single search; in the
other the only page could not be read (HTTP 403, a rate limit) and a failed fetch still counted as 'research attempted'. Fixed in the guards (generic, thresholds are data): finishing is refused while no page was read
(until max_fetch_failures), and while the pages read offer nothing and fewer than min_searches_when_empty searches were read; the owner is told when the research could not be done. SIMULATED-LOCAL (scripted model)."""
from __future__ import annotations

import json

from acceptance_world import build
from test_acceptance_run import sc
from test_research import SEARCH, act, readme, run, serve, settled, start

EMPTY = json.dumps({"items": []}).encode()
Q1, Q2 = SEARCH + "grant+writing+agent", SEARCH + "grant+writing+tool"
HEAD = [act("read_known"), act("choose_type", task_type="text", why="Drafting."), '{"choice": "text"}']


def results(c, jid):
    return [(e.payload.get("tool"), e.payload.get("ok"), str(e.payload.get("error", ""))) for e in c.services.recommend_runs[jid]["store"].events() if e.type == "tool_result"]


def test_a_search_that_offers_nothing_is_not_enough_to_give_up_and_a_second_one_is_asked(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            r, sent = await start(c, host, HEAD + [act("fetch_source", url=Q1), act("finish_recommendation", summary="Nothing."), act("fetch_source", url=Q2), act("finish_recommendation", summary="Nothing.")],
                                  pages={Q1: EMPTY, Q2: EMPTY})
            job = await settled(c, sc(r)["job_id"])
            res = results(c, sc(r)["job_id"])
            assert res[3][:2] == ("finish_recommendation", False) and "research_thin" in res[3][2] and "2 searches are asked" in res[3][2] and "topic%3Agrant-writing" in res[3][2] and Q1 not in res[3][2].split("Not asked yet:")[1]
            assert res[5][:2] == ("finish_recommendation", True) and job.state == "awaiting_answer"
            fin = [e.payload for e in c.log.events("recommend_finished")][-1]
            assert fin["research"]["searches"] == 2 and fin["research"]["attempted"] == 0
        finally:
            await teardown()
    run(go())


def test_a_page_that_could_not_be_read_is_not_research_and_after_the_allowed_failures_the_owner_is_told(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            r, sent = await start(c, host, HEAD + [act("fetch_source", url=Q1), act("finish_recommendation", summary="x"), act("fetch_source", url=Q2), act("finish_recommendation", summary="x")], pages={})
            job = await settled(c, sc(r)["job_id"])
            res = results(c, sc(r)["job_id"])
            assert res[2][:2] == ("fetch_source", False) and "fetch_failed" in res[2][2]
            assert res[3][:2] == ("finish_recommendation", False) and "research_failed" in res[3][2] and "1 of 2 failures" in res[3][2]
            assert res[5][:2] == ("finish_recommendation", True)
            assert "could not be read" in job.text and "no research was done" in job.text  # the owner is told, not left to think the web had nothing
        finally:
            await teardown()
    run(go())


def test_a_search_that_did_offer_something_may_finish_after_one_search(tmp_path):
    async def go():
        w, c, nid, host, teardown = await build(tmp_path)
        try:
            from test_research import GOOD
            r, sent = await start(c, host, HEAD + [act("fetch_source", url=Q1), act("decline_leads", why="None fits."), act("finish_recommendation", summary="Looked.")], pages={Q1: GOOD})
            job = await settled(c, sc(r)["job_id"])
            res = results(c, sc(r)["job_id"])
            assert res[-1][:2] == ("finish_recommendation", True) and not any("research_thin" in x[2] for x in res) and job.state == "awaiting_answer"
        finally:
            await teardown()
    run(go())
