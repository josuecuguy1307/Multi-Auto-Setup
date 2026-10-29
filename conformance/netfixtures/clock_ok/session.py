from envrec_net.session import SessionVerifier


def open_message(msg, verifier: SessionVerifier):
    return verifier.open(msg)  # freshness is the session id and the sequence number
