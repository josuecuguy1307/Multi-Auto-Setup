import asyncio


async def main(handle, host, port, responder):
    loop = asyncio.get_running_loop()
    tcp = await asyncio.start_server(handle, host, port)  # the one LAN-facing TCP socket
    udp = await loop.create_datagram_endpoint(lambda: responder, local_addr=("0.0.0.0", 7444))  # the one optional discovery socket
    return tcp, udp
