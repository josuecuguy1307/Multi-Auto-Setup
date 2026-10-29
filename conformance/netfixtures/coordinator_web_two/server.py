import asyncio
from http.server import ThreadingHTTPServer


async def main(handle, host, port, page):
    node_side = await asyncio.start_server(handle, host, port)
    a = ThreadingHTTPServer(("127.0.0.1", 8080), page)
    b = ThreadingHTTPServer(("::1", 8081), page)  # VIOLATION: a second loopback socket (at most one is allowed)
    return node_side, a, b
