from envrec_node import envrun


def _environment_run(params, ctx):
    entry, mode = envrun.authorize(params, ctx["token"], ctx)
    return envrun.run_environment(params, entry, mode, ctx)


def _os_details(params, ctx):
    return {}, []


ALLOWED = {"environment_run": _environment_run, "os_details": _os_details}


class Runner:
    async def on_task(self, payload, send):
        if payload["task_type"] == "environment_run":
            try:
                envrun.authorize(payload, payload["token"], {})
            except Exception:
                return await send("task_update", {"state": "rejected"})
        await send("task_update", {"state": "accepted"})
