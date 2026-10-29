import asyncio


async def main(handle, host):
    a = await asyncio.start_server(handle, host, 7000)
    b = await asyncio.start_server(handle, host, 7001)
    return a, b
