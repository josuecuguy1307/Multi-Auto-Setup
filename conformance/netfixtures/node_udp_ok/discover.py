import socket


def ask(dest, data):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(data, dest)
        return s.recvfrom(1200)
    finally:
        s.close()
