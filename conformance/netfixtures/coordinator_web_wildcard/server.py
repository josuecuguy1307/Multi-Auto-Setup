import asyncio
from http.server import ThreadingHTTPServer


async def main(handle, host, port, page):
    node_side = await asyncio.start_server(handle, host, port)
    screen = ThreadingHTTPServer(("0.0.0.0", 8080), page)  # VIOLATION: the web screen bound to every interface
    return node_side, screen
