import socket
from http.server import ThreadingHTTPServer


def open_a_port():
    s = socket.socket()
    s.bind(("0.0.0.0", 9000))
    s.listen()
    return s


def also_serve(handler):
    ThreadingHTTPServer(("127.0.0.1", 0), handler).serve_forever()
