"""
'Magical Portal' tab: shows the Magical Portal System control page inside the Portal System window.

The portal server is a separate background program (server.py, with its tray icon), so streams keep
running when this window closes. This tab finds the install, starts the server if needed and embeds
its page (Qt WebEngine). Without WebEngine it falls back to opening the page in the browser.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.request

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QMessageBox, QPushButton, QStackedWidget, QVBoxLayout,
                               QWidget)

try:
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
    HAVE_WEB = True
except ImportError:              # PySide6 without the WebEngine add-on
    HAVE_WEB = False

IS_WINDOWS = os.name == "nt"
HERE = os.path.dirname(os.path.abspath(__file__))
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # local server: never via a proxy


# ------------------------------------------------------------------ install / server helpers

def _registry(name):
    if not IS_WINDOWS:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\MagicalPortalSystem") as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def find_portal_dir():
    """Folder holding Magical Portal's server.py: our parent folder (suite layout), then the registry."""
    candidates = [os.environ.get("MAGICAL_PORTAL_DIR"), os.path.dirname(HERE), _registry("InstallDir")]
    for d in candidates:
        if d and os.path.isfile(os.path.join(d, "server.py")):
            return os.path.abspath(d)
    return None


def read_port(portal_dir):
    try:
        with open(os.path.join(portal_dir, "streams.json"), encoding="utf-8-sig") as f:
            return int(json.load(f).get("settings", {}).get("web_port") or 8080)
    except (OSError, ValueError, TypeError, AttributeError):
        return 8080


def fetch_state(port, timeout=1.5):
    """-> state dict if the portal server answers on this port, else None."""
    try:
        with _NO_PROXY.open(f"http://127.0.0.1:{port}/api/state", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
            return data if isinstance(data, dict) and "streams" in data else None
    except Exception:
        return None


def post(port, path, timeout=4):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=b"{}", method="POST",
                                 headers={"Content-Type": "application/json"})
    with _NO_PROXY.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def pythonw_for_server():
    """Python to run server.py with (standard library only): the one Setup recorded, else ours."""
    cands = [_registry("Pythonw")]
    exe = sys.executable or ""
    if exe.lower().endswith("python.exe"):
        cands.append(exe[:-10] + "pythonw.exe")
    cands.append(exe)
    cands += [shutil.which("pythonw"), shutil.which("python"), shutil.which("python3")]
    for c in cands:
        if c and os.path.isfile(c) and "python" in os.path.basename(c).lower():
            return c
    return None


def start_server(portal_dir):
    py = pythonw_for_server()
    if not py:
        raise RuntimeError("Python was not found to start the portal server.")
    flags = 0
    if IS_WINDOWS:
        flags = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: outlives this window
    subprocess.Popen([py, os.path.join(portal_dir, "server.py"), "--hidden", "--no-browser"],
                     cwd=portal_dir, creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def summary(state):
    streams = state.get("streams") or []
    running = sum(1 for s in streams if (s.get("runtime") or {}).get("status") in ("running", "restarting"))
    lost = sum(1 for s in streams if (s.get("runtime") or {}).get("signal") == "lost")
    text = f"{running} of {len(streams)} portal{'s' if len(streams) != 1 else ''} running"
    return text + (f"  ·  {lost} with no signal!" if lost else ""), lost > 0


# ------------------------------------------------------------------ web view pieces

if HAVE_WEB:
    class _ExternalPage(QWebEnginePage):
        """Target of window.open / target=_blank: hand the link to the normal browser."""

        def acceptNavigationRequest(self, url, _type, _main):
            QDesktopServices.openUrl(url)
            self.deleteLater()
            return False

    class PortalPage(QWebEnginePage):
        def createWindow(self, _type):
            self._ext = _ExternalPage(self.profile(), self)
            return self._ext

        def javaScriptConsoleMessage(self, *_a):
            pass


class _Poll(QObject):
    done = Signal(object, int)


# ------------------------------------------------------------------ the tab

class PortalTab(QWidget):
    running = False                    # MainWindow never asks to confirm closing this one

    def __init__(self):
        super().__init__()
        self.portal_dir = find_portal_dir()
        self.port = read_port(self.portal_dir) if self.portal_dir else 8080
        self.state = None
        self.loaded_port = None
        self.starting = 0              # poll ticks left while waiting for the server to start
        self._start_failed = False
        self.polling = False
        self.poll_sig = _Poll()
        self.poll_sig.done.connect(self._poll_done)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        bar = QWidget()
        bar.setObjectName("portalbar")
        bar.setStyleSheet("#portalbar { border-bottom: 1px solid palette(mid); }")
        h = QHBoxLayout(bar)
        h.setContentsMargins(8, 4, 8, 4)
        self.dot = QLabel("●")
        self.status = QLabel("Checking…")
        h.addWidget(self.dot)
        h.addWidget(self.status, 1)
        self.start_btn = QPushButton("Start portal server")
        self.start_btn.clicked.connect(self.start)
        self.stop_btn = QPushButton("Shut down…")
        self.stop_btn.setToolTip("Stop all portals and the portal server")
        self.stop_btn.clicked.connect(self.shutdown_server)
        self.reload_btn = QPushButton("Reload")
        self.reload_btn.clicked.connect(self.reload)
        self.browser_btn = QPushButton("Open in browser")
        self.browser_btn.clicked.connect(self.open_browser)
        for b in (self.start_btn, self.stop_btn, self.reload_btn, self.browser_btn):
            h.addWidget(b)
        v.addWidget(bar)

        self.stack = QStackedWidget()
        self.info = QLabel()
        self.info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.TextFormat.RichText)
        self.info.setOpenExternalLinks(True)
        self.info.setStyleSheet("font-size: 11pt; padding: 30px;")
        info_box = QWidget()
        iv = QVBoxLayout(info_box)
        iv.addStretch(1)
        iv.addWidget(self.info)
        self.big_start = QPushButton("Start portal server")
        self.big_start.setMinimumHeight(40)
        self.big_start.setMaximumWidth(260)
        self.big_start.clicked.connect(self.start)
        self.log_btn = QPushButton("Open log folder")
        self.log_btn.setMaximumWidth(260)
        self.log_btn.clicked.connect(self.open_logs)
        for b in (self.big_start, self.log_btn):
            iv.addWidget(b, 0, Qt.AlignmentFlag.AlignHCenter)
        iv.addStretch(2)
        self.stack.addWidget(info_box)                       # 0 = message
        if HAVE_WEB:
            self.profile = QWebEngineProfile("PortalSystem", self)     # keeps the page's saved choices
            self.view = QWebEngineView()
            self.view.setPage(PortalPage(self.profile, self.view))
            self.stack.addWidget(self.view)                  # 1 = control page
        else:
            self.view = None
        v.addWidget(self.stack, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(2000)
        if not self.portal_dir:
            self._show_message(
                "<h2>Magical Portal System is not installed</h2>"
                "<p>Install the Portal System package (Install.bat) to control your UDP portals here.<br>"
                "All NetworkPocketknife tools work without it.</p>", start=False)
            self.timer.stop()
        else:
            self.poll()

    # ------------------------------------------------------------ status polling
    def poll(self):
        if self.polling or not self.portal_dir:
            return
        self.polling = True
        port = read_port(self.portal_dir)
        threading.Thread(target=lambda: self.poll_sig.done.emit(fetch_state(port), port), daemon=True).start()

    def _poll_done(self, state, port):
        self.polling = False
        if state:
            self.port = (state.get("web") or {}).get("port") or port
        self.state = state
        self.starting = max(0, self.starting - 1) if not state else 0
        self._refresh()

    def _refresh(self):
        st = self.state
        if st:
            text, warn = summary(st)
            self.dot.setStyleSheet("color: #d97706" if warn else "color: #16a34a")
            self.status.setText(f"Magical Portal System v{st.get('version', '?')}  ·  running  ·  {text}"
                                f"  ·  port {self.port}")
            self.start_btn.hide()
            self.stop_btn.show()
            self.reload_btn.setVisible(bool(self.view))
            self.browser_btn.show()
            if self.view:
                if self.loaded_port != self.port:
                    self.loaded_port = self.port
                    self.view.load(QUrl(f"http://127.0.0.1:{self.port}/"))
                self.stack.setCurrentIndex(1)
            else:
                self._show_message("<h2>The portal server is running</h2><p>This copy of Qt has no web view, so the "
                                   "control page opens in your browser.</p>", start=False)
            return
        self.loaded_port = None
        self.stop_btn.hide()
        self.reload_btn.hide()
        self.browser_btn.hide()
        if self.starting:
            self.dot.setStyleSheet("color: #2563eb")
            self.status.setText("Starting the portal server…")
            self.start_btn.hide()
            self._show_message("<h2>Starting Magical Portal System…</h2>", start=False)
        elif self._start_failed:
            self.dot.setStyleSheet("color: #dc2626")
            self.status.setText("The portal server did not start")
            self.start_btn.show()
            self._show_message("<h2>The portal server did not start</h2><p>Look in <b>logs\\server.log</b> in the "
                               "install folder, or run <b>Start Magical Portal System.bat</b> there to see "
                               "the messages.</p>", start=True, log=True)
        else:
            self.dot.setStyleSheet("color: #9ca3af")
            self.status.setText("Portal server is not running")
            self.start_btn.show()
            self._show_message("<h2>Magical Portal System is not running</h2>"
                               "<p>Start it to control your portals here. It keeps running in the background "
                               "(tray icon next to the clock) when you close this window.</p>", start=True)

    def _show_message(self, html, start=True, log=False):
        self.info.setText(html)
        self.big_start.setVisible(start)
        self.log_btn.setVisible(log)
        self.stack.setCurrentIndex(0)

    # ------------------------------------------------------------ actions
    def start(self):
        if not self.portal_dir:
            return
        try:
            start_server(self.portal_dir)
        except Exception as e:
            QMessageBox.warning(self, "Start portal server", str(e))
            return
        self._start_failed = False
        self.starting = 8                     # ~16 s
        self._refresh()
        QTimer.singleShot(16500, self._check_started)
        QTimer.singleShot(700, self.poll)

    def _check_started(self):
        if not self.state:
            self._start_failed = True
            self.starting = 0
            self._refresh()

    def shutdown_server(self):
        if QMessageBox.question(self, "Shut down", "Shut down Magical Portal System?\n\nAll portals stop. "
                                "You can start it again from here.") != QMessageBox.StandardButton.Yes:
            return
        try:
            post(self.port, "/api/shutdown")
        except Exception as e:
            QMessageBox.warning(self, "Shut down", f"The portal server did not answer: {e}")
        self._start_failed = False
        QTimer.singleShot(1500, self.poll)

    def reload(self):
        if self.view:
            self.view.load(QUrl(f"http://127.0.0.1:{self.port}/"))
            self.loaded_port = self.port

    def open_browser(self):
        url = ((self.state or {}).get("web") or {}).get("page_url") or f"http://localhost:{self.port}"
        QDesktopServices.openUrl(QUrl(url))

    def open_logs(self):
        if self.portal_dir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.join(self.portal_dir, "logs")))

    def ensure_started(self):
        """Called once at program start: start the server if it is installed but not running."""
        if self.portal_dir and fetch_state(read_port(self.portal_dir), timeout=0.8) is None:
            self.start()

    def shutdown(self):
        """Window/tab closing: the portal server keeps running on purpose."""
        self.timer.stop()
