"""
REST engine for NetworkPocketknife: HTTP client with device-specific login handling,
device profiles (Generic, Crestron VC-4, Crestron DM NVX), placeholders and cURL export.
No GUI code in here.
"""
import glob
import json
import os
import re
import threading
import time
import xml.dom.minidom
from urllib.parse import urlsplit

try:
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except ImportError:            # reported nicely by the GUI
    requests = None

AUTH_TYPES = [
    ("none", "None"),
    ("basic", "Basic (user + password)"),
    ("digest", "Digest (user + password)"),
    ("bearer", "Bearer token"),
    ("header", "Token in header"),
    ("cresnext", "Crestron device login (NVX / CresNext)"),
]
BODY_TYPES = [("none", "None"), ("json", "JSON"), ("multipart", "Form (multipart)"),
              ("form", "Form (url-encoded)"), ("raw", "Raw text")]

# ------------------------------------------------------------ placeholders

PH = re.compile(r"\$\{([^:|}{]+?)(?::([a-z]+))?(?:\|([^}]*))?\}")


def find_placeholders(*texts):
    """-> ordered list of dicts {name, kind, choices}; kind in text/file/nvxstream/choice."""
    seen, out = set(), []
    for t in texts:
        for m in PH.finditer(t or ""):
            name, kind, choices = m.group(1).strip(), m.group(2) or "", m.group(3)
            if name in seen:
                continue
            seen.add(name)
            if choices is not None:
                kind = kind or "choice"
            out.append({"name": name, "kind": kind or "text",
                        "choices": [c.strip() for c in choices.split(",")] if choices else []})
    return out


def fill_placeholders(text, values, json_escape=False):
    def rep(m):
        v = str(values.get(m.group(1).strip(), ""))
        return json.dumps(v)[1:-1] if json_escape else v
    return PH.sub(rep, text or "")


# ------------------------------------------------------------ helpers

def parse_header_lines(text):
    headers = {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise ValueError(f"header line needs 'Name: value' -> {line!r}")
        k, v = line.split(":", 1)
        headers[k.strip()] = v.strip()
    return headers


def parse_form_lines(text):
    """'key=value' per line; 'key=@C:\\path\\file' attaches a file. -> [(key, value, is_file)]"""
    out = []
    for line in (text or "").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"form line needs 'name=value' -> {line!r}")
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("@") and len(v) > 1:
            out.append((k, v[1:].strip().strip('"'), True))
        else:
            out.append((k, v, False))
    return out


def pretty_body(content: bytes, content_type: str):
    """-> (kind, text) where kind is json/xml/text/image/binary."""
    ct = (content_type or "").lower()
    if ct.startswith("image/"):
        return "image", ""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = content.decode("latin-1") if "text" in ct else None
        except Exception:
            text = None
    if text is None:
        return "binary", " ".join(f"{b:02X}" for b in content[:4096]) + (" …" if len(content) > 4096 else "")
    s = text.strip()
    if s[:1] in "{[" or "json" in ct:
        try:
            return "json", json.dumps(json.loads(s), indent=2, ensure_ascii=False)
        except ValueError:
            pass
    if s.startswith("<") and ("xml" in ct or s.startswith("<?xml")):
        try:
            return "xml", xml.dom.minidom.parseString(s).toprettyxml(indent="  ")
        except Exception:
            pass
    return "text", text


CRESNEXT_STATUS = {0: "OK", 1: "OK - restart required", 2: "OK - application reset required",
                   3: "property not supported"}


def cresnext_summary(content: bytes):
    """Summarise the 'Actions' block a CresNext device returns after a POST."""
    try:
        data = json.loads(content)
    except (ValueError, TypeError):
        return []
    lines = []
    for act in (data.get("Actions") or []) if isinstance(data, dict) else []:
        for r in act.get("Results") or []:
            sid = r.get("StatusId")
            meaning = CRESNEXT_STATUS.get(sid, "ERROR" if isinstance(sid, int) and sid < 0 else "")
            info = r.get("StatusInfo", "")
            lines.append(f"{r.get('Path', '?')}: {meaning or sid} ({info})" if info else
                         f"{r.get('Path', '?')}: {meaning or sid}")
    return lines


def find_streams(obj):
    """Walk a DiscoveredStreams response and return [(label, unique_id)]."""
    found = {}

    def walk(o, key=None):
        if isinstance(o, dict):
            uid = o.get("UniqueId") or o.get("UniqueID")
            if not uid and key and re.fullmatch(r"[0-9a-fA-F-]{32,40}", key) and any(
                    k in o for k in ("SessionName", "StreamLocation", "RtspUri", "Name")):
                uid = key
            if uid and isinstance(uid, str):
                name = o.get("SessionName") or o.get("Name") or o.get("HostName") or ""
                loc = o.get("StreamLocation") or o.get("RtspUri") or o.get("IpAddress") or ""
                label = " — ".join(x for x in (name, loc) if x) or uid
                found[uid] = f"{label}   [{uid}]"
            for k, v in o.items():
                walk(v, k)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj)
    return [(lbl, uid) for uid, lbl in found.items()]


# ------------------------------------------------------------ client

class Result:
    def __init__(self):
        self.ok = False
        self.error = ""
        self.method = self.url = ""
        self.status = 0
        self.reason = ""
        self.headers = {}
        self.req_headers = {}
        self.content = b""
        self.elapsed_ms = 0
        self.notes = []
        self.curl = ""


class RestClient:
    def __init__(self):
        self.lock = threading.Lock()
        self.session = None
        self.key = None
        self.xsrf = None
        self.logged_in = False

    # -------------------------------------------------- configuration
    def configure(self, base_url, auth, user="", secret="", header_name="Authorization",
                  verify=False, timeout=15.0):
        if requests is None:
            raise RuntimeError("the 'requests' package is not installed (run run.bat again)")
        base_url = base_url.rstrip("/")
        key = (base_url, auth, user, secret, header_name, verify)
        if key != self.key:
            self.close_session()
            self.key = key
        self.base_url, self.auth, self.user, self.secret = base_url, auth, user, secret
        self.header_name, self.verify, self.timeout = header_name or "Authorization", verify, timeout

    def close_session(self):
        if self.session:
            try:
                self.session.close()
            except Exception:
                pass
        self.session = None
        self.xsrf = None
        self.logged_in = False

    def _origin(self):
        u = urlsplit(self.base_url)
        return f"{u.scheme}://{u.netloc}"

    def _session(self):
        if self.session is None:
            s = requests.Session()
            s.verify = self.verify
            s.trust_env = False          # talk to LAN devices directly, never via system proxies
            s.headers["User-Agent"] = "NetworkPocketknife/1.4"
            if self.auth == "basic":
                s.auth = requests.auth.HTTPBasicAuth(self.user, self.secret)
            elif self.auth == "digest":
                s.auth = requests.auth.HTTPDigestAuth(self.user, self.secret)
            elif self.auth == "bearer":
                s.headers["Authorization"] = f"Bearer {self.secret}"
            elif self.auth == "header":
                s.headers[self.header_name] = self.secret
            self.session = s
        return self.session

    def full_url(self, path):
        path = (path or "").strip()
        if re.match(r"https?://", path, re.I):
            return path
        if not self.base_url:
            raise ValueError("enter a host first")
        return self.base_url + ("" if path.startswith("/") or not path else "/") + path

    # -------------------------------------------------- CresNext login (DM NVX and similar)
    def cresnext_login(self, notes):
        s = self._session()
        origin = self._origin()
        s.cookies.clear()
        self.xsrf = None
        r = s.get(origin + "/userlogin.html", timeout=self.timeout, allow_redirects=False, verify=self.verify)
        if "TRACKID" not in s.cookies:
            notes.append(f"Login step 1: no TRACKID cookie (HTTP {r.status_code}) - continuing")
        r = s.post(origin + "/userlogin.html", data={"login": self.user, "passwd": self.secret},
                   headers={"Origin": origin, "Referer": origin + "/userlogin.html"},
                   timeout=self.timeout, allow_redirects=False, verify=self.verify)
        self._grab_xsrf(r)
        if r.status_code == 403 or "userstr" not in s.cookies:
            self.logged_in = False
            raise PermissionError(f"device login failed (HTTP {r.status_code}) - check username and password")
        self.logged_in = True
        notes.append("Logged in to device" + (" (XSRF token received)" if self.xsrf else ""))

    def _grab_xsrf(self, r):
        for k, v in r.headers.items():
            if k.lower() in ("crest-xsrf-token", "x-crest-xsrf-token"):
                self.xsrf = v

    # -------------------------------------------------- request
    def request(self, method, path, headers=None, body_type="none", body="", log_curl=True):
        res = Result()
        res.method = method.upper()
        t0 = time.perf_counter()
        with self.lock:
            try:
                url = self.full_url(path)
                res.url = url
                kwargs = self._body_kwargs(body_type, body)
                hdrs = dict(headers or {})
                if self.auth == "cresnext":
                    origin = self._origin()
                    hdrs.setdefault("Origin", origin)
                    hdrs.setdefault("Referer", origin + "/")
                    if not self.logged_in:
                        self.cresnext_login(res.notes)
                if body_type == "json" and not any(k.lower() == "content-type" for k in hdrs):
                    hdrs["Content-Type"] = "application/json"
                if log_curl:
                    res.curl = self.to_curl(res.method, url, hdrs, body_type, body)
                r = self._send(res.method, url, hdrs, kwargs)
                if self.auth == "cresnext" and (300 <= r.status_code < 400 or r.status_code in (401, 403)):
                    res.notes.append(f"Session expired (HTTP {r.status_code}) - logging in again")
                    self.logged_in = False
                    self.cresnext_login(res.notes)
                    kwargs = self._body_kwargs(body_type, body)      # file handles were consumed
                    r = self._send(res.method, url, hdrs, kwargs)
                self._grab_xsrf(r)
                res.status, res.reason = r.status_code, r.reason or ""
                res.headers = dict(r.headers)
                res.req_headers = dict(r.request.headers)
                res.content = r.content
                res.ok = True
                if self.auth == "cresnext":
                    res.notes += cresnext_summary(r.content)
            except Exception as e:
                res.error = self._friendly(e)
            finally:
                self._close_files(locals().get("kwargs"))
        res.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return res

    def _send(self, method, url, hdrs, kwargs):
        s = self._session()
        h = dict(hdrs)
        if self.xsrf and method not in ("GET", "HEAD"):
            h.setdefault("X-CREST-XSRF-TOKEN", self.xsrf)
        return s.request(method, url, headers=h, timeout=self.timeout, verify=self.verify,
                         allow_redirects=self.auth != "cresnext", **kwargs)

    @staticmethod
    def _body_kwargs(body_type, body):
        if body_type == "json":
            text = (body or "").strip()
            if text:
                try:
                    json.loads(text)
                except ValueError as e:
                    raise ValueError(f"body is not valid JSON: {e}") from None
            return {"data": text.encode("utf-8")}
        if body_type == "raw":
            return {"data": (body or "").encode("utf-8")}
        if body_type == "form":
            fields = parse_form_lines(body)
            if any(f for _, _, f in fields):
                raise ValueError("url-encoded forms cannot attach files - use Form (multipart)")
            return {"data": [(k, v) for k, v, _ in fields]}
        if body_type == "multipart":
            files = []
            for k, v, is_file in parse_form_lines(body):
                if is_file:
                    if not os.path.isfile(v):
                        raise FileNotFoundError(f"file not found: {v}")
                    files.append((k, (os.path.basename(v), open(v, "rb"))))
                elif v != "":                     # blank optional fields are left out
                    files.append((k, (None, v)))
            return {"files": files} if files else {}
        return {}

    @staticmethod
    def _close_files(kwargs):
        for _k, part in (kwargs or {}).get("files", []) if isinstance(kwargs, dict) else []:
            fh = part[1]
            if hasattr(fh, "close"):
                fh.close()

    @staticmethod
    def _friendly(e):
        name = type(e).__name__
        msg = str(e)
        if requests is not None:
            if isinstance(e, requests.exceptions.SSLError):
                return ("HTTPS certificate not trusted (normal for devices with a self-signed certificate) - "
                        "tick 'Ignore certificate errors'")
            if isinstance(e, requests.exceptions.ConnectTimeout):
                return "Connection timed out - check the IP address and that the device is reachable"
            if isinstance(e, requests.exceptions.ReadTimeout):
                return "The device did not answer in time"
            if isinstance(e, requests.exceptions.ConnectionError):
                m = re.search(r"(\[Errno -?\d+\][^\"')]*|\[WinError \d+\][^\"')]*|timed out|Name or service not known"
                              r"|getaddrinfo failed|Connection refused|Connection reset[^\"')]*)", msg)
                why = f" ({m.group(1).strip()})" if m else ""
                return f"Could not connect - check the IP, port and HTTP/HTTPS setting{why}"
        if isinstance(e, (ValueError, FileNotFoundError, PermissionError, RuntimeError)):
            return msg
        return f"{name}: {msg}"

    def logout(self):
        if self.auth == "cresnext" and self.session and self.logged_in:
            try:
                self.session.get(self._origin() + "/logout", timeout=5, allow_redirects=False,
                                 verify=self.verify)
            except Exception:
                pass
        self.close_session()

    # -------------------------------------------------- cURL export
    def to_curl(self, method, url, headers, body_type, body, hide_secrets=True):
        parts = ["curl", "-k"] if not self.verify else ["curl"]
        parts += ["-X", method, url]
        h = dict(headers)
        if self.auth == "basic":
            parts += ["-u", f"{self.user}:{'****' if hide_secrets else self.secret}"]
        elif self.auth == "digest":
            parts += ["--digest", "-u", f"{self.user}:{'****' if hide_secrets else self.secret}"]
        elif self.auth == "bearer":
            h["Authorization"] = "Bearer " + ("****" if hide_secrets else self.secret)
        elif self.auth == "header":
            h[self.header_name] = "****" if hide_secrets else self.secret
        elif self.auth == "cresnext":
            origin = self._origin()
            h.setdefault("Origin", origin)
            h.setdefault("Referer", origin + "/")
            h["Cookie"] = "<session cookies from /userlogin.html login>"
        for k, v in h.items():
            parts += ["-H", f"{k}: {v}"]
        if body_type in ("json", "raw") and body and body.strip():
            parts += ["--data-raw", body.strip()]
        elif body_type == "form":
            for k, v, _ in parse_form_lines(body):
                parts += ["--data-urlencode", f"{k}={v}"]
        elif body_type == "multipart":
            for k, v, is_file in parse_form_lines(body):
                if is_file or v != "":
                    parts += ["-F", f"{k}=@{v}" if is_file else f"{k}={v}"]
        return " ".join(_q(p) for p in parts)


def _q(s):
    if re.fullmatch(r"[A-Za-z0-9_\-./:=@,+%]+", s):
        return s
    return "'" + s.replace("'", "'\"'\"'") + "'" if os.name != "nt" else '"' + s.replace('"', '\\"') + '"'


# ------------------------------------------------------------ profiles

def P(group, name, method, path, body_type="none", body="", confirm="", help=""):
    return {"group": group, "name": name, "method": method, "path": path, "body_type": body_type,
            "body": body, "confirm": confirm, "help": help}


def _nvx_json(obj):
    return json.dumps({"Device": obj}, indent=2)


GENERIC = {
    "name": "Generic REST device",
    "scheme": "http", "port": 0, "base_path": "", "auth": "none", "header_name": "Authorization",
    "secret_label": "Password / token",
    "notes": "Any HTTP/REST device. Type a path like /api/status or a full URL.",
    "presets": [],
}

VC4 = {
    "name": "Crestron VC-4 (Virtual Control)",
    "scheme": "https", "port": 0, "base_path": "/VirtualControl/config/api", "auth": "header",
    "header_name": "Authorization", "secret_label": "API token",
    "notes": "Create a token in the VC-4 web page: Settings → Tokens → Add Token, and paste it in "
             "'API token'. Untick Read-Only if you want to start/stop rooms or upload programs.",
    "presets": [
        P("Server", "Server / device info", "GET", "/DeviceInfo"),
        P("Server", "Network settings", "GET", "/Ethernet"),
        P("Server", "Licenses", "GET", "/LicenseRegistry"),
        P("Server", "System table", "GET", "/SystemTable"),
        P("Server", "Device map", "GET", "/DeviceMap"),
        P("Server", "Authentication settings", "GET", "/Authentication"),
        P("Server", "Authentication groups", "GET", "/Authentication/Groups"),
        P("Server", "Stop debugging (all programs)", "POST", "/StopDebugging"),
        P("Server", "Restart Virtual Control service", "POST", "/RestartService",
          confirm="This restarts the whole Virtual Control service - ALL rooms will stop and restart."),

        P("Rooms", "List rooms", "GET", "/ProgramInstance"),
        P("Rooms", "Get room", "GET", "/ProgramInstance/${RoomId}"),
        P("Rooms", "Start room", "PUT", "/ProgramInstance", "multipart",
          "ProgramInstanceId=${RoomId}\nStart=true"),
        P("Rooms", "Stop room", "PUT", "/ProgramInstance", "multipart",
          "ProgramInstanceId=${RoomId}\nStop=true", confirm="Stop room ${RoomId}?"),
        P("Rooms", "Restart room", "PUT", "/ProgramInstance", "multipart",
          "ProgramInstanceId=${RoomId}\nRestart=true", confirm="Restart room ${RoomId}?"),
        P("Rooms", "Debugging on/off", "PUT", "/ProgramInstance", "multipart",
          "ProgramInstanceId=${RoomId}\nDebuggingEnabled=${Enabled|true,false}"),
        P("Rooms", "IP table of room", "GET", "/IpTableByPID/${RoomId}"),
        P("Rooms", "Add room", "POST", "/ProgramInstance", "multipart",
          "Name=${Room name}\nProgramInstanceId=${RoomId}\nProgramLibraryId=${ProgramId}\nNotes=${Notes}"),
        P("Rooms", "Delete room", "DELETE", "/ProgramInstance/${RoomId}",
          confirm="Delete room ${RoomId}? This cannot be undone."),

        P("Programs", "List programs", "GET", "/ProgramLibrary"),
        P("Programs", "Get program", "GET", "/ProgramLibrary/${ProgramId}"),
        P("Programs", "Upload new program", "POST", "/ProgramLibrary", "multipart",
          "FriendlyName=${Program name}\nAppFile=@${Program file (.lpz/.cpz/.clz):file}\nNotes=${Notes}"),
        P("Programs", "Update program (and restart rooms)", "PUT", "/ProgramLibrary", "multipart",
          "ProgramId=${ProgramId}\nAppFile=@${Program file (.lpz/.cpz/.clz):file}\nStartNow=true",
          confirm="Replace program ${ProgramId} and restart the rooms that use it?"),
        P("Programs", "Delete program", "DELETE", "/ProgramLibrary/${ProgramId}",
          confirm="Delete program ${ProgramId}? This cannot be undone."),

        P("Device mapping", "List device-to-room mappings", "GET", "/DeviceProgramMap"),
        P("Device mapping", "Map a device to a room", "POST", "/DeviceProgramMap", "json",
          '{\n  "ProgramInstanceId": "${RoomId}",\n  "MacAddress": "${Device MAC}",\n'
          '  "DeviceIpId": "${Device IP ID (hex)}",\n  "ProgramIpId": "${Program IP ID (hex)}"\n}'),
    ],
}

NVX = {
    "name": "Crestron DM NVX",
    "scheme": "https", "port": 0, "base_path": "", "auth": "cresnext", "header_name": "",
    "secret_label": "Password",
    "notes": "Uses the device's web login (same user/password as its web page). The login, cookies and "
             "re-login are handled automatically. POST results show OK / restart required / error below.",
    "presets": [
        P("Status", "Device info (model, firmware, serial)", "GET", "/Device/DeviceInfo"),
        P("Status", "Mode, video/audio source", "GET", "/Device/DeviceSpecific"),
        P("Status", "Inputs & outputs (sync, resolution, HDCP)", "GET", "/Device/AudioVideoInputOutput"),
        P("Status", "Current route", "GET", "/Device/AvRouting"),
        P("Status", "Stream transmit (encoder)", "GET", "/Device/StreamTransmit"),
        P("Status", "Stream receive (decoder)", "GET", "/Device/StreamReceive"),
        P("Status", "Discovered streams (sources on network)", "GET", "/Device/DiscoveredStreams"),
        P("Status", "Network settings", "GET", "/Device/Ethernet"),
        P("Status", "Capabilities", "GET", "/Device/DeviceCapabilities"),
        P("Status", "Preview image info", "GET", "/Device/Preview"),
        P("Status", "EVERYTHING (large)", "GET", "/Device"),
        P("Status", "Wait for changes (long poll ~20 s)", "GET", "/Device/Longpoll"),

        P("Routing", "Route a source to this receiver", "POST", "/Device", "json",
          _nvx_json({"AvRouting": {"Routes": [{"VideoSource": "${Source:nvxstream}"}]}}),
          help="Pick a source from the list (read from this receiver's discovered streams). "
               "Audio and USB follow video unless breakaway is set."),
        P("Routing", "Route by RTSP / stream URL", "POST", "/Device", "json",
          _nvx_json({"StreamReceive": {"Streams": [{"StreamLocation": "${Stream URL}"}]}}),
          help="Stream URL is the transmitter's RTSP address, e.g. rtsp://10.0.0.5:554/live.sdp"),
        P("Routing", "Clear route", "POST", "/Device", "json",
          _nvx_json({"AvRouting": {"Routes": [{"VideoSource": "", "AudioSource": "", "UsbSource": ""}]}})),
        P("Routing", "Select video source (Stream / local input)", "POST", "/Device", "json",
          _nvx_json({"DeviceSpecific": {"VideoSource": "${VideoSource|Stream,Input1,Input2}"}})),

        P("Settings", "Set host name", "POST", "/Device", "json",
          _nvx_json({"Ethernet": {"HostName": "${Host name}"}})),
        P("Settings", "Front-panel LEDs on/off", "POST", "/Device", "json",
          '{\n  "Device": {\n    "DeviceSpecific": {\n      "LedsEnabled": ${Enabled|true,false}\n    }\n  }\n}'),
        P("Settings", "Set mode Transmitter / Receiver", "POST", "/Device", "json",
          _nvx_json({"DeviceSpecific": {"DeviceMode": "${Mode|Transmitter,Receiver}"}}),
          confirm="Changing the device mode makes the NVX reboot. Continue?"),
        P("Settings", "Reboot device", "POST", "/Device", "json",
          '{\n  "Device": {\n    "DeviceOperations": {\n      "Reboot": true\n    }\n  }\n}',
          confirm="Reboot the device?",
          help="DeviceOperations/Reboot is the common CresNext reboot command; if your firmware "
               "answers 'property not supported', reboot from the web page instead."),
        P("Session", "Log out", "GET", "/logout"),
    ],
}

BUILTIN_PROFILES = [GENERIC, VC4, NVX]


def profiles_dir(appdata):
    d = os.path.join(appdata, "profiles")
    os.makedirs(d, exist_ok=True)
    return d


def load_profiles(appdata):
    """Built-in profiles plus any *.json in %APPDATA%\\Portal System\\NetworkPocketknife\\profiles."""
    profiles, errors = [dict(p) for p in BUILTIN_PROFILES], []
    for fn in sorted(glob.glob(os.path.join(profiles_dir(appdata), "*.json"))):
        try:
            with open(fn, encoding="utf-8") as f:
                p = json.load(f)
            if not isinstance(p, dict) or "name" not in p:
                raise ValueError("missing 'name'")
            base = dict(GENERIC)
            base.update(p)
            base["presets"] = [dict(P("Commands", "?", "GET", ""), **x) for x in p.get("presets", [])]
            base["file"] = fn
            profiles = [q for q in profiles if q["name"] != base["name"]] + [base]
        except Exception as e:
            errors.append(f"{os.path.basename(fn)}: {e}")
    return profiles, errors


def example_profile_json():
    return json.dumps({
        "name": "My device",
        "scheme": "http", "port": 0, "base_path": "/api", "auth": "basic",
        "header_name": "Authorization", "secret_label": "Password",
        "notes": "Shown at the top of the REST tab when this profile is selected.",
        "presets": [
            {"group": "Status", "name": "Get status", "method": "GET", "path": "/status"},
            {"group": "Control", "name": "Set input", "method": "POST", "path": "/input",
             "body_type": "json", "body": "{\"input\": \"${Input|HDMI1,HDMI2}\"}",
             "confirm": "Switch input to ${Input}?"},
        ],
    }, indent=2)


__all__ = ["AUTH_TYPES", "BODY_TYPES", "RestClient", "Result", "find_placeholders", "fill_placeholders",
           "parse_header_lines", "pretty_body", "find_streams", "load_profiles", "profiles_dir",
           "example_profile_json", "BUILTIN_PROFILES"]
