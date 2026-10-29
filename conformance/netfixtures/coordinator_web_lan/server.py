import asyncio
from http.server import ThreadingHTTPServer


async def main(handle, host, port, page, web_host):
    node_side = await asyncio.start_server(handle, host, port)
    screen = ThreadingHTTPServer((web_host, 8080), page)  # VIOLATION: the web screen bound to a host the code does not pin to loopback
    return node_side, screen
