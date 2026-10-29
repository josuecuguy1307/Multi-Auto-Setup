import asyncio


async def main(handle, host, port):
    server = await asyncio.start_server(handle, host, port)
    async with server:
        await server.serve_forever()
