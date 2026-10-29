import asyncio
from http.server import ThreadingHTTPServer

WEB_HOST = "127.0.0.1"


async def main(handle, host, port, page):
    node_side = await asyncio.start_server(handle, host, port)
    screen = ThreadingHTTPServer((WEB_HOST, 0), page)  # a module constant that is a loopback literal still reads as one
    return node_side, screen
