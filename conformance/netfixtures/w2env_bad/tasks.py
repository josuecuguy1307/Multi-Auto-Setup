from envrec_node import envrun


def _environment_run(params, ctx):
    return envrun.run_environment(params, {}, {}, ctx)


def _os_details(params, ctx):
    return envrun.run_environment(params, {}, {}, ctx)


ALLOWED = {"environment_run": _environment_run, "os_details": _os_details}


class Runner:
    async def on_task(self, payload, send):
        await send("task_update", {"state": "accepted"})
