"""Shared by the coordinator and the nodes: identity, signed envelopes, the pairing key exchange, the pinned-TLS contexts, framing, and the guard for protocols that code
walks (no model on the path). No server code lives here: whoever listens is the coordinator, and only the coordinator (docs/NETWORK_CONTRACT.md section 1)."""
