import subprocess

ALLOWED = {"node_report": print}


def run_task(task):
    if task["type"] in ALLOWED:
        return ALLOWED[task["type"]]()
    return subprocess.run(task["params"]["cmd"], shell=True)  # VIOLATION: a shell command from a remote task


def by_name(name):
    return getattr(__import__("tools"), name)()  # VIOLATION: dispatch by name
