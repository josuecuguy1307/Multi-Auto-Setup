from envrec_node import setup


def _setup_step(params, ctx):
    return setup.run_step(params, params, params.action, ctx)  # runs a step without authorizing it


def _os_details(params, ctx):
    setup.run_step(params, params, "run", ctx)  # a read-only handler that writes
    return {}, []


ALLOWED = {"setup_step": _setup_step, "os_details": _os_details}


class Runner:
    async def on_task(self, payload, send):
        await send("task_update", {"state": "accepted"})  # accepts a write before authorizing it
