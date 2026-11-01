from envrec_node import setup


def _setup_step(params, ctx):
    plan, step = setup.authorize(params, ctx["token"], ctx["recipes"])
    return setup.run_step(plan, step, params.action, ctx)


def _os_details(params, ctx):
    return {}, []


ALLOWED = {"setup_step": _setup_step, "os_details": _os_details}


class Runner:
    async def on_task(self, payload, send):
        if payload["task_type"] in ("setup_step",):
            try:
                setup.authorize(payload, payload["token"], {})
            except Exception:
                return await send("task_update", {"state": "rejected"})
        await send("task_update", {"state": "accepted"})
