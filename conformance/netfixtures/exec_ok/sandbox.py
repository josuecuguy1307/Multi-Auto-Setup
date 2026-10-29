import subprocess


class SandboxUnavailable(RuntimeError):
    pass


def backend_ready():
    return False


def run_script(path, payload):
    if not backend_ready():
        raise SandboxUnavailable("no backend")
    done = subprocess.run(["sandbox-exec", str(path)], input=payload, capture_output=True)
    return done.stdout
