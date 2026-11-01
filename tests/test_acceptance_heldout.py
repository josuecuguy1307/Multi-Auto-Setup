"""The generality check of docs/ROUTER_ACCEPTANCE.md section 3, held out: an environment, an access and a task type that exist nowhere in the repository are added as DATA ONLY (a catalog file and a suite file
in a temporary folder), and the examples that concern them are run again through the MCP tools. No code is changed to make them work; if one needed a code edit, that is the finding."""
from __future__ import annotations

import copy
import json

import yaml

from acceptance_world import ENVS, MODEL, SENTINEL, build, wait_job
from test_acceptance_run import actors, answers, create, execute, filt, measure, no_secret_anywhere, record, run, sc, text

NEW_TYPE = "classification"


def held_out_catalog():
    envs = copy.deepcopy(ENVS)
    mode = {"name": "no_tools", "confinement": "none_needed", "argv": ["run", "--text", "{prompt}"]}
    for e in envs.values():  # the new task type, as data: the type and its mode in every environment that can serve it
        e["task_types"].append(NEW_TYPE)
        e["adapter"]["modes"][NEW_TYPE] = copy.deepcopy(e["adapter"]["modes"]["text"])
        for a in e["accesses"]:
            if a.get("adapter", {}).get("modes"):
                a["adapter"]["modes"][NEW_TYPE] = copy.deepcopy(a["adapter"]["modes"]["text"])
    envs["goose-cli"] = {  # another CLI harness, a catalog row with nothing else
        "id": "goose-cli", "title": "A second harness CLI (held-out example)", "subtype": "own_harness", "cloud": False, "task_types": ["text", "summarize", NEW_TYPE], "os": ["darwin", "linux", "windows"],
        "needs": {"sandbox": False, "docker": False, "gui": False}, "license": {"spdx": "unknown", "note": "an example row for the generality check"}, "upstream": {"kind": "vendor_page", "url": "https://example.test"},
        "automated_use": "n/a", "installed_by_us": False, "labels": {},
        "accesses": [{"id": "goose:local", "kind": "local", "cost_class": "local", "plan_group": None, "env_names": []},
                     {"id": "goose:remote", "kind": "api_key", "cost_class": "api", "plan_group": None, "env_names": ["GOOSE_API_KEY"], "cloud": True, "model": "some-remote-model",
                      "adapter": {"needs_model_endpoint": False, "env_pass": ["GOOSE_API_KEY"]}}],
        "adapter": {"kind": "cli", "exe": {"darwin": "goose", "linux": "goose", "windows": "goose"}, "prompt_via": "arg", "timeout_s": 600, "needs_model_endpoint": True, "env_pass": [], "output": {"kind": "stdout"}, "env_set": {},
                    "modes": {"text": dict(mode), "summarize": dict(mode), NEW_TYPE: dict(mode)}}}
    envs["meta-model-api"]["accesses"].append({"id": "meta-model-api:key-b", "kind": "api_key", "cost_class": "api", "plan_group": None, "env_names": ["MODEL_API_KEY_B"], "cloud": True, "signed_in_check": None,
                                              "adapter": {"auth": {"env_name": "MODEL_API_KEY_B"}}})  # a second key name for an existing environment: an access with its own credential name, nothing else
    return envs


def classification_suite(root):
    tasks = [{"id": f"classification-{i:02d}", "title": f"classify {w}", "prompt": f"Classify '{w}' as fruit or vegetable. Reply with one word, lowercase.", "grader": {"kind": "exact", "expect": a}, "reference": a, "timeout_s": 30,
              "source": {"author": "project", "license": "project license (pending)"}}
             for i, (w, a) in enumerate([("apple", "fruit"), ("carrot", "vegetable"), ("pear", "fruit"), ("leek", "vegetable"), ("plum", "fruit"), ("onion", "vegetable"), ("grape", "fruit"), ("kale", "vegetable"),
                                         ("mango", "fruit"), ("beet", "vegetable")], 1)]
    d = root / "classification-v1"
    d.mkdir(parents=True)
    (d / "suite.yaml").write_text(yaml.safe_dump({"suite_id": "classification-v1", "task_type": NEW_TYPE, "version": 1, "tasks": tasks}, sort_keys=False))


def test_held_out_a_new_environment_a_new_access_and_a_new_task_type_enter_as_data_and_the_examples_still_hold(tmp_path, monkeypatch):
    async def go():
        from envrec_coordinator.admission import suite as SU
        root = tmp_path / "suites"
        root.mkdir()
        for p in SU.DATA_ROOT.iterdir():
            (root / p.name).mkdir()
            (root / p.name / "suite.yaml").write_bytes((p / "suite.yaml").read_bytes())
        classification_suite(root)
        monkeypatch.setattr(SU, "DATA_ROOT", root)
        monkeypatch.setattr(SU.load_suite, "__defaults__", (root,))  # the suite folder is a parameter of the loader: only the data moves
        envs = held_out_catalog()
        w, c, nid, host, teardown = await build(tmp_path / "w", envs=envs, clis=[{"id": "opencode", "name": "OpenCode", "set_up": True}, {"id": "goose", "name": "Goose", "set_up": True}])
        try:
            await measure(w, host, "llamacpp-local", "local-hardware", "summarize", 9)
            answers_note = None
            # example 1 (public): the new environment is a candidate, and measured-first keeps it out of the way until it is measured
            r = await host.tool("mas_explain_route", task_type="summarize", sensitivity="public")
            d = sc(r)
            assert f"goose-cli|{MODEL}|goose:local|{nid}" in d["candidates"] and f"goose-cli|some-remote-model|goose:remote|{nid}" not in d["candidates"]  # the remote access needs GOOSE_API_KEY
            assert d["left_out"][f"goose-cli via goose:remote@{nid}"] == "access missing: GOOSE_API_KEY"
            assert filt(d, "admission_filter")["values"][f"goose-cli|{MODEL}|goose:local|{nid}"] == "unmeasured" and d["chosen"].startswith("llamacpp-local")
            # example 3 (confidential): the new environment is cut or kept by the same sensitivity rule
            k = await host.local.tool("mas_explain_route", task_type="summarize", sensitivity="confidential")
            assert filt(sc(k), "sensitivity")["values"][f"goose-cli|{MODEL}|goose:local|{nid}"] == "provisional" and sc(k)["chosen"].startswith("llamacpp-local")
            # example 5 with the second key name: only that access is affected
            from envrec_node import credentials as C
            C.set("MODEL_API_KEY_B", SENTINEL)
            await c.services.refresh_credentials()
            both = sc(await host.tool("mas_explain_route", task_type="text", sensitivity="public"))
            assert f"meta-model-api|muse-spark-1.3|meta-model-api:key-b|{nid}" in both["candidates"] and f"meta-model-api|muse-spark-1.3|meta-model-api:api-key|{nid}" in both["candidates"]
            r = await host.tool("mas_remove_access", node="lenovo", access="meta-model-api:key-b")
            j = sc(r)["job_id"]
            import asyncio
            for _ in range(100):
                if c.services.jobs[j].intent_id:
                    break
                await asyncio.sleep(0.1)
            await host.tool("mas_request_approval", intent_id=c.services.jobs[j].intent_id)
            assert sc(await wait_job(host, j, until=("done", "failed", "declined")))["state"] == "done"
            after = sc(await host.tool("mas_explain_route", task_type="text", sensitivity="public"))
            assert f"meta-model-api|muse-spark-1.3|meta-model-api:key-b|{nid}" not in after["candidates"] and f"meta-model-api|muse-spark-1.3|meta-model-api:api-key|{nid}" in after["candidates"]
            assert after["left_out"][f"meta-model-api via meta-model-api:key-b@{nid}"] == "access missing: MODEL_API_KEY_B"
            # a task type that exists in no code: a suite file and a catalog entry
            w.script.answers.append((lambda e, a, p: p.startswith("Classify '"), "fruit"))
            await measure(w, host, "llamacpp-local", "local-hardware", NEW_TYPE, 10)
            t = await host.tool("mas_create_task", text="Classify 'apple' as fruit or vegetable. Reply with one word, lowercase.", task_type=NEW_TYPE, sensitivity="public", check={"kind": "equals", "value": "fruit"})
            assert sc(t)["chosen"] == f"llamacpp-local|{MODEL}|local-hardware|{nid}" and sc(t)["evidence"] == "measured here 10/10"
            j = await execute(host, t)
            assert sc(j)["state"] == "done" and sc(j)["attempts"][0]["graded"] is True
            no_secret_anywhere(c, tmp_path, r, after)
            record("heldout", environment=text(r), confidential=text(k), after_second_key_removed=text(await host.tool("mas_explain_route", task_type="text", sensitivity="public")), new_type_task=text(t), actors=actors(c))
        finally:
            await teardown()
    run(go())
