#!/usr/bin/env python3
"""One node of the discovery lab. A fixed, known role per container: mdns | ssdp | http | unknown | silent.

The lab's contents are known by construction, so what discovery should find is ground truth without anyone confirming it."""
import http.server
import socket
import struct
import sys
import threading
import time

ROLE = sys.argv[1]
IP = sys.argv[2]


def my_ip():
    return IP


def role_mdns():
    import zeroconf
    zc = zeroconf.Zeroconf(interfaces=[IP])
    host = "labprinter.local."
    zc.register_service(zeroconf.ServiceInfo("_ipp._tcp.local.", "Lab Printer._ipp._tcp.local.", addresses=[socket.inet_aton(IP)], port=631,
                                             properties={"ty": "Lab LaserJet", "deviceid": "AA:BB:CC:DD:EE:FF"}, server=host))
    zc.register_service(zeroconf.ServiceInfo("_zxq-lab._tcp.local.", "Lab Strange._zxq-lab._tcp.local.", addresses=[socket.inet_aton(IP)], port=31337,
                                             properties={}, server=host))
    threading.Event().wait()


def role_ssdp():
    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = (b'<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
                    b'<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType><friendlyName>Lab Media Renderer</friendlyName>'
                    b'<manufacturer>Lab Corp</manufacturer><modelName>LR-1</modelName><serialNumber>LAB-SECRET-0001</serialNumber><UDN>uuid:lab-0001</UDN>'
                    b'</device></root>')
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = http.server.ThreadingHTTPServer(("0.0.0.0", 8200), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", 1900))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, struct.pack("4s4s", socket.inet_aton("239.255.255.250"), socket.inet_aton(IP)))
    while True:
        data, addr = s.recvfrom(2048)
        if b"M-SEARCH" in data:
            for st in ("upnp:rootdevice", "urn:schemas-upnp-org:device:MediaRenderer:1"):
                msg = (f"HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\nEXT:\r\nLOCATION: http://{IP}:8200/desc.xml\r\nSERVER: Lab/1.0 UPnP/1.0 LabRenderer/1.0\r\n"
                       f"ST: {st}\r\nUSN: uuid:lab-0001::{st}\r\n\r\n").encode()
                s.sendto(msg, addr)


def role_http():
    class H(http.server.BaseHTTPRequestHandler):
        server_version = "LabHTTPd/1.0"
        sys_version = ""

        def log_message(self, *a):
            pass

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
    for port in (80, 8080):
        srv = http.server.ThreadingHTTPServer(("0.0.0.0", port), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    threading.Event().wait()


def role_unknown():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", 31337))
    srv.listen(8)
    while True:
        c, _ = srv.accept()
        try:
            c.sendall(b"ZXQ-PROTO v9 ready\r\n")
        finally:
            c.close()


def role_silent():
    while True:
        time.sleep(3600)


{"mdns": role_mdns, "ssdp": role_ssdp, "http": role_http, "unknown": role_unknown, "silent": role_silent}[ROLE]()
