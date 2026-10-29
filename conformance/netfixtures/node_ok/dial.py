import socket
import ssl


def dial_the_coordinator(host, port, context: ssl.SSLContext):
    raw = socket.create_connection((host, port), timeout=10)
    return context.wrap_socket(raw, server_hostname=None)
