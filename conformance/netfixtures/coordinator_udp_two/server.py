import asyncio


async def main(handle, host, port, responder):
    loop = asyncio.get_running_loop()
    tcp = await asyncio.start_server(handle, host, port)
    a = await loop.create_datagram_endpoint(lambda: responder, local_addr=("0.0.0.0", 7444))
    b = await loop.create_datagram_endpoint(lambda: responder, local_addr=("0.0.0.0", 7445))  # VIOLATION: a second UDP socket
    return tcp, a, b
