def report():
    return {}


def analysis():
    return {}


ALLOWED = {"node_report": report, "machine_analysis": analysis}


def run_task(task):
    fn = ALLOWED.get(task["type"])
    return fn() if fn else {"status": "rejected"}
