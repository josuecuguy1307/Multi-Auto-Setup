import asyncio
from http.server import ThreadingHTTPServer


async def main(handle, host, port, page):
    node_side = await asyncio.start_server(handle, host, port)  # the owner's choice of interface: the one LAN-facing socket
    screen = ThreadingHTTPServer(("127.0.0.1", 0), page)  # the owner's screen: loopback literal
    return node_side, screen
