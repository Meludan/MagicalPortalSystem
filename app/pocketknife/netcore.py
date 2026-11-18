"""
NetworkPocketknife core: transports (UDP, TCP client, TCP server, Serial, SSH) and
send/receive formatting helpers. No GUI code in here.

Every transport reports back through plain callbacks, called from worker threads:
    on_data(data: bytes, peer: str)
    on_status(msg: str)
    on_error(msg: str)
    on_closed()
    on_clients(list_of_peer_strings)      # TCP server only
"""
import base64
import hashlib
import os
import re
import select
import socket
import threading

# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------

_HEX_JUNK = re.compile(r"0x|0X|[\s,;:\-]")


def parse_hex(text: str) -> bytes:
    """'01 02 0A', '01020a', '0x01,0x02' -> bytes."""
    cleaned = _HEX_JUNK.sub("", text)
    if len(cleaned) % 2:
        raise ValueError("odd number of hex digits")
    try:
        return bytes.fromhex(cleaned)
    except ValueError:
        raise ValueError("not valid hex") from None


_ESC = {"r": b"\r", "n": b"\n", "t": b"\t", "0": b"\x00", "\\": b"\\",
        "e": b"\x1b", "a": b"\x07", "b": b"\x08"}
_ANGLE = re.compile(r"<([0-9A-Fa-f]{2})>")


def parse_ascii(text: str) -> bytes:
    """Text with escapes:  \\r \\n \\t \\0 \\e \\\\ \\xHH  and  <HH>  (e.g. <0D><0A>)."""
    out = bytearray()
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            nxt = text[i + 1]
            if nxt in ("x", "X") and i + 3 < n and re.fullmatch(r"[0-9A-Fa-f]{2}", text[i + 2:i + 4]):
                out += bytes([int(text[i + 2:i + 4], 16)])
                i += 4
                continue
            if nxt in _ESC:
                out += _ESC[nxt]
                i += 2
                continue
        if ch == "<":
            m = _ANGLE.match(text, i)
            if m:
                out.append(int(m.group(1), 16))
                i = m.end()
                continue
        out += ch.encode("utf-8")
        i += 1
    return bytes(out)


def fmt_hex(data: bytes) -> str:
    return " ".join(f"{b:02X}" for b in data)


def fmt_ascii(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else f"<{b:02X}>" for b in data)


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------

def _noop(*_a, **_k):
    pass


class Transport:
    def __init__(self, on_data=None, on_status=None, on_error=None,
                 on_closed=None, on_clients=None):
        self.on_data = on_data or _noop
        self.on_status = on_status or _noop
        self.on_error = on_error or self.on_status
        self.on_closed = on_closed or _noop
        self.on_clients = on_clients or _noop
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._closed_sent = False

    def _spawn(self, target, *args):
        t = threading.Thread(target=target, args=args, daemon=True)
        t.start()
        return t

    def _finish(self):
        with self._lock:
            if self._closed_sent:
                return
            self._closed_sent = True
        self.on_closed()

    @property
    def stopping(self):
        return self._stop.is_set()

    def _close_resources(self):
        pass

    def stop(self):
        self._stop.set()
        try:
            self._close_resources()
        finally:
            self._finish()


def _peer(addr):
    return f"{addr[0]}:{addr[1]}"


# ---------------------------------------------------------------- UDP -----

class UdpTransport(Transport):
    """UDP that listens on one port and can send from the same or a different port."""

    def start(self, local_ip, listen_port, remote_ip, remote_port,
              separate_send=False, send_port=0, broadcast=False,
              reply_to_sender=False):
        self.remote = (remote_ip, int(remote_port))
        self.reply_to_sender = reply_to_sender
        self.last_sender = None
        bind_ip = local_ip or "0.0.0.0"
        self.rx = self._make(bind_ip, int(listen_port), broadcast)
        self.socks = [self.rx]
        if separate_send:
            try:
                self.tx = self._make(bind_ip, int(send_port), broadcast)
            except Exception:
                self.rx.close()
                raise
            self.socks.append(self.tx)
        else:
            self.tx = self.rx
        lp = self.rx.getsockname()
        msg = f"UDP listening on {_peer(lp)}"
        if separate_send:
            msg += f", sending from port {self.tx.getsockname()[1]}"
        msg += f", default target {_peer(self.remote)}"
        self.on_status(msg)
        self._spawn(self._loop)

    @staticmethod
    def _make(ip, port, broadcast):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if broadcast:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # Windows: stop ICMP "port unreachable" from killing recvfrom()
        if hasattr(socket, "SIO_UDP_CONNRESET"):
            try:
                s.ioctl(socket.SIO_UDP_CONNRESET, False)
            except (OSError, ValueError):
                pass
        try:
            s.bind((ip, port))
        except OSError:
            s.close()
            raise
        return s

    def _loop(self):
        while not self.stopping:
            try:
                ready, _, _ = select.select(self.socks, [], [], 0.2)
            except (OSError, ValueError):
                break
            for s in ready:
                try:
                    data, addr = s.recvfrom(65535)
                except ConnectionResetError:
                    continue
                except OSError as e:
                    if not self.stopping:
                        self.on_error(f"Receive error: {e}")
                    continue
                self.last_sender = addr
                self.on_data(data, _peer(addr))
        self._finish()

    def send(self, data, dest=None):
        if dest is None:
            dest = self.last_sender if (self.reply_to_sender and self.last_sender) else self.remote
        self.tx.sendto(data, dest)
        return _peer(dest)

    def _close_resources(self):
        for s in getattr(self, "socks", []):
            try:
                s.close()
            except OSError:
                pass


# --------------------------------------------------------- TCP client -----

class TcpClientTransport(Transport):
    def start(self, host, port, local_port=0, timeout=5.0):
        self.sock = None
        self.peer = f"{host}:{port}"
        self._spawn(self._run, host, int(port), int(local_port or 0), timeout)

    def _run(self, host, port, local_port, timeout):
        self.on_status(f"Connecting to {host}:{port} ...")
        try:
            src = ("", local_port) if local_port else None
            s = socket.create_connection((host, port), timeout=timeout, source_address=src)
        except OSError as e:
            if not self.stopping:
                self.on_error(f"Connect failed: {e}")
            self._finish()
            return
        if self.stopping:
            s.close()
            return
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(0.3)
        self.sock = s
        self.peer = _peer(s.getpeername())
        self.on_status(f"Connected to {self.peer} (local {_peer(s.getsockname())})")
        while not self.stopping:
            try:
                data = s.recv(65536)
            except socket.timeout:
                continue
            except OSError as e:
                if not self.stopping:
                    self.on_error(f"Connection error: {e}")
                break
            if not data:
                self.on_status("Connection closed by remote host")
                break
            self.on_data(data, self.peer)
        try:
            s.close()
        except OSError:
            pass
        self.sock = None
        self._finish()

    def send(self, data):
        s = self.sock
        if not s:
            raise ConnectionError("not connected yet")
        s.sendall(data)
        return self.peer

    def _close_resources(self):
        s = self.sock
        if s:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            s.close()


# --------------------------------------------------------- TCP server -----

class TcpServerTransport(Transport):
    def start(self, bind_ip, port):
        self.clients = {}
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):          # Windows
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((bind_ip or "0.0.0.0", int(port)))
            srv.listen(32)
        except OSError:
            srv.close()
            raise
        srv.settimeout(0.3)
        self.srv = srv
        self.on_status(f"TCP server listening on {_peer(srv.getsockname())}")
        self._spawn(self._accept_loop)

    def _client_list(self):
        with self._lock:
            return list(self.clients)

    def _accept_loop(self):
        while not self.stopping:
            try:
                c, addr = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            c.settimeout(0.3)
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            peer = _peer(addr)
            with self._lock:
                self.clients[peer] = c
            self.on_status(f"Client connected: {peer}")
            self.on_clients(self._client_list())
            self._spawn(self._client_loop, peer, c)
        self._finish()

    def _client_loop(self, peer, c):
        reason = "disconnected"
        while not self.stopping:
            try:
                data = c.recv(65536)
            except socket.timeout:
                continue
            except OSError as e:
                reason = f"error: {e}"
                break
            if not data:
                break
            self.on_data(data, peer)
        with self._lock:
            self.clients.pop(peer, None)
        try:
            c.close()
        except OSError:
            pass
        if not self.stopping:
            self.on_status(f"Client {peer} {reason}")
            self.on_clients(self._client_list())

    def send(self, data, peers=None):
        with self._lock:
            targets = dict(self.clients) if peers is None else \
                {p: self.clients[p] for p in peers if p in self.clients}
        if not targets:
            raise ConnectionError("no clients connected" if peers is None else "selected client(s) not connected")
        errors = []
        for p, c in targets.items():
            try:
                c.sendall(data)
            except OSError as e:
                errors.append(f"{p}: {e}")
        if errors and len(errors) == len(targets):
            raise ConnectionError("; ".join(errors))
        if len(targets) == 1:
            return next(iter(targets))
        return f"{len(targets)} clients"

    def disconnect(self, peer):
        with self._lock:
            c = self.clients.get(peer)
        if c:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _close_resources(self):
        srv = getattr(self, "srv", None)
        if srv:
            srv.close()
        with self._lock:
            socks = list(self.clients.values())
            self.clients.clear()
        for c in socks:
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            c.close()
        self.on_clients([])


# ------------------------------------------------------------- Serial -----

def list_serial_ports():
    """[(device, description)]; empty list if pyserial is missing."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    ports = sorted(list_ports.comports(), key=lambda p: (len(p.device), p.device))
    return [(p.device, p.description or "") for p in ports]


class SerialTransport(Transport):
    def start(self, port, baud, bytesize=8, parity="N", stopbits=1,
              flow="none", dtr=True, rts=True):
        try:
            import serial
        except ImportError:
            raise RuntimeError("pyserial is not installed (pip install pyserial)")
        self._serial_mod = serial
        ser = serial.Serial()
        ser.port = port
        ser.baudrate = int(baud)
        ser.bytesize = int(bytesize)
        ser.parity = parity
        ser.stopbits = stopbits
        ser.timeout = 0.1
        ser.xonxoff = flow == "xonxoff"
        ser.rtscts = flow == "rtscts"
        ser.dtr = dtr
        if flow != "rtscts":
            ser.rts = rts
        ser.open()
        self.ser = ser
        self.port = port
        self.on_status(f"Opened {port} @ {baud} {bytesize}{parity}{stopbits}, flow={flow}")
        self._spawn(self._loop)

    def _loop(self):
        ser = self.ser
        while not self.stopping:
            try:
                data = ser.read(ser.in_waiting or 1)
            except Exception as e:  # SerialException, OSError
                if not self.stopping:
                    self.on_error(f"Serial error: {e}")
                break
            if data:
                self.on_data(data, self.port)
        try:
            ser.close()
        except Exception:
            pass
        self._finish()

    def send(self, data):
        self.ser.write(data)
        return self.port

    def set_dtr(self, on):
        self.ser.dtr = on

    def set_rts(self, on):
        self.ser.rts = on

    def _close_resources(self):
        ser = getattr(self, "ser", None)
        if ser:
            try:
                ser.cancel_read()
            except Exception:
                pass


# ---------------------------------------------------------------- SSH -----

def key_fingerprint(key) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class SshTransport(Transport):
    """Interactive shell over SSH (paramiko). ask_host_key(host, keytype, fingerprint) -> bool
    is called from the worker thread for unknown host keys and may block."""

    def start(self, host, port, username, password=None, key_file=None,
              cols=80, rows=24, known_hosts=None, ask_host_key=None):
        self.chan = None
        self.client = None
        self._spawn(self._run, host, int(port), username, password, key_file,
                    cols, rows, known_hosts, ask_host_key)

    def _run(self, host, port, username, password, key_file, cols, rows,
             known_hosts, ask_host_key):
        try:
            import paramiko
        except ImportError:
            self.on_error("paramiko is not installed (pip install paramiko)")
            self._finish()
            return

        client = paramiko.SSHClient()
        try:
            client.load_system_host_keys()
        except Exception:
            pass
        if known_hosts:
            os.makedirs(os.path.dirname(known_hosts), exist_ok=True)
            if not os.path.exists(known_hosts):
                open(known_hosts, "a").close()
            try:
                client.load_host_keys(known_hosts)
            except Exception as e:
                self.on_error(f"Could not read {known_hosts}: {e}")

        class AskPolicy(paramiko.MissingHostKeyPolicy):
            def missing_host_key(_self, cl, hostname, key):
                fp = key_fingerprint(key)
                if ask_host_key and ask_host_key(hostname, key.get_name(), fp):
                    cl.get_host_keys().add(hostname, key.get_name(), key)
                    if known_hosts:
                        try:
                            cl.save_host_keys(known_hosts)
                        except Exception:
                            pass
                else:
                    raise paramiko.SSHException("host key not accepted")

        client.set_missing_host_key_policy(AskPolicy())
        self.client = client
        self.on_status(f"Connecting to {username}@{host}:{port} ...")
        try:
            client.connect(host, port=port, username=username,
                           password=password or None,
                           key_filename=key_file or None,
                           timeout=10, banner_timeout=20, auth_timeout=30,
                           look_for_keys=True, allow_agent=True)
        except paramiko.BadHostKeyException as e:
            self.on_error("WARNING: the server's host key has CHANGED since you last connected. "
                          "This could be a man-in-the-middle attack, or the server was reinstalled. "
                          f"Expected {key_fingerprint(e.expected_key)}, got {key_fingerprint(e.key)}. "
                          "If the change is expected, remove the old entry from the known_hosts file.")
            self._abort(client)
            return
        except paramiko.AuthenticationException:
            self.on_error("Authentication failed (check username, password or key)")
            self._abort(client)
            return
        except Exception as e:
            if not self.stopping:
                self.on_error(f"Connection failed: {e}")
            self._abort(client)
            return

        try:
            tr = client.get_transport()
            if tr:
                tr.set_keepalive(30)
            chan = client.invoke_shell(term="xterm-256color", width=cols, height=rows)
        except Exception as e:
            self.on_error(f"Could not open shell: {e}")
            self._abort(client)
            return
        chan.settimeout(0.2)
        self.chan = chan
        self.on_status("Connected")
        while not self.stopping:
            try:
                data = chan.recv(32768)
            except socket.timeout:
                if chan.closed or (chan.exit_status_ready() and not chan.recv_ready()):
                    break
                continue
            except Exception as e:
                if not self.stopping:
                    self.on_error(f"Connection error: {e}")
                break
            if not data:
                break
            self.on_data(data, "")
        if not self.stopping:
            self.on_status("Session closed")
        self._abort(client)

    def _abort(self, client):
        try:
            client.close()
        except Exception:
            pass
        self._finish()

    def send(self, data):
        if not self.chan:
            raise ConnectionError("not connected yet")
        self.chan.sendall(data)
        return ""

    def resize(self, cols, rows):
        if self.chan:
            try:
                self.chan.resize_pty(width=cols, height=rows)
            except Exception:
                pass

    def _close_resources(self):
        if self.client:
            try:
                self.client.close()
            except Exception:
                pass
