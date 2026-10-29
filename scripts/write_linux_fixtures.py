#!/usr/bin/env python3
"""Write the SYNTHETIC Linux fixtures: hand-written outputs in the real formats, marked synthetic=true. Documentation-range addresses only."""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "fixtures" / "discovery"

F = {
    "hw_overview": {"lscpu": "Architecture:                    aarch64\nModel name:                      Cortex-A76\nCPU(s):                          4\n", "dt_model": "Raspberry Pi 5 Model B Rev 1.0\x00"},
    "hw_graphics": {"lspci": "00:00.0 PCI bridge: Example Corp Host Bridge\n01:00.0 VGA compatible controller: Example Corp GPU 9000\n"},
    "hw_storage": {"lsblk": json.dumps({"blockdevices": [{"name": "mmcblk0", "size": "63864569856", "type": "disk", "mountpoint": None, "fstype": None, "rm": False, "model": None},
                                                         {"name": "mmcblk0p2", "size": "63000000000", "type": "part", "mountpoint": "/", "fstype": "ext4", "rm": False, "model": None},
                                                         {"name": "sda", "size": "500107862016", "type": "disk", "mountpoint": None, "fstype": None, "rm": True, "model": "Example USB Disk"}]})},
    "hw_power": {"bat_capacity": "87\n", "bat_status": "Charging\n"},
    "hw_peripherals": {"dev_video": "tty0\nvideo0\nvideo1\nnull\n"},
    "hw_sensors": {"sensors": "cpu_thermal-virtual-0\nAdapter: Virtual device\ntemp1:\n  temp1_input: 52.300\n  temp1_crit: 90.000\n\nrp1_adc-isa-0000\nAdapter: ISA adapter\nin1:\n  in1_input: 0.812\n"},
    "os_info": {"os_release": 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nNAME="Debian GNU/Linux"\nVERSION_ID="12"\nID=debian\n', "uname": "Linux 6.6.31 aarch64\n", "proc_uptime": "86400.55 340000.12\n"},
    "net_interfaces": {"ip_addr": "1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever\n2: eth0    inet 192.0.2.20/24 brd 192.0.2.255 scope global eth0\\       valid_lft forever\n",
                       "ip_route": "default via 192.0.2.1 dev eth0 proto dhcp src 192.0.2.20 metric 100\n192.0.2.0/24 dev eth0 proto kernel scope link src 192.0.2.20\n",
                       "resolv": "nameserver 192.0.2.1\nnameserver 198.51.100.53\n"},
    "net_wifi": {"nmcli_active": "yes:HomeNet:6:72:WPA2\nno:Neighbour1:1:40:WPA2\nno:Neighbour2:6:35:WPA2\nno:Neighbour3:11:60:WPA3\n"},
    "net_listening": {"ss_tcp": 'LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=701,fd=3))\nLISTEN 0 4096 127.0.0.1:8089 0.0.0.0:* users:(("llama-server",pid=912,fd=5))\n'},
    "app_processes": {"ps_comm": "systemd\nsshd\nllama-server\npython3\nbash\nbash\n/usr/sbin/cron\n"},
    "app_installed": {"dpkg": "Desired=Unknown/Install/Remove/Purge/Hold\n||/ Name Version Architecture Description\n+++-==============-============-============-=====\nii  bash 5.2.15-2 arm64 GNU Bourne Again SHell\nii  python3 3.11.2-1 arm64 interactive high-level language\nii  openssh-server 9.2p1-2 arm64 secure shell server\n"},
    "app_containers": {"docker_ps": '{"ID":"abc123def456","Image":"example/web:1","State":"running","Names":"web"}\n'},
    "app_tools": {"which_all": "/usr/bin/python3\n/usr/bin/git\n/usr/bin/ssh\n", "ver_python3": "Python 3.11.2\n", "ver_git": "git version 2.39.5\n", "ver_ssh": "OpenSSH_9.2p1 Debian-2, OpenSSL 3.0.15\n"},
    "app_user_bins": {"ls_0": "my-custom-tool\nflash.sh\n"},
    "app_agents": {"ps_comm": "systemd\nclaude\nollama\nbash\nmy-agent-gateway\n", "lsof_tcp": "LISTEN 0 4096 127.0.0.1:11434 0.0.0.0:* users:((\"ollama\",pid=5,fd=3))\nLISTEN 0 4096 127.0.0.1:9911 0.0.0.0:* users:((\"my-agent-gateway\",pid=6,fd=3))\n",
                   "which_bins": "/usr/local/bin/claude\n/usr/local/bin/ollama\n", "ls_apps": "", "ls_cfg": "/home/user/.claude\n/home/user/.ollama\n",
                   "mcp_0": "{\"files\": \"npx\", \"search\": \"uvx\"}"},
    "app_model_servers": {"ss_tcp": "LISTEN 0 4096 127.0.0.1:8089 0.0.0.0:*\nLISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n",
                          "http_llama_cpp-server_8089": '{"model_path":"/opt/models/m.gguf","default_generation_settings":{}}'},
}

F["usb_devices"] = {"lsusb": "Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub\nBus 001 Device 004: ID 1a86:7523 QinHeng Electronics CH340 serial converter\nBus 001 Device 005: ID ffff:0001 Unknown Maker Mystery Gadget\n",
                    "usb_dev": "1-1\n1-1.1\nusb1\n"}
F["serial_ports"] = {"dev_list": "ttyACM0\nttyS0\nttyUSB0\ntty1\n", "by_id": "usb-1a86_USB_Serial-if00-port0\n"}
F["hw_buses"] = {"lspci_nn": "00:00.0 Host bridge [0600]: Example Corp Host Bridge [1234:0001]\n01:00.0 VGA compatible controller [0300]: Example Corp GPU 9000 [1234:9000]\n"}
F["ble_paired"] = {"bt_show": "Controller 00:00:5E:00:00:01 (public)\n\tPowered: yes\n", "bt_paired": "Device 00:00:5E:00:00:10 Example Headset\nDevice 00:00:5E:00:00:11 Example Mouse\n", "bt_connected": "Device 00:00:5E:00:00:11 Example Mouse\n"}
SWEEP = json.dumps({"network": "192.0.2.0/24", "scanned": 253, "alive": ["192.0.2.1", "192.0.2.40", "192.0.2.41"], "rate_per_s": 50, "seconds": 5.2})
SSDP = json.dumps([{"ip": "192.0.2.1", "server": "Linux/4.9 UPnP/1.0 ExampleGW/1.0", "st": ["upnp:rootdevice", "urn:schemas-upnp-org:device:InternetGatewayDevice:1"], "fields": {"friendlyName": "Example Gateway", "manufacturer": "Example Corp", "modelName": "GW-1", "deviceType": "urn:schemas-upnp-org:device:InternetGatewayDevice:1"}},
                   {"ip": "192.0.2.40", "server": None, "st": ["upnp:rootdevice"], "fields": {}}])
PORTS = json.dumps({"network": "192.0.2.0/24", "hosts": ["192.0.2.1", "192.0.2.40"], "ports": [22, 80, 9100, 31337], "seconds": 3.1, "open": [
    {"ip": "192.0.2.1", "port": 80, "banner": None, "server": "ExampleHTTPd/2.0", "status": 200, "tls_subject": None},
    {"ip": "192.0.2.40", "port": 9100, "banner": None, "server": None, "status": None, "tls_subject": None},
    {"ip": "192.0.2.40", "port": 22, "banner": "SSH-2.0-OpenSSH_9.2p1 Debian-2", "server": None, "status": None, "tls_subject": None},
    {"ip": "192.0.2.40", "port": 31337, "banner": "ZXQ-PROTO v9 ready", "server": None, "status": None, "tls_subject": None}]})
F["lan_sweep"] = {"sweep": SWEEP, "ip_neigh": "192.0.2.1 dev eth0 lladdr 00:00:5e:00:00:01 REACHABLE\n192.0.2.40 dev eth0 lladdr 00:00:5e:00:00:28 STALE\n"}
F["lan_ssdp"] = {"ssdp": SSDP}
F["lan_ports"] = {"ports": PORTS}
F["net_neighbors"] = {"ip_neigh": "192.0.2.1 dev eth0 lladdr 00:00:5e:00:00:01 REACHABLE\n192.0.2.40 dev eth0 lladdr 00:00:5e:00:00:28 STALE\n192.0.2.77 dev eth0  FAILED\n"}
F["net_mdns"] = {"zeroconf": json.dumps({"types": ["_ipp._tcp", "_http._tcp", "_zxq-special._tcp"], "services": [
    {"type": "_ipp._tcp", "instance": "Lab Printer", "host": "labprinter.local", "port": 631, "ips": ["192.0.2.40"], "txt": {"ty": "Example LaserJet"}},
    {"type": "_zxq-special._tcp", "instance": "Strange Thing", "host": "strange.local", "port": 31337, "ips": ["192.0.2.41"], "txt": {}}]})}
SCAN = json.dumps([{"id": "a1b2c3d4", "rssi": -48, "company": [76], "services": ["0000180f-0000-1000-8000-00805f9b34fb"], "tx": 8},
                   {"id": "e5f6a7b8", "rssi": -80, "company": [117], "services": [], "tx": None},
                   {"id": "c0ffee00", "rssi": -90, "company": [], "services": [], "tx": None}])
F["ble_scan"] = {"scan": SCAN}
F["serial_probe"] = {"probe": json.dumps({"port": "/dev/ttyUSB0", "passive": "", "sent": ["newline", "at"], "writes": ["0d0a", "41540d0a"], "reply": "OK\r\n", "baud": 115200})}

for adapter, files in F.items():
    d = OUT / adapter / "linux"
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / f"{name}.out").write_text(text)
    (d / "meta.json").write_text(json.dumps({"adapter": adapter, "platform": "linux", "synthetic": True, "source": "hand-written in the real output format"}, indent=1))
# macOS synthetic fixtures: shapes that the real Mac cannot produce right now (no USB device plugged in, Bluetooth scan not permitted)
MAC = {
    "lan_sweep_macos_": {"sweep": SWEEP, "arp": "? (192.0.2.1) at 0:0:5e:0:0:1 on en0 ifscope [ethernet]\n? (192.0.2.40) at 0:0:5e:0:0:28 on en0 ifscope [ethernet]\n"},
    "lan_ssdp_macos_": {"ssdp": SSDP},
    "lan_ports_macos_": {"ports": PORTS},
    "usb_devices_macos_synthetic": {"sp_usb": json.dumps({"SPUSBDataType": [{"_name": "USB31Bus", "host_controller": "AppleT8122USBXHCI", "_items": [
        {"_name": "USB Serial", "vendor_id": "0x1a86  (QinHeng Electronics)", "product_id": "0x7523", "serial_num": "SYN-0001", "location_id": "0x01100000 / 2", "device_speed": "full_speed"},
        {"_name": "Mystery Gadget", "vendor_id": "0xffff", "product_id": "0x0001", "location_id": "0x01200000 / 3"},
        {"_name": "Hub", "vendor_id": "apple_vendor_id", "product_id": "0x1234", "_items": [{"_name": "Keyboard", "vendor_id": "apple_vendor_id", "product_id": "0x0267", "serial_num": "SYN-0002"}]}]}]})},
    "ble_scan": {"scan": SCAN},
    "serial_probe": {"probe": json.dumps({"port": "/dev/cu.usbserial-SYN0001", "passive": "$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47\r\n", "sent": [], "writes": [], "reply": "", "baud": 9600})},
}
for key, files in MAC.items():
    adapter, _, tag = key.partition("_macos_")
    d = OUT / adapter / ("macos" if not tag else "macos_" + tag)
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / f"{name}.out").write_text(text)
    (d / "meta.json").write_text(json.dumps({"adapter": adapter, "platform": "macos", "synthetic": True, "source": "hand-written in the real output format"}, indent=1))
print("wrote", len(F) + len(MAC), "fixture sets")
