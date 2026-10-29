import asyncio
import socket


def listen_for_queries():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("", 7444))  # VIOLATION: a node bound to a fixed UDP port
    return s


async def answer_everyone(loop, proto):
    return await loop.create_datagram_endpoint(lambda: proto, local_addr=("0.0.0.0", 7444))  # VIOLATION: a node that answers
