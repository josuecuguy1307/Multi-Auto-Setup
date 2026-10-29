import time

from envrec_net.envelope import Verifier


def open_message(msg, peers):
    if abs(time.time() - msg["ts"]) > 30:  # VIOLATION: a session that refuses by wall-clock time
        raise ValueError("stale_timestamp")
    return Verifier(peers).open(msg)
