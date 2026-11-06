"""
Magical Portal System - UDP streaming control for BrightSign
-----------------------------------------------------------
A small local web server (Python standard library only - nothing to pip install)
that serves a control page and starts/stops FFmpeg or VLC processes that send
UDP (MPEG-TS) streams to BrightSign players, or receive a UDP stream to watch it.

Run:   python server.py            (then open http://localhost:8080)
Opts:  python server.py --port 8080 --host 127.0.0.1
       --host 0.0.0.0 lets other PCs/phones on your network control it (no password!).
"""

import argparse
import io
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

__version__ = "1.7.2"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
CONFIG_FILE = os.path.join(BASE_DIR, "streams.json")
LOG_DIR = os.path.join(BASE_DIR, "logs")
IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0
SIGNAL_LOST_AFTER = 3  # seconds without new data = "no signal"

os.makedirs(LOG_DIR, exist_ok=True)

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

DEFAULT_SETTINGS = {
    "ffmpeg_path": "",
    "vlc_path": "",
    "default_adapter": "",       # IPv4 of the network card to use when a portal doesn't pick one
    "restart_delay": 3,
    "tray_notifications": True,
    "web_port": 8080,            # control page port
    "web_name": "PortalSystem",  # friendly address: http://PortalSystem:8080
    "web_access": "local",       # local = this PC only | network = other devices too
    "web_password_hash": "",     # optional password for OTHER devices (this PC never needs it)
    "web_password_salt": "",  # Windows pop-ups when a signal is lost / a portal fails
}

DEFAULT_STREAM = {
    "id": "",
    "name": "New portal",
    "role": "send",              # send = PC -> UDP -> BrightSign ; watch = receive a UDP stream on this PC
    "engine": "ffmpeg",          # ffmpeg | vlc (send) ; vlc | ffplay (watch)
    "source_type": "file",       # file | url | desktop | dshow | test
    "source": "",
    "loop": True,
    "smooth_send": True,         # constant bitrate + evenly paced packets (fewer drops on the network)
    "draw_mouse": True,          # desktop capture: show the mouse pointer
    "desktop_audio": "",         # desktop capture: Windows recording device for sound ("" = no sound)
    "test_pattern": "testsrc2",  # testsrc2 | smptehdbars | color | image | video
    "test_color": "#1e1b4b",     # solid colour pattern
    "test_media": "",            # image / video file for the test source
    "test_tone": True,           # 1 kHz tone (otherwise silence) for patterns and images
    "dest_ip": "239.192.1.1",
    "dest_port": 5000,
    "ttl": 4,
    "pkt_size": 1316,
    "local_addr": "",            # network adapter IPv4 ("" = use the default from Settings)
    "source_filter": "",         # watch: only accept multicast from this sender IP (SSM)
    "video_mode": "h264",        # copy | h264
    "video_encoder": "libx264",  # libx264 | h264_nvenc | h264_qsv | h264_amf
    "video_bitrate": 8000,       # kbps
    "width": 1920,
    "height": 1080,
    "fps": 30,
    "gop": 60,
    "audio_mode": "aac",         # aac | copy | none
    "audio_bitrate": 192,        # kbps per 2 channels
    "audio_channels": 2,         # 1 | 2 | 6 (5.1) | 8 (7.1) | 16 (8 stereo tracks)
    "network_caching": 300,      # watch mode, ms
    "extra_args": "",
    "auto_restart": True,
    "autostart": False,
}

COMMON_FFMPEG = [
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
    os.path.join(BASE_DIR, "ffmpeg", "bin", "ffmpeg.exe"),
    os.path.join(BASE_DIR, "ffmpeg.exe"),
]
COMMON_VLC = [
    r"C:\Program Files\VideoLAN\VLC\vlc.exe",
    r"C:\Program Files (x86)\VideoLAN\VLC\vlc.exe",
]


def detect_tool(name, candidates):
    found = shutil.which(name)
    if found:
        return found
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return ""


# --------------------------------------------------------------------------- #
# Config persistence
# --------------------------------------------------------------------------- #

config_lock = threading.RLock()
config = {"settings": dict(DEFAULT_SETTINGS), "streams": []}


def load_config():
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            config["settings"] = {**DEFAULT_SETTINGS, **data.get("settings", {})}
            config["streams"] = [{**DEFAULT_STREAM, **s} for s in data.get("streams", [])]
        except Exception as e:  # noqa
            print(f"[warn] could not read streams.json: {e}")
    s = config["settings"]
    if not s.get("ffmpeg_path"):
        s["ffmpeg_path"] = detect_tool("ffmpeg", COMMON_FFMPEG)
    if not s.get("vlc_path"):
        s["vlc_path"] = detect_tool("vlc", COMMON_VLC)
    if not config["streams"]:
        config["streams"].append(dict(DEFAULT_STREAM, id=new_id(), name="Test pattern -> BrightSign",
                                      source_type="test", source=""))
    save_config()


def save_config():
    with config_lock:
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        os.replace(tmp, CONFIG_FILE)


def new_id():
    return uuid.uuid4().hex[:8]


def get_stream(sid):
    with config_lock:
        for s in config["streams"]:
            if s["id"] == sid:
                return s
    return None


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def is_multicast(ip):
    try:
        return ipaddress.ip_address((ip or "").strip()).is_multicast
    except ValueError:
        return False


def valid_ip(ip):
    try:
        ipaddress.ip_address((ip or "").strip())
        return True
    except ValueError:
        return False


def as_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def sibling_tool(name):
    """ffplay/ffprobe live next to ffmpeg."""
    ff = config["settings"].get("ffmpeg_path") or ""
    exe = name + (".exe" if IS_WINDOWS else "")
    if ff:
        cand = os.path.join(os.path.dirname(ff), exe)
        if os.path.isfile(cand):
            return cand
    return shutil.which(name) or name


def adapter_ip(st):
    ip = (st.get("local_addr") or "").strip() or (config["settings"].get("default_adapter") or "").strip()
    return ip if valid_ip(ip) else ""


def split_extra(text):
    text = (text or "").strip()
    if not text:
        return []
    parts = shlex.split(text, posix=not IS_WINDOWS)
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]


def udp_out_url(st, pace_kbps=0):
    ip = st["dest_ip"].strip()
    q = [f"pkt_size={as_int(st['pkt_size'], 1316)}"]
    if pace_kbps:
        # Send packets evenly spaced instead of in whole-frame bursts.
        # burst_bits = how far the sender may catch up after oversleeping. Windows timers tick
        # every ~15 ms, so allow 50 ms - otherwise FFmpeg "forgives" the lost time, sends slower
        # than the stream, and its send buffer overflows ("Cannot allocate memory").
        bps = pace_kbps * 1000
        q += [f"bitrate={bps}", f"burst_bits={max(8 * 1316 * 10, bps // 20)}", "fifo_size=262144"]
    if is_multicast(ip):
        q.append(f"ttl={as_int(st['ttl'], 4)}")
    nic = adapter_ip(st)
    if nic:
        q.append(f"localaddr={nic}")
    return f"udp://{ip}:{as_int(st['dest_port'], 5000)}?" + "&".join(q)


def udp_in_url(st, timeout_us=None):
    """FFmpeg receive URL - joins multicast on the chosen network adapter."""
    ip = (st.get("dest_ip") or "").strip()
    port = as_int(st["dest_port"], 5000)
    q = ["overrun_nonfatal=1", "fifo_size=1000000", "buffer_size=8388608"]
    if is_multicast(ip):
        nic = adapter_ip(st)
        if nic:
            q.append(f"localaddr={nic}")
        if valid_ip(st.get("source_filter")):
            q.append(f"sources={st['source_filter'].strip()}")
        host = ip
    else:
        host = "0.0.0.0"
    if timeout_us:
        q.append(f"timeout={timeout_us}")
    return f"udp://{host}:{port}?" + "&".join(q)


def brightsign_url(st):
    ip = (st.get("dest_ip") or "").strip()
    port = as_int(st["dest_port"], 5000)
    return f"udp://{ip}:{port}" if is_multicast(ip) else f"udp://0.0.0.0:{port}"


# --------------------------------------------------------------------------- #
# Network adapters
# --------------------------------------------------------------------------- #

_adapter_cache = {"t": 0, "list": []}


def list_adapters(force=False):
    if not force and time.time() - _adapter_cache["t"] < 30 and _adapter_cache["list"]:
        return _adapter_cache["list"]
    out = []
    try:
        if IS_WINDOWS:
            ps = ("Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -ne '127.0.0.1' } | "
                  "ForEach-Object { $a = Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue; "
                  "[pscustomobject]@{ name = $_.InterfaceAlias; ip = $_.IPAddress; prefix = $_.PrefixLength; "
                  "status = $(if ($a) { [string]$a.Status } else { '' }); "
                  "desc = $(if ($a) { $a.InterfaceDescription } else { '' }) } } | ConvertTo-Json -Compress")
            r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                               capture_output=True, text=True, timeout=20, creationflags=CREATE_NO_WINDOW)
            data = json.loads(r.stdout or "[]")
            if isinstance(data, dict):
                data = [data]
            for d in data:
                out.append({"name": d.get("name", ""), "ip": d.get("ip", ""), "prefix": d.get("prefix"),
                            "status": d.get("status", ""), "desc": d.get("desc", "")})
            if not out:  # fallback: ipconfig
                r = subprocess.run(["ipconfig"], capture_output=True, text=True, timeout=10,
                                   creationflags=CREATE_NO_WINDOW, errors="replace")
                name = ""
                for line in r.stdout.splitlines():
                    if line and not line.startswith(" ") and line.rstrip().endswith(":"):
                        name = re.sub(r"^.*?adapter\s+", "", line.strip()[:-1])
                    m = re.search(r"IPv4.*?:\s*([\d.]+)", line)
                    if m and name:
                        out.append({"name": name, "ip": m.group(1), "prefix": None, "status": "", "desc": ""})
        else:
            r = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=10)
            for line in r.stdout.splitlines():
                m = re.match(r"\d+:\s+(\S+)\s+inet\s+([\d.]+)/(\d+)", line)
                if m and m.group(2) != "127.0.0.1":
                    out.append({"name": m.group(1), "ip": m.group(2), "prefix": int(m.group(3)),
                                "status": "Up", "desc": ""})
    except Exception as e:
        print(f"[warn] adapter listing failed: {e}")
    if not out:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                out.append({"name": "Default adapter", "ip": ip, "prefix": None, "status": "", "desc": ""})
        except Exception:
            pass
    if not out:
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if not ip.startswith("127."):
                    out.append({"name": "Adapter", "ip": ip, "prefix": None, "status": "", "desc": ""})
        except Exception:
            pass
    for a in out:
        a["apipa"] = a["ip"].startswith("169.254.")
    _adapter_cache.update(t=time.time(), list=out)
    return out


# --------------------------------------------------------------------------- #
# Command builders
# --------------------------------------------------------------------------- #

class BuildError(Exception):
    pass


AUDIO_LAYOUTS = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}


def audio_channels(st):
    ch = as_int(st.get("audio_channels"), 2)
    return ch if ch in (1, 2, 6, 8, 16) else 2


def tone_inputs(st, ch):
    """lavfi audio inputs for the test source. Every channel gets its own pitch
    (300, 400, 500 ... Hz) so you can tell them apart on a meter; mono/stereo use 1 kHz.
    16 channels = 8 stereo tracks. Returns (args, number_of_audio_inputs)."""
    on = st.get("test_tone", True)

    def expr(f):
        return f"0.125*sin(2*PI*{f}*t)" if on else "0"   # 0.125 = -18 dBFS
    if ch in (1, 2):
        freqs = [1000] * ch
    else:
        freqs = [300 + 100 * i for i in range(ch)]
    if ch == 16:
        args = []
        for k in range(8):
            args += ["-f", "lavfi", "-i",
                     f"aevalsrc=exprs={expr(freqs[2 * k])}|{expr(freqs[2 * k + 1])}:s=48000:c=stereo"]
        return args, 8
    exprs = "|".join(expr(f) for f in freqs)
    return ["-f", "lavfi", "-i", f"aevalsrc=exprs={exprs}:s=48000:c={AUDIO_LAYOUTS[ch]}"], 1


_dpi_set = False


def list_monitors():
    """Windows screens: [{'num': 2, 'x':1920, 'y':0, 'w':1920, 'h':1080, 'primary':False}, ...]
    num = the Windows display number (DISPLAY2 -> 'Screen 2'). Coordinates in real pixels."""
    global _dpi_set
    if not IS_WINDOWS:
        return []
    import ctypes
    from ctypes import wintypes
    if not _dpi_set:   # real pixel coordinates even with 125% / 150% scaling
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        _dpi_set = True

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD), ("szDevice", wintypes.WCHAR * 32)]
    user32 = ctypes.windll.user32
    PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), PROC, wintypes.LPARAM]
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.c_void_p]
    mons = []

    def cb(hmon, hdc, rect, lparam):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            r = mi.rcMonitor
            m = re.search(r"DISPLAY(\d+)", mi.szDevice)
            mons.append({"num": int(m.group(1)) if m else len(mons) + 1, "x": r.left, "y": r.top,
                         "w": r.right - r.left, "h": r.bottom - r.top, "primary": bool(mi.dwFlags & 1)})
        return True
    try:
        user32.EnumDisplayMonitors(None, None, PROC(cb), 0)
    except Exception as e:
        print(f"[warn] screen list failed: {e}")
    mons.sort(key=lambda m: (m["x"], m["y"]))
    return mons


RELAY_WAIT_S = 15   # no data for this long -> FFmpeg exits and the portal reconnects


def udp_source_url(src, st):
    """Tidy a udp:// source: accept VLC-style '@', join multicast on the chosen network adapter,
    add buffering and a 'no data' timeout so a dropped source reconnects by itself.
    Returns (url, host, port) or None if src isn't a udp:// URL."""
    m = re.fullmatch(r"udp://@?\[?([^\]/:?@]*)\]?:(\d+)/?(?:\?(.*))?", src.strip(), re.IGNORECASE)
    if not m:
        return None
    host, port, query = m.group(1) or "0.0.0.0", int(m.group(2)), m.group(3) or ""
    opts = dict(kv.split("=", 1) if "=" in kv else (kv, "") for kv in query.split("&") if kv)
    if is_multicast(host) and "localaddr" not in opts and adapter_ip(st):
        opts["localaddr"] = adapter_ip(st)
    opts.setdefault("overrun_nonfatal", "1")
    opts.setdefault("fifo_size", "1000000")
    opts.setdefault("buffer_size", "8388608")
    opts.setdefault("timeout", str(RELAY_WAIT_S * 1_000_000))
    q = "&".join(f"{k}={v}" if v != "" else k for k, v in opts.items())
    return f"udp://{host}:{port}?{q}", host, port


def gdigrab_input(src, fps, draw_mouse=True):
    """FFmpeg input args for 'desktop' (all screens), 'screen:N' (one screen) or 'title=Window'."""
    src = (src or "desktop").strip()
    base = ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1" if draw_mouse else "0"]
    m = re.fullmatch(r"screen:(\d+)", src)
    if m:
        num = int(m.group(1))
        mon = next((x for x in list_monitors() if x["num"] == num), None)
        if not mon:
            raise BuildError(f"Screen {num} isn't connected right now. Pick another screen.")
        return base + ["-offset_x", str(mon["x"]), "-offset_y", str(mon["y"]),
                       "-video_size", f"{mon['w']}x{mon['h']}", "-i", "desktop"]
    return base + ["-i", src]


def build_ffmpeg_send(st):
    exe = config["settings"].get("ffmpeg_path") or "ffmpeg"
    stype = st["source_type"]
    src = (st.get("source") or "").strip()
    fps = as_int(st["fps"], 30)
    w, h = as_int(st["width"], 0), as_int(st["height"], 0)
    a = [exe, "-hide_banner", "-nostdin", "-loglevel", "info", "-stats"]

    raw_input = stype in ("desktop", "dshow", "test")
    maps = []
    if stype == "file":
        if not src:
            raise BuildError("Pick a video file for the source.")
        if st.get("loop"):
            a += ["-stream_loop", "-1"]
        a += ["-re", "-i", src]
    elif stype == "url":
        if not src:
            raise BuildError("Enter a source URL (udp://, rtsp://, http://, srt://...).")
        udp = udp_source_url(src, st)
        if udp:
            url, in_host, in_port = udp
            if in_port == as_int(st["dest_port"], 0) and in_host.strip() == st["dest_ip"].strip():
                raise BuildError("The output can't be the same address and port as the input - "
                                 "that would feed the stream back into itself. Pick a different destination.")
            a += ["-fflags", "+genpts+discardcorrupt", "-probesize", "5000000", "-analyzeduration", "3000000",
                  "-i", url]
        else:
            a += ["-i", src]
    elif stype == "desktop":
        dev = (st.get("desktop_audio") or "").strip()
        with_sound = bool(dev) and st["audio_mode"] != "none"
        a += (["-thread_queue_size", "1024"] if with_sound else []) + gdigrab_input(src, fps, st.get("draw_mouse", True))
        if with_sound:
            # sound from a recording device (Stereo Mix, virtual cable, mic, line-in) at the same time
            a += ["-thread_queue_size", "1024", "-f", "dshow", "-audio_buffer_size", "50", "-i", f"audio={dev}"]
            maps = ["-map", "0:v:0", "-map", "1:a:0"]
    elif stype == "dshow":
        if not src:
            raise BuildError("Enter a DirectShow device, e.g. video=Cam Link 4K:audio=Line In")
        a += ["-f", "dshow", "-rtbufsize", "256M", "-i", src]
    elif stype == "test":
        size = f"{w or 1920}x{h or 1080}"
        pat = st.get("test_pattern") or "testsrc2"
        media = (st.get("test_media") or "").strip()
        if pat in ("image", "video") and not media:
            raise BuildError(f"Pick the {pat} to show for the test source (Browse...).")
        if pat == "video":
            a += ["-stream_loop", "-1", "-re", "-i", media]
        elif pat == "image":
            a += ["-re", "-loop", "1", "-framerate", str(fps), "-i", media]
        elif pat == "smptehdbars":
            a += ["-re", "-f", "lavfi", "-i", f"smptehdbars=size={size}:rate={fps}"]
        elif pat == "color":
            col = (st.get("test_color") or "").strip().lstrip("#")
            col = col if re.fullmatch(r"[0-9a-fA-F]{6}", col) else "000000"
            a += ["-re", "-f", "lavfi", "-i", f"color=c=0x{col}:size={size}:rate={fps}"]
        else:
            a += ["-re", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}"]
        if pat != "video" and st["audio_mode"] != "none":
            tone_args, n_audio = tone_inputs(st, audio_channels(st))
            a += tone_args
            # explicit stream selection: picture from input 0, every tone input as its own track
            maps = ["-map", "0:v:0"] + sum((["-map", f"{i + 1}:a:0"] for i in range(n_audio)), [])
    else:
        raise BuildError("Unknown source type")

    vmode = "h264" if raw_input else st["video_mode"]
    if vmode == "copy":
        a += ["-c:v", "copy"]
    else:
        enc = st.get("video_encoder") or "libx264"
        br = as_int(st["video_bitrate"], 8000)
        a += ["-c:v", enc]
        if enc == "libx264":
            a += ["-preset", "veryfast", "-tune", "zerolatency"]
        smooth = st.get("smooth_send", True)
        # smaller rate buffer = keyframes can't spike far above the bitrate
        a += ["-b:v", f"{br}k", "-maxrate", f"{br}k", "-bufsize", f"{br if smooth else br * 2}k",
              "-g", str(as_int(st["gop"], fps * 2)), "-pix_fmt", "yuv420p"]
        if smooth:
            if enc == "libx264":
                a += ["-x264-params", "nal-hrd=cbr:force-cfr=1"]
            elif enc in ("h264_nvenc", "h264_amf"):
                a += ["-rc", "cbr"]
        generated = stype == "test" and (st.get("test_pattern") or "testsrc2") not in ("image", "video")
        if w and h and not generated:
            # fit inside WxH keeping the shape, black bars if needed
            a += ["-vf", f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                         f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"]
        elif not generated:
            a += ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
        a += ["-r", str(fps)]

    desk_sound = stype == "desktop" and bool((st.get("desktop_audio") or "").strip())
    amode = "none" if (stype == "desktop" and not desk_sound) else st["audio_mode"]
    if amode == "copy" and desk_sound:
        amode = "aac"
    ch = audio_channels(st)
    if desk_sound and ch == 16:
        ch = 2      # one recording device = one track
    if amode != "none" and ch == 16 and not maps:
        # 16 ch from a file / stream / device: keep every audio track the source has
        maps = ["-map", "0:v:0?", "-map", "0:a?"]
    a += maps
    kbps = as_int(st["audio_bitrate"], 192)   # (refined below when audio is encoded)
    if amode == "none":
        a += ["-an"]
    elif amode == "copy" and not raw_input:
        a += ["-c:a", "copy"]
    else:
        per_track = 2 if ch == 16 else ch
        kbps = max(64, as_int(st["audio_bitrate"], 192) * max(1, per_track) // 2)
        if desk_sound:
            a += ["-af", "aresample=async=1000"]   # keep the sound in step with the screen
        a += ["-c:a", "aac", "-b:a", f"{kbps}k", "-ar", "48000"]
        if ch != 16:
            a += ["-ac", str(ch)]

    a += split_extra(st.get("extra_args"))
    pace = 0
    if st.get("smooth_send", True) and vmode != "copy":
        # constant-rate transport stream: video + audio + ~15% for the TS packaging
        a_kbps = 0
        if amode != "none":
            a_kbps = kbps * (8 if ch == 16 else 1)
        muxrate = int((as_int(st["video_bitrate"], 8000) + a_kbps) * 1.15) + 150
        a += ["-muxrate", f"{muxrate}k"]
        if not st.get("_no_pace"):
            pace = int(muxrate * 1.05)      # drain slightly faster than we fill
    a += ["-f", "mpegts", udp_out_url(st, pace)]
    return a


def build_vlc_send(st):
    exe = config["settings"].get("vlc_path") or "vlc"
    stype = st["source_type"]
    src = (st.get("source") or "").strip()
    fps = as_int(st["fps"], 30)
    a = [exe, "-I", "dummy", "--dummy-quiet", "--file-logging",
         f"--logfile={os.path.join(LOG_DIR, st['id'] + '_vlc.log')}"]
    opts = []
    if stype in ("file", "url"):
        if not src:
            raise BuildError("Pick a video file / URL for the source.")
        mrl = src
    elif stype == "desktop":
        mrl = "screen://"
        opts.append(f":screen-fps={fps}")
        m = re.fullmatch(r"screen:(\d+)", src)
        if m:
            mon = next((x for x in list_monitors() if x["num"] == int(m.group(1))), None)
            if not mon:
                raise BuildError(f"Screen {m.group(1)} isn't connected right now.")
            opts += [f":screen-left={mon['x']}", f":screen-top={mon['y']}",
                     f":screen-width={mon['w']}", f":screen-height={mon['h']}"]
        elif src.startswith("title="):
            raise BuildError("Capturing one window needs FFmpeg. Switch the program to FFmpeg.")
        if (st.get("desktop_audio") or "").strip() and st["audio_mode"] != "none":
            raise BuildError("Desktop capture with sound needs FFmpeg. Switch the program to FFmpeg.")
    elif stype == "dshow":
        mrl = "dshow://"
        m = re.search(r"video=([^:]+)", src)
        opts.append(f":dshow-vdev={(m.group(1) if m else src).strip()}")
        m = re.search(r"audio=(.+)", src)
        if m:
            opts.append(f":dshow-adev={m.group(1).strip()}")
    else:
        raise BuildError("The test source needs FFmpeg. Switch the program to FFmpeg.")

    ip, port = st["dest_ip"].strip(), as_int(st["dest_port"], 5000)
    dst = f"std{{access=udp{{ttl={as_int(st['ttl'], 4)}}},mux=ts,dst={ip}:{port}}}"
    raw = stype in ("desktop", "dshow")
    if st["video_mode"] == "h264" or raw:
        parts = [f"vcodec=h264,vb={as_int(st['video_bitrate'], 8000)},fps={fps}"]
        if as_int(st["width"], 0) and as_int(st["height"], 0):
            parts.append(f"width={st['width']},height={st['height']}")
        parts.append(f"venc=x264{{preset=veryfast,tune=zerolatency,keyint={as_int(st['gop'], 60)}}}")
        if st["audio_mode"] == "none" or stype == "desktop":
            parts.append("acodec=none")
        elif st["audio_mode"] == "aac" or raw:
            vch = audio_channels(st)
            if vch == 16:
                raise BuildError("16-channel audio needs FFmpeg. Switch the program to FFmpeg.")
            parts.append(f"acodec=mp4a,ab={as_int(st['audio_bitrate'], 192) * max(1, vch) // 2},"
                         f"channels={vch},samplerate=48000")
        sout = f"#transcode{{{','.join(parts)}}}:{dst}"
    else:
        sout = f"#{dst}"

    a += [mrl] + opts + [f":sout={sout}", ":sout-keep", ":no-sout-all"]
    a += ["--loop"] if (st.get("loop") and stype == "file") else ["vlc://quit"]
    a += split_extra(st.get("extra_args"))
    return a


def build_receiver(st):
    """FFmpeg grabs the UDP stream on the chosen adapter and pipes it (untouched) to the player."""
    exe = config["settings"].get("ffmpeg_path") or "ffmpeg"
    return [exe, "-hide_banner", "-nostdin", "-loglevel", "info", "-stats",
            "-fflags", "+nobuffer+discardcorrupt", "-probesize", "2000000", "-analyzeduration", "1500000",
            "-i", udp_in_url(st),
            "-map", "0:v?", "-map", "0:a?", "-c", "copy", "-f", "mpegts", "pipe:1"]


def build_player(st):
    cache = as_int(st.get("network_caching"), 300)
    title = st.get("name") or "Magical Portal"
    if st["engine"] == "ffplay":
        return [sibling_tool("ffplay"), "-hide_banner", "-loglevel", "error", "-fflags", "nobuffer",
                "-flags", "low_delay", "-window_title", title, "-f", "mpegts", "-i", "pipe:0"]
    exe = config["settings"].get("vlc_path") or "vlc"
    return [exe, f"--file-caching={cache}", f"--network-caching={cache}", f"--meta-title={title}",
            "--demux=ts", "-"] + split_extra(st.get("extra_args"))


def validate(st):
    if not (st.get("dest_ip") or "").strip():
        raise BuildError("Enter an IP address.")
    if not valid_ip(st["dest_ip"]):
        raise BuildError(f"'{st['dest_ip']}' is not a valid IP address.")
    if not 1 <= as_int(st["dest_port"], 0) <= 65535:
        raise BuildError("Port must be 1-65535.")
    if (st.get("local_addr") or "").strip() and not valid_ip(st["local_addr"]):
        raise BuildError(f"Network adapter '{st['local_addr']}' is not a valid IP.")


def build_commands(st):
    """Returns a list of commands. Watch mode = [receiver, player] connected by a pipe."""
    st = {**DEFAULT_STREAM, **st}
    validate(st)
    if st["role"] == "watch":
        return [build_receiver(st), build_player(st)]
    return [build_vlc_send(st) if st["engine"] == "vlc" else build_ffmpeg_send(st)]


def cmd_text(args):
    return subprocess.list2cmdline(args) if IS_WINDOWS else " ".join(shlex.quote(x) for x in args)


def commands_text(cmds):
    return "  |  ".join(cmd_text(c) for c in cmds)


# --------------------------------------------------------------------------- #
# Output parsing
# --------------------------------------------------------------------------- #

RE_BITRATE = re.compile(r"bitrate=\s*([\d.]+\s*\w+/s|N/A)")
RE_SIZE = re.compile(r"size=\s*(\d+)")
RE_TIME = re.compile(r"time=\s*(\S+)")
RE_SPEED = re.compile(r"speed=\s*([\d.]+x|N/A)")
RE_FPS = re.compile(r"fps=\s*([\d.]+)")
RE_VIDEO = re.compile(r"Stream #\d+:\d+.*?Video:\s*(\w+)")
RE_RES = re.compile(r"\b(\d{2,5}x\d{2,5})\b")
RE_VFPS = re.compile(r"([\d.]+)\s*fps")


def describe_video(line):
    m = RE_VIDEO.search(line)
    if not m:
        return ""
    res, fps = RE_RES.search(line), RE_VFPS.search(line)
    return " ".join(x for x in (m.group(1), res.group(1) if res else "", f"{fps.group(1)}fps" if fps else "") if x)
RE_AUDIO = re.compile(r"Stream #\d+:\d+.*?Audio:\s*(\w+)")
RE_LAYOUT = re.compile(r"\d+ Hz,\s*([^,]+)")
CH_COUNT = {"mono": 1, "stereo": 2, "2.1": 3, "quad": 4, "5.0": 5, "5.0(side)": 5, "5.1": 6, "5.1(side)": 6,
            "6.1": 7, "7.1": 8, "7.1(wide)": 8, "hexadecagonal": 16}


def describe_audio(lines):
    """['aac stereo', 'aac stereo', ...] -> 'aac 5.1' or '8x aac stereo (16 ch)'"""
    tracks = []
    for l in lines:
        m = RE_AUDIO.search(l)
        if m:
            lay = RE_LAYOUT.search(l)
            tracks.append((m.group(1), lay.group(1).strip() if lay else ""))
    if not tracks:
        return ""
    if len(tracks) == 1:
        return " ".join(x for x in tracks[0] if x)
    total = 0
    for _, lay in tracks:
        n = CH_COUNT.get(lay)
        if n is None:
            m = re.match(r"(\d+) channels", lay)
            n = int(m.group(1)) if m else 0
        total += n
    codec, lay = tracks[0]
    same = all(t == tracks[0] for t in tracks)
    desc = f"{len(tracks)}x {codec} {lay}" if same else f"{len(tracks)} audio tracks"
    return desc + (f" ({total} ch)" if total else "")
RE_FFPLAY_STATUS = re.compile(r"^\s*(nan|-?[\d.]+)\s+[AMV]?-?[AMV]?:?\s*-?[\d.]+\s+fd=")


# --------------------------------------------------------------------------- #
# Process manager
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Windows: put every child process in a "job" that Windows kills automatically
# when this server ends - even if the console window is just closed with the X.
# --------------------------------------------------------------------------- #

_job = None
if IS_WINDOWS:
    try:
        import ctypes
        from ctypes import wintypes

        class _BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("R", "W", "O", "RB", "WB", "OB")]

        class _EXT(ctypes.Structure):
            _fields_ = [("Basic", _BASIC), ("Io", _IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        _k32.CreateJobObjectW.restype = wintypes.HANDLE
        _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        _job = _k32.CreateJobObjectW(None, None)
        _info = _EXT()
        _info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _k32.SetInformationJobObject(_job, 9, ctypes.byref(_info), ctypes.sizeof(_info)):
            _job = None
    except Exception as _e:  # never let this stop the app
        print(f"[warn] job object unavailable: {_e}")
        _job = None


def adopt(proc):
    """Tie a child process to this server's lifetime (Windows)."""
    if _job and proc:
        try:
            _k32.AssignProcessToJobObject(_job, int(proc._handle))
        except Exception:
            pass
    return proc


# --------------------------------------------------------------------------- #
# Plain-English explanations for common FFmpeg / VLC failures
# --------------------------------------------------------------------------- #

KNOWN_ERRORS = [
    (r"minimum required Nvidia driver for nvenc is ([\d.]+)",
     lambda m: f"Your NVIDIA driver is too old for this FFmpeg version (it needs driver {m.group(1)} or newer). "
               "Update the NVIDIA driver, install an older FFmpeg build, or pick CPU / Intel as the Encoder."),
    (r"Driver does not support the required nvenc API version",
     lambda m: "Your NVIDIA driver is too old for this FFmpeg version. Update the NVIDIA driver, "
               "or pick CPU / Intel as the Encoder."),
    (r"OpenEncodeSessionEx failed: (?:out of memory|incompatible client key)|concurrent .*session",
     lambda m: "The NVIDIA encoder is full - GeForce cards allow 8 encodes at once for the whole PC. "
               "Stop another portal, or switch this one to CPU / Intel."),
    (r"Cannot load (?:nvcuda\.dll|libcuda)|No NVENC capable devices found|nvenc.*not available",
     lambda m: "No usable NVIDIA encoder on this PC. Pick CPU / Intel / AMD as the Encoder."),
    (r"Error initializing an internal MFX session|Error creating a MFX session|qsv.*(?:unsupported|failed)",
     lambda m: "Intel Quick Sync isn't available on this PC (or its graphics driver is too old). "
               "Pick CPU or another Encoder."),
    (r"amfrt64\.dll|AMF.*(?:failed|not found|unsupported)",
     lambda m: "The AMD encoder isn't available on this PC. Pick CPU or another Encoder."),
    (r"Unknown encoder '([^']+)'|Encoder (\S+) not found",
     lambda m: f"This FFmpeg build doesn't include the encoder {m.group(1) or m.group(2)}. Pick another Encoder."),
    (r"^(.+?): No such file or directory",
     lambda m: f"Source not found: {m.group(1).strip()} - check the file path."),
    (r"Cannot assign requested address|bind failed",
     lambda m: "The chosen network adapter IP isn't on this PC (any more). Pick the adapter again in Settings / the portal."),
    (r"Could not find audio only device with name \[([^\]]+)\]|Could not find audio device.*?\[([^\]]+)\]",
     lambda m: f"The sound device '{m.group(1) or m.group(2)}' wasn't found (unplugged, renamed or disabled). "
               "Pick it again under Sound, or enable Stereo Mix in Windows Sound settings."),
    (r"Could not find video device|Could not enumerate video devices|I/O error.*dshow",
     lambda m: "The capture device wasn't found or is in use by another program. Use 'Find capture devices'."),
    (r"udp://[^\s]*.*?(?:Connection timed out|Input/output error|Immediate exit)|Error opening input.*udp",
     lambda m: f"No data from the input stream for {RELAY_WAIT_S} s - check it is running and that the network "
               "adapter is right (use Test). The portal keeps retrying."),
    (r"Error submitting a packet to the muxer: Cannot allocate memory",
     lambda m: "The packet pacing of 'Smooth sending' overflowed on this PC. The portal restarts with pacing "
               "off (constant bitrate stays on). If it keeps happening, untick Smooth sending."),
    (r"Connection refused|Connection timed out|Server returned 404|Network is unreachable",
     lambda m: "The source URL couldn't be reached - check the address and that the source is running."),
]


def explain_failure(lines):
    """Return a plain-English reason from the last run's log, or ''."""
    text = "\n".join(lines[-80:])
    for pattern, msg in KNOWN_ERRORS:
        m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
        if m:
            return msg(m)
    return ""


def kill_tree(p):
    if not p or p.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                           capture_output=True, creationflags=CREATE_NO_WINDOW)
        else:
            p.terminate()
        p.wait(timeout=5)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


class Runner:
    def __init__(self, sid, getter=None):
        self.sid = sid
        self.getter = getter or (lambda: get_stream(sid))
        self.proc = None       # sender, or receiver in watch mode
        self.player = None     # watch mode only
        self.log = deque(maxlen=400)
        self.status = "stopped"
        self.started_at = None
        self.restarts = 0
        self.quick_fails = 0
        self.pace_off = False
        self.exit_code = None
        self.progress = {}
        self.media = ""
        self.last_data = 0
        self.want_running = False
        self.last_error = ""
        self.lock = threading.RLock()
        self._section = ""
        self._media_from = "in"
        self._video = self._audio = ""
        self._audio_lines = []
        self._mark = None
        self.input_seen = False

    # ---- log handling
    def add_log(self, line):
        line = line.rstrip()
        if not line or RE_FFPLAY_STATUS.match(line):
            return
        if line.startswith("Input #"):
            self._section = "in"
            self.input_seen = True
        elif line.startswith("Output #"):
            self._section = "out"
        elif line.startswith("Stream mapping"):
            self._section = ""
        if self._section == self._media_from:
            v = describe_video(line)
            if v:
                self._video = v
            ma = RE_AUDIO.search(line)
            if ma:
                self._audio_lines.append(line)
                self._audio = describe_audio(self._audio_lines)
            if v or ma:
                self.media = " + ".join(x for x in (self._video, self._audio) if x)
        if "time=" in line and "bitrate=" in line:
            b, s, f = RE_BITRATE.search(line), RE_SPEED.search(line), RE_FPS.search(line)
            new = {"bitrate": b.group(1) if b else "", "speed": s.group(1) if s else "", "fps": f.group(1) if f else ""}
            sz, tm = RE_SIZE.search(line), RE_TIME.search(line)
            mark = (sz.group(1) if sz else "", tm.group(1) if tm else "")
            if mark != self._mark and mark[1] not in ("", "N/A"):
                self._mark = mark            # bytes / timestamp moved on = real data arrived
                self.last_data = time.time()
            self.progress = new
            if self.log and "time=" in self.log[-1] and "bitrate=" in self.log[-1]:
                self.log[-1] = line
                return
        if self.log and self.log[-1] == line:
            return
        self.log.append(line)

    # ---- lifecycle
    def start(self, is_restart=False):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return True, "already running"
            st = self.getter()
            if not st:
                return False, "stream not found"
            st = {**DEFAULT_STREAM, **st}
            if self.pace_off:
                st["_no_pace"] = True
            try:
                cmds = build_commands(st)
            except BuildError as e:
                self.status, self.last_error = "error", str(e)
                return False, str(e)
            for c in cmds:
                if not (os.path.isfile(c[0]) or shutil.which(c[0])):
                    msg = f"Program not found: {c[0]}. Set the FFmpeg/VLC path in Settings."
                    self.status, self.last_error = "error", msg
                    return False, msg
            if not is_restart:
                self.log.clear()
                self.restarts = 0
                self.quick_fails = 0
                self.pace_off = False
            self.log.append(f"$ {commands_text(cmds)}")
            self.media, self.progress, self.last_data, self._section = "", {}, 0, ""
            self.input_seen = False
            self._video = self._audio = ""
            self._audio_lines = []
            self._mark = None
            self._media_from = "in" if st["role"] == "watch" else "out"
            common = dict(text=True, encoding="utf-8", errors="replace", bufsize=1)
            try:
                if st["role"] == "watch":
                    # binary pipe: ffmpeg (receiver) stdout -> player stdin
                    rx = subprocess.Popen(cmds[0], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
                    pl = subprocess.Popen(cmds[1], stdin=rx.stdout, stdout=subprocess.DEVNULL,
                                          stderr=subprocess.DEVNULL)
                    adopt(rx)
                    adopt(pl)
                    rx.stdout.close()  # player owns the read end now
                    self.proc, self.player = rx, pl
                    log_stream = io.TextIOWrapper(rx.stderr, encoding="utf-8", errors="replace")
                    threading.Thread(target=self._watch_player, args=(rx, pl), daemon=True).start()
                else:
                    self.proc = subprocess.Popen(cmds[0], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                 stderr=subprocess.STDOUT, creationflags=CREATE_NO_WINDOW,
                                                 **common)
                    self.player = None
                    adopt(self.proc)
                    log_stream = self.proc.stdout
            except Exception as e:
                self.status, self.last_error = "error", str(e)
                return False, str(e)
            self.want_running = True
            self.status = "running"
            self.started_at = time.time()
            self.exit_code = None
            self.last_error = ""
            threading.Thread(target=self._reader, args=(self.proc, log_stream), daemon=True).start()
            return True, "started"

    def _watch_player(self, rx, pl):
        pl.wait()
        if rx.poll() is None:  # user closed the VLC/FFplay window
            with self.lock:
                if self.proc is rx:
                    self.want_running = False
                    self.log.append("[player window closed]")
            kill_tree(rx)

    def _reader(self, proc, stream):
        try:
            for line in stream:
                for part in line.split("\r"):
                    self.add_log(part)
        except Exception:
            pass
        code = proc.wait()
        with self.lock:
            if proc is not self.proc:
                return
            self.exit_code = code
            self.log.append(f"[process exited with code {code}]")
            kill_tree(self.player)
            st = self.getter()
            ran = time.time() - (self.started_at or time.time())
            if code != 0 and any("Cannot allocate memory" in l for l in list(self.log)[-30:]) and not self.pace_off:
                self.pace_off = True    # packet pacing overflowed on this PC: keep CBR, drop the pacing
                self.log.append("[smooth sending: packet pacing overflowed on this PC - restarting without "
                                "pacing (constant bitrate stays on)]")
                self.quick_fails = 0
                self.last_error = ""
            self.quick_fails = self.quick_fails + 1 if (ran < 10 and code != 0) else 0
            reason = explain_failure(list(self.log)) if code != 0 else ""
            if self.want_running and self.quick_fails >= 3:
                # it can't start - restarting forever won't help
                self.status, self.want_running = "error", False
                self.last_error = (reason or f"Failed to start 3 times in a row (exit code {code}) - open the Log.") \
                    + "  Auto-restart paused; press Start after fixing it."
                self.log.append("[auto-restart paused: the portal fails right after starting]")
            elif self.want_running and st and st.get("auto_restart") and st.get("role") == "send":
                self.status = "restarting"
                delay = as_int(config["settings"].get("restart_delay"), 3)
                threading.Timer(delay, self._auto_restart).start()
            else:
                self.status = "stopped" if (code == 0 or not self.want_running) else "error"
                if self.status == "error":
                    self.last_error = reason or f"Exited with code {code} - open the log for details."
                self.want_running = False

    def _auto_restart(self):
        with self.lock:
            if not self.want_running:
                self.status = "stopped"
                return
            self.restarts += 1
            self.log.append(f"[auto-restart #{self.restarts}]")
            self.start(is_restart=True)

    def stop(self):
        with self.lock:
            self.want_running = False
            p, pl = self.proc, self.player
            if (not p or p.poll() is not None) and (not pl or pl.poll() is not None):
                self.status = "stopped"
                return
            kill_tree(pl)
            kill_tree(p)
            self.status = "stopped"
            self.log.append("[stopped by user]")

    def info(self):
        running = bool(self.proc and self.proc.poll() is None)
        if running:
            if not self.last_data:
                signal = "starting" if self.input_seen else "waiting"
            elif time.time() - self.last_data > SIGNAL_LOST_AFTER:
                signal = "lost"
            else:
                signal = "ok"
        else:
            signal = ""
        return {
            "status": self.status if (running or self.status != "running") else "stopped",
            "pid": self.proc.pid if running else None,
            "uptime": int(time.time() - self.started_at) if running and self.started_at else 0,
            "restarts": self.restarts,
            "exit_code": self.exit_code,
            "progress": self.progress if running else {},
            "media": self.media if running else "",
            "signal": signal,
            "pace_off": self.pace_off,
            "error": self.last_error,
        }

    def get_log(self):
        lines = list(self.log)
        vlc_log = os.path.join(LOG_DIR, self.sid + "_vlc.log")
        st = self.getter()
        if st and st.get("engine") == "vlc" and st.get("role") == "send" and os.path.isfile(vlc_log):
            try:
                with open(vlc_log, "r", encoding="utf-8", errors="replace") as f:
                    lines += ["--- VLC log ---"] + f.read().splitlines()[-150:]
            except Exception:
                pass
        return lines


runners = {}
runners_lock = threading.Lock()


def runner(sid):
    with runners_lock:
        if sid not in runners:
            runners[sid] = Runner(sid)
        return runners[sid]


def preview_runner(sid):
    key = sid + "~preview"
    with runners_lock:
        if key not in runners:
            def getter():
                st = get_stream(sid)
                if not st:
                    return None
                return dict(st, role="watch", engine="vlc", extra_args="",
                            name="Preview: " + st["name"], auto_restart=False)
            runners[key] = Runner(key, getter)
        return runners[key]


def start_all_senders():
    errors = []
    for s in list(config["streams"]):
        if s["role"] == "send":
            ok, msg = runner(s["id"]).start()
            if not ok:
                errors.append(f"{s['name']}: {msg}")
    return errors


def status_line():
    streams = list(config["streams"])
    running = sum(1 for s in streams if runner(s["id"]).info()["status"] in ("running", "restarting"))
    lost = sum(1 for s in streams if runner(s["id"]).info()["signal"] == "lost")
    text = f"{running} of {len(streams)} portal{'s' if len(streams) != 1 else ''} running"
    return text + (f" - {lost} with no signal!" if lost else "")


def monitor_loop(notify):
    """Watch every portal and raise a Windows pop-up when something goes wrong."""
    last = {}
    while True:
        time.sleep(2)
        try:
            for s in list(config["streams"]):
                inf = runner(s["id"]).info()
                now = (inf["status"], inf["signal"])
                prev = last.get(s["id"])
                last[s["id"]] = now
                if prev is None or time.time() < QUIET_UNTIL or not config["settings"].get("tray_notifications", True):
                    continue
                name = s["name"]
                if prev[1] in ("ok", "starting") and now[1] == "lost":
                    notify(f"{name}: signal lost", "No data for 3+ seconds.", True)
                elif prev[1] == "lost" and now[1] == "ok":
                    notify(f"{name}: signal back", "The stream is flowing again.", False)
                elif prev[0] in ("running", "restarting") and now[0] == "error":
                    notify(f"{name} stopped", inf.get("error") or "Open the log for details.", True)
        except Exception as e:
            print(f"[monitor] {e}")


QUIET_UNTIL = 0  # no pop-ups while everything is being stopped on purpose


def stop_all():
    global QUIET_UNTIL
    QUIET_UNTIL = time.time() + 10
    # receivers first, so they don't report "signal lost" while the senders shut down
    rs = sorted(runners.values(), key=lambda r: 0 if (r.getter() or {}).get("role") == "watch" else 1)
    for r in rs:
        r.stop()


# --------------------------------------------------------------------------- #
# Signal test
# --------------------------------------------------------------------------- #

def probe_stream(st, seconds=6):
    st = {**DEFAULT_STREAM, **st}
    validate(st)
    exe = config["settings"].get("ffmpeg_path") or "ffmpeg"
    url = udp_in_url(st, timeout_us=seconds * 1_000_000)
    args = [exe, "-hide_banner", "-nostdin", "-probesize", "2000000", "-analyzeduration", "1500000",
            "-i", url, "-t", "3", "-map", "0:v?", "-map", "0:a?",
            "-c", "copy", "-f", "null", "-"]
    nic = adapter_ip(st)
    where = f"{st['dest_ip'].strip()}:{st['dest_port']}"
    on = f"adapter {nic}" if nic else "the Windows default adapter"
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=seconds + 10, creationflags=CREATE_NO_WINDOW)
        err = r.stderr
    except subprocess.TimeoutExpired:
        err = ""
    except FileNotFoundError:
        return {"ok": False, "signal": False, "message": "FFmpeg not found - set its path in Settings."}

    video = next((describe_video(l) for l in err.splitlines() if RE_VIDEO.search(l)), "")
    in_lines = err.split("Output #")[0].splitlines()
    audio = describe_audio(in_lines)
    br = RE_BITRATE.findall(err)
    if video or audio:
        parts = []
        if video:
            parts.append(f"Video {video}")
        if audio:
            parts.append(f"Audio {audio}")
        if br and br[-1] != "N/A":
            parts.append(f"~{br[-1]}")
        return {"ok": True, "signal": True,
                "message": f"✅ Signal found on {where} via {on}: " + " · ".join(parts)}

    tips = []
    if is_multicast(st["dest_ip"]):
        tips.append("Pick the network adapter that is on the same network as the stream source.")
        tips.append("Allow FFmpeg/VLC through Windows Firewall (Settings → Firewall commands), and make sure the network is set to Private, not Public.")
        tips.append("Check the switch has IGMP snooping + an IGMP querier, or that the source really is sending to this group/port.")
        tips.append("If the source uses Source-Specific Multicast (232.x.x.x), enter the sender's IP in 'Only from sender'.")
    else:
        tips.append("For unicast the sender must send to THIS PC's IP on that port.")
        tips.append("Allow FFmpeg/VLC through Windows Firewall (Settings → Firewall commands).")
    detail = ""
    for line in err.splitlines():
        if "error" in line.lower() or "timed out" in line.lower() or "cannot" in line.lower():
            detail = line.strip()
            break
    return {"ok": True, "signal": False,
            "message": f"❌ No data on {where} via {on} in {seconds} s." + (f"\nFFmpeg said: {detail}" if detail else ""),
            "tips": tips}


# --------------------------------------------------------------------------- #
# Device discovery & firewall
# --------------------------------------------------------------------------- #

_enc_cache = {"ffmpeg": None, "result": None, "running": False}
ENCODERS = ["libx264", "h264_nvenc", "h264_qsv", "h264_amf"]


def check_encoders(force=False):
    """Try a tiny real encode with each H.264 encoder. ~1-3 s total; cached."""
    exe = config["settings"].get("ffmpeg_path") or "ffmpeg"
    if not force and _enc_cache["ffmpeg"] == exe and _enc_cache["result"] is not None:
        return _enc_cache["result"]
    result = {}
    for enc in ENCODERS:
        args = [exe, "-hide_banner", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                "color=c=black:s=640x360:r=30:d=0.5", "-frames:v", "5", "-c:v", enc, "-f", "null", "-"]
        try:
            r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=20, creationflags=CREATE_NO_WINDOW)
            if r.returncode == 0:
                result[enc] = {"ok": True, "reason": ""}
            else:
                lines = r.stderr.splitlines()
                result[enc] = {"ok": False,
                               "reason": explain_failure(lines) or (lines[-1] if lines else "failed")}
        except FileNotFoundError:
            result[enc] = {"ok": False, "reason": "FFmpeg not found - set its path in Settings."}
        except Exception as e:
            result[enc] = {"ok": False, "reason": str(e)}
    _enc_cache.update(ffmpeg=exe, result=result)
    return result


def list_dshow_devices():
    exe = config["settings"].get("ffmpeg_path") or "ffmpeg"
    if not IS_WINDOWS:
        return {"video": [], "audio": [], "note": "Device listing only works on Windows (DirectShow)."}
    try:
        r = subprocess.run([exe, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=15, creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        return {"video": [], "audio": [], "note": str(e)}
    video, audio, section = [], [], None
    for line in r.stderr.splitlines():
        if "DirectShow video devices" in line:
            section = "video"
            continue
        if "DirectShow audio devices" in line:
            section = "audio"
            continue
        if "Alternative name" in line:
            continue
        m = re.search(r'"([^"]+)"\s*(\((video|audio)\))?', line)
        if m:
            kind = m.group(3) or section
            (video if kind == "video" else audio if kind == "audio" else []).append(m.group(1))
    return {"video": video, "audio": audio, "note": ""}


_browse_lock = threading.Lock()
BROWSE_TYPES = {
    "video": [("Video files", "*.mp4 *.mov *.mkv *.ts *.m2ts *.mts *.avi *.wmv *.webm *.mpg *.mpeg *.m4v"),
              ("All files", "*.*")],
    "image": [("Pictures", "*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff"), ("All files", "*.*")],
}


def browse_file(kind):
    """Open a normal Windows 'Open file' window on this PC and return the chosen path."""
    if not _browse_lock.acquire(blocking=False):
        return {"ok": False, "error": "A file browser is already open - check the taskbar."}
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        path = filedialog.askopenfilename(parent=root, title=f"Magical Portal System - choose a {kind}",
                                          filetypes=BROWSE_TYPES.get(kind, [("All files", "*.*")]))
        root.destroy()
        return {"ok": True, "path": os.path.normpath(path) if path else ""}
    except Exception as e:
        return {"ok": False, "error": f"Can't open a file browser here ({e}). Type or paste the full path instead."}
    finally:
        _browse_lock.release()


def firewall_commands():
    progs = []
    for label, path in (("FFmpeg", config["settings"].get("ffmpeg_path")),
                        ("FFplay", sibling_tool("ffplay")),
                        ("VLC", config["settings"].get("vlc_path"))):
        if path and os.path.isfile(path):
            progs.append((label, os.path.realpath(path)))  # WinGet "Links" are shortcuts; rules need the real exe
    lines = [f'netsh advfirewall firewall add rule name="Magical Portal - {label}" dir=in action=allow '
             f'program="{path}" protocol=UDP profile=any enable=yes' for label, path in progs]
    if WEB["host"] != "127.0.0.1":
        lines.append('netsh advfirewall firewall delete rule name="Magical Portal - Web page"')
        lines.append(f'netsh advfirewall firewall add rule name="Magical Portal - Web page" dir=in action=allow '
                     f'protocol=TCP localport={WEB["port"]} profile=private,domain enable=yes')
        lines.append('netsh advfirewall firewall delete rule name="Magical Portal - Name lookup"')
        lines.append('netsh advfirewall firewall add rule name="Magical Portal - Name lookup" dir=in action=allow '
                     'protocol=UDP localport=5353,5355 profile=private,domain enable=yes')
    return {"commands": lines}


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #

MIME = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
        ".css": "text/css", ".svg": "image/svg+xml", ".ico": "image/x-icon", ".png": "image/png"}


def state_payload():
    with config_lock:
        streams = []
        for s in config["streams"]:
            item = dict(s)
            item["runtime"] = runner(s["id"]).info()
            item["preview_running"] = preview_runner(s["id"]).info()["status"] == "running" if s["role"] == "send" else False
            try:
                item["command"] = commands_text(build_commands(s))
            except BuildError as e:
                item["command"] = f"(incomplete: {e})"
            item["brightsign_url"] = brightsign_url(s)
            item["adapter_used"] = adapter_ip(s)
            streams.append(item)
        s = config["settings"]

        def ok(p):
            return bool(p and (os.path.isfile(p) or shutil.which(p)))
        return {
            "settings": {k: v for k, v in s.items() if not k.startswith("web_password")},
            "streams": streams,
            "tools": {"ffmpeg": ok(s.get("ffmpeg_path")), "vlc": ok(s.get("vlc_path"))},
            "platform": "windows" if IS_WINDOWS else sys.platform,
            "version": __version__,
            "web": {"port": WEB["port"], "access": s.get("web_access", "local"), "host": WEB["host"],
                    "name": s.get("web_name", ""), "name_works": name_works_here(), "page_url": web_url(),
                    "responder": bool(WEB["responder"]),
                    "urls": network_urls(), "password_set": bool(s.get("web_password_hash")),
                    "error": WEB["error"]},
        }


def clean_stream(body, existing=None):
    base = dict(existing or DEFAULT_STREAM)
    for k, default in DEFAULT_STREAM.items():
        if k in body and k != "id":
            v = body[k]
            if isinstance(default, bool):
                v = bool(v)
            elif isinstance(default, int):
                v = as_int(v, base.get(k, default))
            else:
                v = "" if v is None else str(v).strip()
            base[k] = v
    return base


# --------------------------------------------------------------------------- #
# Control page address (port / who can reach it / password)
# --------------------------------------------------------------------------- #

import base64
import hashlib
import hmac
import secrets

import netname

WEB = {"httpd": None, "host": "127.0.0.1", "port": 8080, "rebind": None, "quit": False, "error": "",
       "responder": None, "name_msg": ""}


class PortalHTTPServer(ThreadingHTTPServer):
    """Windows lets two programs share a port when SO_REUSEADDR is set (Python's default),
    so on Windows claim the port exclusively - a second copy then fails and just opens the page."""
    allow_reuse_address = not IS_WINDOWS
    daemon_threads = True

    def server_bind(self):
        if IS_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def port_free(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if IS_WINDOWS and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # ignore leftover TIME_WAIT
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def page_name():
    n = (config["settings"].get("web_name") or "").strip()
    return n if netname.valid_name(n) else ""


_name_cache = {"key": None, "t": 0, "ok": False}


def name_works_here():
    """Does http://<name> reach this PC? (Windows: the hosts entry; cached so polling stays fast)"""
    n = page_name()
    if not n:
        return False
    if IS_WINDOWS:
        return netname.hosts_has(n)
    if _name_cache["key"] != n or time.time() - _name_cache["t"] > 30:
        _name_cache.update(key=n, t=time.time(), ok=netname.resolves_locally(n))
    return _name_cache["ok"]


def update_responder():
    """Answer name look-ups from other devices only while the page is open to the network."""
    want = WEB["host"] == "0.0.0.0" and bool(page_name())
    r = WEB["responder"]
    if want and not r:
        r = netname.NameResponder(page_name, list_adapters)
        r.start()
        WEB["responder"] = r
        if r.error:
            print(f"[name] {r.error}")
    elif not want and r:
        r.stop()
        WEB["responder"] = None


def bind_host(access):
    return "0.0.0.0" if access == "network" else "127.0.0.1"


def web_url(host=None, port=None):
    """Address to open on this PC: the friendly name if Windows knows it, else localhost."""
    port = port or WEB["port"]
    shown = page_name() if name_works_here() else "localhost"
    return f"http://{shown}:{port}"


def network_urls():
    """Addresses other devices can type in."""
    if WEB["host"] == "127.0.0.1":
        return []
    port = WEB["port"]
    urls = []
    if WEB["host"] == "0.0.0.0":
        n = page_name()
        if n:
            urls += [f"http://{n}:{port}", f"http://{n}.local:{port}"]
        try:
            urls.append(f"http://{socket.gethostname()}:{port}")
        except Exception:
            pass
        urls += [f"http://{a['ip']}:{port}" for a in list_adapters() if not a.get("apipa")]
    else:
        urls.append(f"http://{WEB['host']}:{port}")
    return urls


def hash_password(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt.encode("utf-8"), 200_000).hex()


def check_password(pw):
    s = config["settings"]
    if not s.get("web_password_hash"):
        return True
    return hmac.compare_digest(hash_password(pw, s.get("web_password_salt", "")), s["web_password_hash"])


def request_rebind(host, port):
    """Move the control page to a new address without stopping any streams."""
    WEB["rebind"] = (host, port)
    threading.Thread(target=lambda: (time.sleep(0.4), WEB["httpd"].shutdown()), daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "MagicalPortalSystem/" + __version__

    def log_message(self, fmt, *args):
        pass

    def send_json(self, obj, code=200):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def same_origin(self):
        origin = self.headers.get("Origin")
        return not origin or urlparse(origin).netloc == self.headers.get("Host")

    def is_local_client(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def authorized(self):
        """Other devices need the password (if one is set). This PC never does."""
        if self.is_local_client() or not config["settings"].get("web_password_hash"):
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                _, _, pw = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
                if check_password(pw):
                    return True
            except Exception:
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Magical Portal System", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):
        if not self.authorized():
            return
        path = urlparse(self.path).path
        if path == "/api/state":
            return self.send_json(state_payload())
        m = re.fullmatch(r"/api/streams/([\w-]+)/log", path)
        if m:
            return self.send_json({"lines": runner(m.group(1)).get_log()})
        if path == "/api/monitors":
            return self.send_json({"monitors": list_monitors(), "platform": "windows" if IS_WINDOWS else sys.platform})
        if path == "/api/screen-preview":
            from urllib.parse import parse_qs
            src = (parse_qs(urlparse(self.path).query).get("src") or ["desktop"])[0]
            try:
                exe = config["settings"].get("ffmpeg_path") or "ffmpeg"
                args = [exe, "-hide_banner", "-loglevel", "error"] + gdigrab_input(src, 5, True) + \
                       ["-frames:v", "1", "-vf", "scale=640:-2", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "5", "-"]
                r = subprocess.run(args, capture_output=True, timeout=12, creationflags=CREATE_NO_WINDOW)
                if r.returncode != 0 or not r.stdout:
                    raise BuildError((r.stderr.decode(errors="replace").strip().splitlines() or ["capture failed"])[-1])
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(r.stdout)))
                self.end_headers()
                self.wfile.write(r.stdout)
            except Exception as e:
                msg = str(e).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
            return
        if path == "/api/devices":
            return self.send_json(list_dshow_devices())
        if path == "/api/encoders":
            force = "recheck" in (urlparse(self.path).query or "")
            return self.send_json({"encoders": check_encoders(force)})
        if path == "/api/adapters":
            return self.send_json({"adapters": list_adapters(force=True)})
        if path == "/api/firewall":
            return self.send_json(firewall_commands())
        if path == "/api/detect":
            return self.send_json({"ffmpeg_path": detect_tool("ffmpeg", COMMON_FFMPEG),
                                   "vlc_path": detect_tool("vlc", COMMON_VLC)})
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        full = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            self.send_error(404)
            return
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(os.path.splitext(full)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        if not self.authorized():
            return
        if not self.same_origin():
            return self.send_json({"ok": False, "error": "cross-origin request refused"}, 403)
        path = urlparse(self.path).path
        b = self.body()

        if path == "/api/web-settings":
            port = as_int(b.get("port"), 0)
            if not 1024 <= port <= 65535:
                return self.send_json({"ok": False, "error": "Port must be between 1024 and 65535."})
            access = str(b.get("access") or "local").strip()
            if access not in ("local", "network"):
                return self.send_json({"ok": False, "error": "Unknown access option."})
            if not self.is_local_client() and access == "local":
                return self.send_json({"ok": False, "error": "'This PC only' can only be chosen on the PC itself, otherwise you'd lock yourself out."})
            name = str(b.get("name", config["settings"].get("web_name", "")) or "").strip()
            if name and not netname.valid_name(name):
                return self.send_json({"ok": False, "error": "Name: letters, numbers and - only, starting with a letter (e.g. PortalSystem)."})
            host = bind_host(access)
            if (host, port) != (WEB["host"], WEB["port"]):
                # make sure the new address is free before switching
                if port != WEB["port"] and not port_free(host, port):
                        return self.send_json({"ok": False, "error": f"Port {port} is already used by another program. Pick a different one."})
            with config_lock:
                s = config["settings"]
                name_changed = name.lower() != (s.get("web_name") or "").lower()
                s["web_port"], s["web_access"], s["web_name"] = port, access, name
                if "password" in b and b["password"] is not None:
                    pw = str(b["password"])
                    if pw:
                        s["web_password_salt"] = secrets.token_hex(16)
                        s["web_password_hash"] = hash_password(pw, s["web_password_salt"])
                    else:
                        s["web_password_salt"] = s["web_password_hash"] = ""
                save_config()
            moved = (host, port) != (WEB["host"], WEB["port"])
            note = ""
            if name and IS_WINDOWS and not netname.hosts_has(name):
                started, note = netname.request_hosts_entry(name)
            if not moved:
                update_responder()
            self.send_json({"ok": True, "moved": moved, "port": port, "host": host, "name": name,
                            "name_works": name_works_here(), "note": note})
            if moved:
                request_rebind(host, port)
            return

        if path == "/api/web-name-fix":
            n = page_name()
            if not n:
                return self.send_json({"ok": False, "error": "Set a name first."})
            started, msg = netname.request_hosts_entry(n)
            return self.send_json({"ok": started, "error": "" if started else msg, "note": msg if started else ""})

        if path == "/api/settings":
            with config_lock:
                for k in DEFAULT_SETTINGS:
                    if k in b and not k.startswith("web_"):
                        config["settings"][k] = b[k]
                save_config()
            return self.send_json({"ok": True})

        if path == "/api/command":
            try:
                st = clean_stream(b)
                st["id"] = b.get("id") or "preview"
                return self.send_json({"ok": True, "command": commands_text(build_commands(st))})
            except BuildError as e:
                return self.send_json({"ok": False, "error": str(e)})

        if path.startswith("/api/browse/"):
            return self.send_json(browse_file(path.rsplit("/", 1)[-1]))

        if path == "/api/probe":
            try:
                st = clean_stream(b)
                # unicast ports can only be opened by one program: if a portal is already
                # receiving this port, report its live status instead of fighting over the port
                if not is_multicast(st["dest_ip"]):
                    for key, r in list(runners.items()):
                        cfg = r.getter() or {}
                        inf = r.info()
                        if (cfg.get("role") == "watch" and inf["status"] == "running"
                                and as_int(cfg.get("dest_port"), 0) == st["dest_port"]):
                            p = inf["progress"]
                            if inf["signal"] in ("ok", "starting"):
                                msg = (f"✅ '{cfg.get('name')}' is already receiving port {st['dest_port']}: "
                                       f"{inf['media']}" + (f" · {p.get('bitrate')}" if p.get("bitrate") else ""))
                            else:
                                msg = (f"❌ '{cfg.get('name')}' is listening on port {st['dest_port']} "
                                       f"but no data is arriving ({inf['signal']}).")
                            return self.send_json({"ok": True, "signal": inf["signal"] in ("ok", "starting"),
                                                   "message": msg})
                return self.send_json(probe_stream(st))
            except BuildError as e:
                return self.send_json({"ok": False, "error": str(e)})

        if path == "/api/streams":
            restart = bool(b.pop("restart_if_running", True))
            with config_lock:
                sid = b.get("id")
                existing = get_stream(sid) if sid else None
                st = clean_stream(b, existing)
                if existing:
                    existing.update(st)
                    st = existing
                else:
                    st["id"] = new_id()
                    config["streams"].append(st)
                save_config()
            r = runner(st["id"])
            restarted = False
            if restart and r.info()["status"] in ("running", "restarting"):
                r.stop()
                r.start()
                restarted = True
            return self.send_json({"ok": True, "id": st["id"], "restarted": restarted})

        if path == "/api/shutdown":
            self.send_json({"ok": True})

            def _down():
                time.sleep(0.5)
                stop_all()
                WEB["quit"] = True
                if WEB["httpd"]:
                    WEB["httpd"].shutdown()
            threading.Thread(target=_down, daemon=True).start()
            return

        if path == "/api/stop-all":
            stop_all()
            return self.send_json({"ok": True})

        if path == "/api/start-all":
            errors = start_all_senders()
            return self.send_json({"ok": not errors, "error": "\n".join(errors)})

        m = re.fullmatch(r"/api/streams/([\w-]+)/(start|stop|restart|preview|duplicate|delete)", path)
        if m:
            sid, action = m.groups()
            st = get_stream(sid)
            if not st:
                return self.send_json({"ok": False, "error": "stream not found"}, 404)
            r = runner(sid)
            if action == "start":
                ok, msg = r.start()
                return self.send_json({"ok": ok, "error": "" if ok else msg})
            if action == "stop":
                r.stop()
                return self.send_json({"ok": True})
            if action == "restart":
                r.stop()
                ok, msg = r.start()
                return self.send_json({"ok": ok, "error": "" if ok else msg})
            if action == "preview":
                pr = preview_runner(sid)
                if pr.info()["status"] == "running":
                    pr.stop()
                    return self.send_json({"ok": True})
                ok, msg = pr.start()
                note = "" if is_multicast(st["dest_ip"]) else (
                    "Unicast stream: this PC only sees it if the destination IP is this PC.")
                return self.send_json({"ok": ok, "error": "" if ok else msg, "note": note if ok else ""})
            if action == "duplicate":
                with config_lock:
                    copy = dict(st, id=new_id(), name=st["name"] + " (copy)", autostart=False)
                    copy["dest_port"] = as_int(st["dest_port"], 5000) + 2
                    config["streams"].append(copy)
                    save_config()
                return self.send_json({"ok": True, "id": copy["id"]})
            if action == "delete":
                r.stop()
                preview_runner(sid).stop()
                with config_lock:
                    config["streams"] = [s for s in config["streams"] if s["id"] != sid]
                    save_config()
                return self.send_json({"ok": True})

        self.send_json({"ok": False, "error": "unknown endpoint"}, 404)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main():
    # Started hidden (pythonw / desktop icon): there is no console, so write output to a log file
    if sys.stdout is None or sys.stderr is None or "--hidden" in sys.argv:
        f = open(os.path.join(LOG_DIR, "server.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = f
        print(f"\n--- started {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
    ap = argparse.ArgumentParser(description="Magical Portal System")
    ap.add_argument("--host", default=None, help="override Settings: 127.0.0.1 = this PC only, 0.0.0.0 = whole network")
    ap.add_argument("--port", type=int, default=None, help="override Settings: control page port")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-tray", action="store_true", help="don't show the Windows tray icon")
    ap.add_argument("--hidden", action="store_true", help="no console output (log goes to logs/server.log)")
    args = ap.parse_args()

    import atexit
    import signal as _signal
    import webbrowser
    atexit.register(stop_all)

    def _bye(*_):
        raise KeyboardInterrupt
    for sig in ("SIGTERM", "SIGBREAK", "SIGHUP"):
        if hasattr(_signal, sig):
            try:
                _signal.signal(getattr(_signal, sig), _bye)
            except Exception:
                pass

    load_config()
    s = config["settings"]
    host = args.host or bind_host(s.get("web_access", "local"))
    port = args.port or as_int(s.get("web_port"), 8080)

    try:
        httpd = PortalHTTPServer((host, port), Handler)
    except OSError:
        # Already running (e.g. launched twice) - just open the page and quit
        url = web_url(host, port)
        print(f"Port {port} is busy - Magical Portal System is probably already running. Opening {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return
    WEB.update(httpd=httpd, host=host, port=port)
    update_responder()

    for st in config["streams"]:
        if st.get("autostart") and st["role"] == "send":
            ok, msg = runner(st["id"]).start()
            print(f"[autostart] {st['name']}: {'started' if ok else msg}")

    print("=" * 60)
    print(f" Magical Portal System v{__version__} running")
    print(f"   Open: {web_url()}")
    for u in network_urls():
        print(f"   From other devices: {u}" + ("" if s.get("web_password_hash") else "  (no password set!)"))
    print(f"   FFmpeg: {s['ffmpeg_path'] or 'NOT FOUND - set it in Settings'}")
    print(f"   VLC:    {s['vlc_path'] or 'NOT FOUND - set it in Settings'}")
    print("   Network adapters:")
    for a in list_adapters():
        print(f"     {a['ip']:<16} {a['name']}")
    print("   Stop it with the Shut down button in the page, or Ctrl+C here.")
    print("=" * 60)
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(web_url())).start()

    def _quit():
        stop_all()
        WEB["quit"] = True
        WEB["httpd"].shutdown()

    tray = None
    if IS_WINDOWS and not args.no_tray:
        try:
            import tray as tray_mod
            tray = tray_mod.TrayIcon(
                icon_path=os.path.join(WEB_DIR, "portal.ico"),
                on_open=lambda: webbrowser.open(web_url()),   # always the current address
                on_start_all=start_all_senders,
                on_stop_all=stop_all,
                on_quit=_quit,
                status_text=status_line)
            tray.start()
            threading.Thread(target=monitor_loop, args=(tray.notify,), daemon=True).start()
            print("   Tray icon added (look in the hidden icons ^ next to the clock).")
        except Exception as e:
            print(f"[warn] tray icon not available: {e}")
            tray = None
    try:
        while True:
            WEB["httpd"].serve_forever()
            if WEB["quit"] or not WEB["rebind"]:
                break
            # move the control page to a new address; streams keep running
            new_host, new_port = WEB["rebind"]
            WEB["rebind"] = None
            old = WEB["httpd"]
            old_addr = (WEB["host"], WEB["port"])
            old.server_close()
            try:
                WEB["httpd"] = PortalHTTPServer((new_host, new_port), Handler)
                WEB.update(host=new_host, port=new_port, error="")
                print(f"[web] control page moved to {web_url()}")
                update_responder()
            except OSError as e:
                WEB["httpd"] = PortalHTTPServer(old_addr, Handler)
                WEB["error"] = f"Could not use {new_host}:{new_port} ({e}) - stayed on port {old_addr[1]}."
                print(f"[web] {WEB['error']}")
    except KeyboardInterrupt:
        pass
    finally:
        print("Stopping all streams...")
        stop_all()
        if tray:
            tray.stop()
        if WEB["responder"]:
            WEB["responder"].stop()
        try:
            WEB["httpd"].server_close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
