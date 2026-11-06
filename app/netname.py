"""
Friendly name for the control page, e.g.  http://PortalSystem:8080

* This PC        : a line in the Windows hosts file  ->  127.0.0.1  portalsystem
                   (needs one admin prompt; done by Setup or when the name changes)
* Other devices  : this module answers name look-ups on the local network
                     - LLMNR  (UDP 5355)  -> Windows PCs can use  http://portalsystem:8080
                     - mDNS   (UDP 5353)  -> phones, Macs, Windows use  http://portalsystem.local:8080
                   Only runs while the page is open to the network.

Pure standard library.
"""

import ipaddress
import os
import re
import socket
import struct
import subprocess
import tempfile
import threading

HOSTS = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "drivers", "etc", "hosts")
TAG = "# Magical Portal System"
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,62}$")

MDNS_ADDR, MDNS_PORT = "224.0.0.251", 5353
LLMNR_ADDR, LLMNR_PORT = "224.0.0.252", 5355


def valid_name(name):
    return bool(NAME_RE.match(name or "")) and not name.endswith("-")


# --------------------------------------------------------------------------- #
# hosts file (this PC)
# --------------------------------------------------------------------------- #

def hosts_has(name):
    try:
        with open(HOSTS, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split("#")[0].split()
                if len(parts) >= 2 and parts[0] == "127.0.0.1" and name.lower() in (p.lower() for p in parts[1:]):
                    return True
    except OSError:
        pass
    return False


def resolves_locally(name):
    try:
        return socket.gethostbyname(name) == "127.0.0.1"
    except OSError:
        return False


def request_hosts_entry(name):
    """Ask Windows (one UAC prompt) to point <name> at this PC. Returns (started, message)."""
    if os.name != "nt":
        return False, "Only needed on Windows."
    script = os.path.join(tempfile.gettempdir(), "magical_portal_hosts.ps1")
    ps = f"""
$h = '{HOSTS}'
$lines = @(Get-Content -LiteralPath $h -ErrorAction SilentlyContinue | Where-Object {{ $_ -notmatch '{TAG}' }})
$lines += '127.0.0.1    {name.lower()}    {TAG}'
Set-Content -LiteralPath $h -Value $lines -Encoding ASCII
ipconfig /flushdns | Out-Null
"""
    with open(script, "w", encoding="ascii") as f:
        f.write(ps)
    import ctypes
    params = f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{script}"'
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", params, None, 0)
    if rc <= 32:
        return False, "The admin prompt was cancelled - the name only works after allowing it."
    return True, "Windows asked for permission to add the name (check the taskbar if you don't see the prompt)."


# --------------------------------------------------------------------------- #
# network name responder (other devices)
# --------------------------------------------------------------------------- #

def _read_name(data, off):
    labels, jumped, end = [], False, None
    for _ in range(64):
        if off >= len(data):
            return None, len(data)
        ln = data[off]
        if ln == 0:
            off += 1
            break
        if ln & 0xC0 == 0xC0:
            ptr = struct.unpack("!H", data[off:off + 2])[0] & 0x3FFF
            if not jumped:
                end = off + 2
            jumped, off = True, ptr
            continue
        labels.append(data[off + 1:off + 1 + ln].decode("ascii", "replace"))
        off += 1 + ln
    return ".".join(labels).lower(), (end if jumped else off)


def _encode_name(name):
    out = b""
    for part in name.split("."):
        b = part.encode("ascii")
        out += bytes([len(b)]) + b
    return out + b"\x00"


class NameResponder:
    """Answers 'where is <name>?' on the LAN with this PC's address."""

    def __init__(self, get_name, get_adapters):
        self.get_name = get_name          # () -> "portalsystem"
        self.get_adapters = get_adapters  # () -> [{"ip":..., "prefix":...}]
        self._socks = []
        self._stop = threading.Event()
        self.error = ""

    def _my_ip_for(self, peer):
        adapters = [a for a in self.get_adapters() if a.get("ip") and not a.get("apipa")]
        try:
            p = ipaddress.ip_address(peer)
            for a in adapters:
                if a.get("prefix") is not None and p in ipaddress.ip_network(f"{a['ip']}/{a['prefix']}", strict=False):
                    return a["ip"]
        except ValueError:
            pass
        try:   # fall back to the adapter Windows would use to reach that device
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((peer, 9))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            return adapters[0]["ip"] if adapters else None

    def _open(self, group, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", port))
        joined = 0
        for a in self.get_adapters():
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                             socket.inet_aton(group) + socket.inet_aton(a["ip"]))
                joined += 1
            except OSError:
                pass
        if not joined:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                         socket.inet_aton(group) + socket.inet_aton("0.0.0.0"))
        s.settimeout(1.0)
        return s

    def start(self):
        for proto, group, port in (("llmnr", LLMNR_ADDR, LLMNR_PORT), ("mdns", MDNS_ADDR, MDNS_PORT)):
            try:
                s = self._open(group, port)
            except OSError as e:
                self.error += f"{proto}: {e}. "
                continue
            self._socks.append(s)
            threading.Thread(target=self._loop, args=(s, proto), daemon=True).start()
        return bool(self._socks)

    def stop(self):
        self._stop.set()
        for s in self._socks:
            try:
                s.close()
            except OSError:
                pass
        self._socks = []

    def _loop(self, sock, proto):
        while not self._stop.is_set():
            try:
                data, (peer, pport) = sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                reply = self._answer(data, peer, pport, proto)
                if reply:
                    pkt, dest = reply
                    sock.sendto(pkt, dest)
            except Exception:
                pass

    def _answer(self, data, peer, pport, proto):
        if len(data) < 12:
            return None
        qid, flags, qd, an, ns, ar = struct.unpack("!6H", data[:12])
        if flags & 0x8000 or qd == 0:          # a response, or no question
            return None
        name = (self.get_name() or "").lower()
        if not name:
            return None
        wanted = name if proto == "llmnr" else name + ".local"
        off = 12
        for _ in range(qd):
            qname, off = _read_name(data, off)
            if qname is None or off + 4 > len(data):
                return None
            qtype, qclass = struct.unpack("!HH", data[off:off + 4])
            off += 4
            if qname != wanted or qtype not in (1, 255):   # A or ANY
                continue
            ip = self._my_ip_for(peer)
            if not ip:
                return None
            rdata = socket.inet_aton(ip)
            if proto == "llmnr":
                q = _encode_name(qname) + struct.pack("!HH", qtype, 1)
                ans = _encode_name(qname) + struct.pack("!HHIH", 1, 1, 30, 4) + rdata
                hdr = struct.pack("!6H", qid, 0x8000, 1, 1, 0, 0)
                return hdr + q + ans, (peer, pport)
            # mDNS: legacy (non-5353 source) or unicast-response bit -> answer the asker directly
            unicast = pport != MDNS_PORT or (qclass & 0x8000)
            ans = _encode_name(qname) + struct.pack("!HHIH", 1, 0x8001, 120, 4) + rdata
            if pport != MDNS_PORT:   # legacy resolver: echo id + question
                q = _encode_name(qname) + struct.pack("!HH", qtype, 1)
                hdr = struct.pack("!6H", qid, 0x8400, 1, 1, 0, 0)
                return hdr + q + ans, (peer, pport)
            hdr = struct.pack("!6H", 0, 0x8400, 0, 1, 0, 0)
            return hdr + ans, ((peer, pport) if unicast else (MDNS_ADDR, MDNS_PORT))
        return None
