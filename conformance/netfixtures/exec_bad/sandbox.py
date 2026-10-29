import subprocess


class SandboxUnavailable(RuntimeError):
    pass


def backend_ready():
    return False


def run_script(path, payload):
    done = subprocess.run(["python3", str(path)], input=payload, capture_output=True)
    if not backend_ready():
        raise SandboxUnavailable("no backend")
    return done.stdout
