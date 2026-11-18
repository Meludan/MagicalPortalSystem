"""REST API tab for NetworkPocketknife (Generic, Crestron VC-4, Crestron DM NVX + user profiles)."""
import datetime
import json
import os
import subprocess
import sys
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
                               QRadioButton, QScrollArea, QSplitter, QStackedWidget, QTabWidget, QToolButton,
                               QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from restcore import (AUTH_TYPES, BODY_TYPES, RestClient, example_profile_json, fill_placeholders,
                      find_placeholders, find_streams, load_profiles, parse_header_lines, pretty_body,
                      profiles_dir)
import restio
from uicommon import APP_NAME, APP_VERSION, appdata_dir, mono_font, port_spin, settings

METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


class _Sig(QObject):
    done = Signal(object, object)       # (Result, context)


def run_async(sig, fn, ctx=None):
    def work():
        res = fn()
        try:
            sig.done.emit(res, ctx)
        except RuntimeError:          # receiver (e.g. a closed dialog) is gone
            pass
    threading.Thread(target=work, daemon=True).start()


def human_size(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


# ============================================================ parameter dialog

class ParamDialog(QDialog):
    """Asks for the ${...} values of a command before it is sent."""

    def __init__(self, parent, title, params, help_text, fetch_streams):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(520)
        self.params = params
        self.fetch_streams = fetch_streams
        self.widgets = {}
        self.sig = _Sig()
        self.sig.done.connect(self._streams_loaded)
        v = QVBoxLayout(self)
        if help_text:
            lbl = QLabel(help_text)
            lbl.setWordWrap(True)
            lbl.setStyleSheet("color: gray")
            v.addWidget(lbl)
        form = QFormLayout()
        v.addLayout(form)
        s = settings()
        for p in params:
            name, kind = p["name"], p["kind"]
            last = s.value(f"rest/param/{name}", "") or ""
            if kind == "choice":
                w = QComboBox()
                w.setEditable(True)
                w.addItems(p["choices"])
                if last:
                    w.setCurrentText(last)
                form.addRow(name, w)
            elif kind == "file":
                w = QLineEdit(last)
                b = QToolButton()
                b.setText("…")
                b.clicked.connect(lambda _c=False, w=w, n=name: self._browse(w, n))
                h = QHBoxLayout()
                h.addWidget(w, 1)
                h.addWidget(b)
                form.addRow(name, h)
            elif kind == "nvxstream":
                w = QComboBox()
                w.setEditable(True)
                w.setMinimumWidth(420)
                w.lineEdit().setPlaceholderText("loading sources from the receiver…")
                self.stream_status = QLabel("Reading discovered streams…")
                self.stream_status.setStyleSheet("color: gray")
                rb = QToolButton()
                rb.setText("⟳")
                rb.setToolTip("Read the list again")
                rb.clicked.connect(lambda _c=False, w=w: self._load_streams(w))
                h = QHBoxLayout()
                h.addWidget(w, 1)
                h.addWidget(rb)
                form.addRow(name, h)
                form.addRow("", self.stream_status)
                self._load_streams(w)
            else:
                w = QLineEdit(last)
                form.addRow(name, w)
            self.widgets[name] = (kind, w)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Send")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _browse(self, w, name):
        fn, _ = QFileDialog.getOpenFileName(self, name, os.path.dirname(w.text()) if w.text() else "",
                                            "Crestron programs (*.lpz *.cpz *.clz *.zip);;All files (*)")
        if fn:
            w.setText(os.path.normpath(fn))

    def _load_streams(self, combo):
        self.stream_status.setText("Reading discovered streams…")
        run_async(self.sig, self.fetch_streams, combo)

    def _streams_loaded(self, res, combo):
        if not res.ok or res.status >= 400:
            self.stream_status.setText(f"Could not read the source list ({res.error or res.status}). "
                                       "You can paste a source UUID instead.")
            return
        try:
            streams = find_streams(json.loads(res.content))
        except ValueError:
            streams = []
        current = combo.currentText()
        combo.clear()
        for label, uid in sorted(streams):
            combo.addItem(label, uid)
        if current:
            combo.setCurrentText(current)
        self.stream_status.setText(f"{len(streams)} source(s) found on the network" if streams else
                                   "No sources found - is this device a receiver, and are transmitters streaming?")

    def values(self):
        out = {}
        s = settings()
        for name, (kind, w) in self.widgets.items():
            if kind == "nvxstream":
                idx = w.currentIndex()
                text = w.currentText().strip()
                val = w.itemData(idx) if idx >= 0 and w.itemText(idx) == text else text
                if val and "[" in val and val.endswith("]"):
                    val = val.rsplit("[", 1)[1][:-1]
            elif kind == "choice":
                val = w.currentText().strip()
            else:
                val = w.text().strip()
            out[name] = val
            if kind != "nvxstream":
                s.setValue(f"rest/param/{name}", val)
        return out


# ============================================================ profile / command editors

class ProfileDialog(QDialog):
    """New / edit / duplicate a device profile (saved as a .json file in the profiles folder)."""

    def __init__(self, parent, title, profile=None, copy_choices=(), taken=()):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(520)
        self.taken = {t.lower() for t in taken}
        p = profile or {}
        form = QFormLayout(self)
        self.name = QLineEdit(p.get("name", ""))
        self.name.setPlaceholderText("e.g. BrightSign player, Shelly relay, Q-SYS Core")
        self.scheme = QComboBox()
        self.scheme.addItems(["https", "http"])
        self.scheme.setCurrentText(p.get("scheme", "http"))
        self.port = port_spin(int(p.get("port", 0) or 0))
        self.port.setSpecialValueText("default (80 / 443)")
        self.base = QLineEdit(p.get("base_path", ""))
        self.base.setPlaceholderText("optional - common start of every path, e.g. /api/v1")
        self.auth = QComboBox()
        for k, label in AUTH_TYPES:
            self.auth.addItem(label, k)
        self.auth.setCurrentIndex(max(0, self.auth.findData(p.get("auth", "none"))))
        self.header = QLineEdit(p.get("header_name", "Authorization") or "Authorization")
        self.secret_label = QLineEdit(p.get("secret_label", ""))
        self.secret_label.setPlaceholderText("label for the password box, e.g. API key")
        self.notes = QPlainTextEdit(p.get("notes", ""))
        self.notes.setPlaceholderText("optional - shown under the device settings (where to find the token etc.)")
        self.notes.setMaximumHeight(70)
        self.example = QLabel()
        self.example.setStyleSheet("color: gray")
        form.addRow("Name", self.name)
        form.addRow("Protocol", self.scheme)
        form.addRow("Port", self.port)
        form.addRow("Base path", self.base)
        form.addRow("Login type", self.auth)
        form.addRow("Token header", self.header)
        form.addRow("Password label", self.secret_label)
        form.addRow("Notes", self.notes)
        form.addRow("", self.example)
        self.copy_from = None
        if copy_choices:
            self.copy_from = QComboBox()
            self.copy_from.addItem("— start with no commands —", "")
            for n in copy_choices:
                self.copy_from.addItem(f"copy commands from: {n}", n)
            form.addRow("Commands", self.copy_from)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        for w in (self.scheme, self.auth):
            w.currentIndexChanged.connect(self._update)
        self.port.valueChanged.connect(self._update)
        self.base.textChanged.connect(self._update)
        self._update()

    def _update(self, *_):
        self.header.setEnabled(self.auth.currentData() == "header")
        port = f":{self.port.value()}" if self.port.value() else ""
        base = self.base.text().strip()
        if base and not base.startswith("/"):
            base = "/" + base
        self.example.setText(f"Requests go to  {self.scheme.currentText()}://DEVICE-IP{port}{base.rstrip('/')}/…")

    def _ok(self):
        n = self.name.text().strip()
        if not n:
            QMessageBox.information(self, self.windowTitle(), "Give the profile a name.")
            return
        if n.lower() in self.taken:
            QMessageBox.information(self, self.windowTitle(), f"There is already a profile called '{n}'.")
            return
        self.accept()

    def values(self):
        base = self.base.text().strip().rstrip("/")
        if base and not base.startswith("/"):
            base = "/" + base
        auth = self.auth.currentData()
        label = self.secret_label.text().strip() or {"bearer": "Token", "header": "Token"}.get(auth, "Password")
        return {"name": self.name.text().strip(), "scheme": self.scheme.currentText(), "port": self.port.value(),
                "base_path": base, "auth": auth, "header_name": self.header.text().strip() or "Authorization",
                "secret_label": label, "notes": self.notes.toPlainText().strip()}

    def copy_source(self):
        return self.copy_from.currentData() if self.copy_from else ""


class CommandDialog(QDialog):
    """Create or edit one saved command."""

    def __init__(self, parent, title, cmd, groups, from_request=None, where=""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(640, 560)
        self.from_request = from_request
        mono = mono_font(10)
        v = QVBoxLayout(self)
        form = QFormLayout()
        v.addLayout(form)
        self.name = QLineEdit(cmd.get("name", ""))
        self.name.setPlaceholderText("e.g. Get status, Route lobby TX, Reboot")
        self.group = QComboBox()
        self.group.setEditable(True)
        self.group.addItems(groups or ["Commands"])
        self.group.setCurrentText(cmd.get("group", groups[0] if groups else "Commands"))
        top = QHBoxLayout()
        self.method = QComboBox()
        self.method.setEditable(True)
        self.method.addItems(METHODS)
        self.method.setCurrentText(cmd.get("method", "GET"))
        self.path = QLineEdit(cmd.get("path", ""))
        self.path.setFont(mono)
        self.path.setPlaceholderText("/status   or   /zones/${Zone}   or a full URL")
        top.addWidget(self.method)
        top.addWidget(self.path, 1)
        form.addRow("Name", self.name)
        form.addRow("Group", self.group)
        form.addRow("Request", top)
        self.body_type = QComboBox()
        for k, label in BODY_TYPES:
            self.body_type.addItem(label, k)
        self.body_type.setCurrentIndex(max(0, self.body_type.findData(cmd.get("body_type", "none"))))
        form.addRow("Body type", self.body_type)
        self.body = QPlainTextEdit(cmd.get("body", ""))
        self.body.setFont(mono)
        v.addWidget(QLabel("Body"))
        v.addWidget(self.body, 2)
        self.headers = QPlainTextEdit(cmd.get("headers", ""))
        self.headers.setFont(mono)
        self.headers.setPlaceholderText("Extra headers (optional), one per line:   Accept: application/json")
        self.headers.setMaximumHeight(70)
        v.addWidget(QLabel("Headers"))
        v.addWidget(self.headers)
        f2 = QFormLayout()
        self.confirm = QLineEdit(cmd.get("confirm", ""))
        self.confirm.setPlaceholderText("optional - e.g. Reboot the device?   (empty = sends straight away)")
        self.help = QLineEdit(cmd.get("help", ""))
        self.help.setPlaceholderText("optional note shown with the command")
        f2.addRow("Ask first", self.confirm)
        f2.addRow("Note", self.help)
        v.addLayout(f2)
        tip = QLabel("Values to fill in when sending:  <code>${Name}</code>  ·  a choice  <code>${Input|HDMI1,HDMI2}</code>"
                     "  ·  a file  <code>${Firmware:file}</code>  (Form multipart: <code>file=@${Firmware:file}</code>)"
                     + (f"<br>Saved in: <b>{where}</b>" if where else ""))
        tip.setWordWrap(True)
        tip.setStyleSheet("color: gray")
        v.addWidget(tip)
        bh = QHBoxLayout()
        if from_request:
            fill = QPushButton("Copy from request editor")
            fill.setToolTip("Take method, path, body and headers from the request on the right")
            fill.clicked.connect(self._fill)
            bh.addWidget(fill)
        fmt = QPushButton("Format JSON")
        fmt.clicked.connect(self._format)
        bh.addWidget(fmt)
        bh.addStretch(1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        bh.addWidget(bb)
        v.addLayout(bh)
        self.body_type.currentIndexChanged.connect(self._bt)
        self._bt()

    def _bt(self, *_):
        bt = self.body_type.currentData()
        self.body.setEnabled(bt != "none")
        self.body.setPlaceholderText({"json": '{\n  "power": "${State|on,off}"\n}',
                                      "multipart": "name=value\nfile=@${Firmware:file}",
                                      "form": "name=value", "raw": "raw text"}.get(bt, "(no body)"))

    def _fill(self):
        r = self.from_request()
        self.method.setCurrentText(r["method"])
        self.path.setText(r["path"])
        self.body_type.setCurrentIndex(max(0, self.body_type.findData(r["body_type"])))
        self.body.setPlainText(r.get("body", ""))
        self.headers.setPlainText(r.get("headers", ""))

    def _format(self):
        try:
            self.body.setPlainText(json.dumps(json.loads(self.body.toPlainText()), indent=2))
        except ValueError as e:
            QMessageBox.information(self, "Format JSON", f"Not valid JSON yet:\n{e}\n\n"
                                    "(Values like ${Name} must be inside quotes to format.)")

    def _ok(self):
        if not self.name.text().strip():
            QMessageBox.information(self, self.windowTitle(), "Give the command a name.")
            return
        if not self.path.text().strip():
            QMessageBox.information(self, self.windowTitle(), "Enter the path (for example /status).")
            return
        try:
            parse_header_lines(self.headers.toPlainText())
        except ValueError as e:
            QMessageBox.information(self, self.windowTitle(), f"Headers: {e}")
            return
        self.accept()

    def values(self):
        cmd = {"group": self.group.currentText().strip() or "Commands", "name": self.name.text().strip(),
               "method": self.method.currentText().strip().upper() or "GET", "path": self.path.text().strip(),
               "body_type": self.body_type.currentData(), "body": self.body.toPlainText(),
               "headers": self.headers.toPlainText().strip(), "confirm": self.confirm.text().strip(),
               "help": self.help.text().strip()}
        if cmd["body_type"] == "none":
            cmd["body"] = ""
        return restio.clean_preset(cmd)


# ============================================================ REST tab

class RestTab(QWidget):
    key = "rest"

    def __init__(self):
        super().__init__()
        self.running = False            # MainWindow asks this before closing a tab
        self.client = RestClient()
        self.sig = _Sig()
        self.sig.done.connect(self._on_result)
        self.busy = False
        self.history = []
        self.profiles = []
        self.profile = None
        self.current_preset = None
        self._loading = False

        root = QVBoxLayout(self)
        root.addWidget(self._build_device_box())
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._build_left())
        split.addWidget(self._build_right())
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        split.setSizes([300, 900])
        root.addWidget(split, 1)
        self._reload_profiles(select=settings().value("rest/profile", "") or "")

    # ------------------------------------------------------------ layout
    def _build_device_box(self):
        box = QGroupBox("Device")
        g = QGridLayout(box)
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(230)
        self.profile_combo.currentIndexChanged.connect(self._profile_changed)
        self.host = QLineEdit()
        self.host.setPlaceholderText("IP address or host name")
        self.port = port_spin(0)
        self.port.setSpecialValueText("default")
        self.https = QCheckBox("HTTPS")
        self.insecure = QCheckBox("Ignore certificate errors")
        self.insecure.setToolTip("Crestron devices and most AV gear use self-signed certificates - leave this ticked")
        self.auth = QComboBox()
        for _k, label in AUTH_TYPES:
            self.auth.addItem(label, _k)
        self.auth.currentIndexChanged.connect(self._auth_changed)
        self.user = QLineEdit()
        self.user.setPlaceholderText("username")
        self.secret_label = QLabel("Password")
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.header_name = QLineEdit()
        self.header_name.setPlaceholderText("header name")
        self.header_name.setMaximumWidth(150)
        self.remember = QCheckBox("Remember")
        self.remember.setToolTip("Save the password/token for this device profile in your Windows user "
                                 "settings (not encrypted). Leave off on shared PCs.")
        self.profile_menu_btn = QPushButton("Profile ▾")
        self.profile_menu_btn.setToolTip("New, edit, duplicate or delete device profiles")
        self.profile_menu_btn.clicked.connect(lambda: self._profile_menu())
        imp_b = QPushButton("Import…")
        imp_b.setToolTip("Import a Portal System REST file, a device profile or a Postman collection")
        imp_b.clicked.connect(self.import_file)
        exp_b = QPushButton("Export…")
        exp_b.setToolTip("Export devices and commands, or the current profile as a Postman collection")
        exp_b.clicked.connect(self.export_file)
        reload_b = QToolButton()
        reload_b.setText("⟳")
        reload_b.setToolTip("Reload profiles")
        reload_b.clicked.connect(lambda: self._reload_profiles(select=self.profile_combo.currentText()))
        self.notes = QLabel()
        self.notes.setWordWrap(True)
        self.notes.setStyleSheet("color: gray")

        g.addWidget(QLabel("Profile"), 0, 0)
        ph = QHBoxLayout()
        ph.addWidget(self.profile_combo, 1)
        ph.addWidget(self.profile_menu_btn)
        ph.addWidget(reload_b)
        g.addLayout(ph, 0, 1)
        g.addWidget(QLabel("Host"), 0, 2)
        g.addWidget(self.host, 0, 3)
        g.addWidget(QLabel("Port"), 0, 4)
        g.addWidget(self.port, 0, 5)
        hh = QHBoxLayout()
        hh.addWidget(self.https)
        hh.addWidget(self.insecure)
        hh.addStretch(1)
        hh.addWidget(imp_b)
        hh.addWidget(exp_b)

        g.addLayout(hh, 0, 6)
        g.addWidget(QLabel("Login"), 1, 0)
        g.addWidget(self.auth, 1, 1)
        g.addWidget(QLabel("User"), 1, 2)
        g.addWidget(self.user, 1, 3)
        sh = QHBoxLayout()
        sh.addWidget(self.secret_label)
        sh.addWidget(self.header_name)
        sh.addWidget(self.secret, 1)
        sh.addWidget(self.remember)
        g.addLayout(sh, 1, 4, 1, 3)
        g.addWidget(self.notes, 2, 0, 1, 7)
        g.setColumnStretch(3, 2)
        g.setColumnStretch(6, 3)
        for w in (self.host, self.user, self.secret, self.header_name):
            w.editingFinished.connect(self._save_device)
        for w in (self.https, self.insecure, self.remember):
            w.toggled.connect(self._save_device)
        self.port.valueChanged.connect(self._save_device)
        return box

    def _build_left(self):
        tabs = QTabWidget()
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 4, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemClicked.connect(self._preset_clicked)
        self.tree.itemDoubleClicked.connect(self._preset_double)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        v.addWidget(self.tree, 1)
        self.preset_help = QLabel("Click a command to load it, double-click to send it.")
        self.preset_help.setWordWrap(True)
        self.preset_help.setStyleSheet("color: gray")
        v.addWidget(self.preset_help)
        nb = QPushButton("New command…")
        nb.clicked.connect(lambda: self.new_command())
        v.addWidget(nb)
        tabs.addTab(w, "Commands")
        self.hist = QListWidget()
        self.hist.itemClicked.connect(self._history_clicked)
        hw = QWidget()
        hv = QVBoxLayout(hw)
        hv.setContentsMargins(0, 4, 0, 0)
        hv.addWidget(self.hist, 1)
        clr = QPushButton("Clear history")
        clr.clicked.connect(lambda: (self.hist.clear(), self.history.clear()))
        hv.addWidget(clr)
        tabs.addTab(hw, "History")
        self.left_tabs = tabs
        return tabs

    def _build_right(self):
        split = QSplitter(Qt.Orientation.Vertical)
        mono = mono_font(10)

        req = QGroupBox("Request")
        rv = QVBoxLayout(req)
        top = QHBoxLayout()
        self.method = QComboBox()
        self.method.addItems(METHODS)
        self.method.setEditable(True)
        self.method.setMinimumWidth(95)
        self.path = QLineEdit()
        self.path.setFont(mono)
        self.path.setPlaceholderText("/path  or  full URL")
        self.path.returnPressed.connect(self.send)
        self.send_btn = QPushButton("Send")
        self.send_btn.setMinimumWidth(90)
        self.send_btn.setDefault(True)
        self.send_btn.clicked.connect(self.send)
        curl = QPushButton("Copy as cURL")
        curl.clicked.connect(self._copy_curl)
        save_cmd = QPushButton("Save as command…")
        save_cmd.setToolTip("Save this request under 'My commands' for this profile (keeps ${...} values)")
        save_cmd.clicked.connect(lambda: self.save_command())
        self.base_label = QLabel()
        self.base_label.setStyleSheet("color: gray")
        top.addWidget(self.method)
        top.addWidget(self.path, 1)
        top.addWidget(self.send_btn)
        top.addWidget(curl)
        top.addWidget(save_cmd)
        rv.addLayout(top)
        rv.addWidget(self.base_label)

        rt = QTabWidget()
        bw = QWidget()
        bv = QVBoxLayout(bw)
        bv.setContentsMargins(0, 4, 0, 0)
        bh = QHBoxLayout()
        bh.addWidget(QLabel("Body:"))
        self.body_type = QComboBox()
        for k, label in BODY_TYPES:
            self.body_type.addItem(label, k)
        self.body_type.currentIndexChanged.connect(self._body_type_changed)
        bh.addWidget(self.body_type)
        fmt = QPushButton("Format JSON")
        fmt.clicked.connect(self._format_json)
        bh.addWidget(fmt)
        bh.addStretch(1)
        bv.addLayout(bh)
        self.body = QPlainTextEdit()
        self.body.setFont(mono)
        bv.addWidget(self.body, 1)
        rt.addTab(bw, "Body")
        self.req_headers = QPlainTextEdit()
        self.req_headers.setFont(mono)
        self.req_headers.setPlaceholderText("Extra headers, one per line:\nAccept: application/json\nX-My-Header: value")
        rt.addTab(self.req_headers, "Headers")
        rv.addWidget(rt, 1)
        split.addWidget(req)

        resp = QGroupBox("Response")
        pv = QVBoxLayout(resp)
        sh = QHBoxLayout()
        self.status = QLabel("—")
        f = self.status.font()
        f.setBold(True)
        f.setPointSize(f.pointSize() + 1)
        self.status.setFont(f)
        sh.addWidget(self.status, 1)
        save = QPushButton("Save response…")
        save.clicked.connect(self._save_response)
        copy = QPushButton("Copy")
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.resp_body.toPlainText()))
        sh.addWidget(copy)
        sh.addWidget(save)
        pv.addLayout(sh)
        self.resp_notes = QLabel()
        self.resp_notes.setWordWrap(True)
        self.resp_notes.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        pv.addWidget(self.resp_notes)
        self.resp_tabs = QTabWidget()
        self.resp_stack = QStackedWidget()
        self.resp_body = QPlainTextEdit()
        self.resp_body.setReadOnly(True)
        self.resp_body.setFont(mono)
        self.resp_image = QLabel()
        self.resp_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll = QScrollArea()
        scroll.setWidget(self.resp_image)
        scroll.setWidgetResizable(True)
        self.resp_stack.addWidget(self.resp_body)
        self.resp_stack.addWidget(scroll)
        self.resp_tabs.addTab(self.resp_stack, "Body")
        self.resp_headers = QPlainTextEdit()
        self.resp_headers.setReadOnly(True)
        self.resp_headers.setFont(mono)
        self.resp_tabs.addTab(self.resp_headers, "Headers")
        self.resp_sent = QPlainTextEdit()
        self.resp_sent.setReadOnly(True)
        self.resp_sent.setFont(mono)
        self.resp_tabs.addTab(self.resp_sent, "Sent request")
        pv.addWidget(self.resp_tabs, 1)
        split.addWidget(resp)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        self.last = None
        self.last_ctx = None
        return split

    # ------------------------------------------------------------ profiles
    def _reload_profiles(self, select=""):
        self.profiles, errors = load_profiles(appdata_dir())
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for p in self.profiles:
            self.profile_combo.addItem(p["name"])
        self.profile_combo.blockSignals(False)
        idx = max(0, self.profile_combo.findText(select)) if select else 0
        self.profile_combo.setCurrentIndex(idx)
        self._profile_changed(idx)
        if errors:
            QMessageBox.warning(self, "Profile problem", "Some profile files could not be loaded:\n\n" +
                                "\n".join(errors))

    def _pkey(self, name=None):
        name = name or (self.profile["name"] if self.profile else "")
        return "rest/dev/" + "".join(c if c.isalnum() else "_" for c in name)

    def _profile_changed(self, idx):
        if idx < 0 or idx >= len(self.profiles):
            return
        self.profile = p = self.profiles[idx]
        settings().setValue("rest/profile", p["name"])
        s = settings()
        k = self._pkey()
        self._loading = True
        self.host.setText(s.value(f"{k}/host", "") or "")
        self.port.setValue(int(s.value(f"{k}/port", p.get("port", 0)) or 0))
        self.https.setChecked(str(s.value(f"{k}/https", p.get("scheme", "http") == "https")).lower() == "true")
        self.insecure.setChecked(str(s.value(f"{k}/insecure", True)).lower() == "true")
        ai = self.auth.findData(s.value(f"{k}/auth", p.get("auth", "none")))
        self.auth.setCurrentIndex(max(0, ai))
        self.user.setText(s.value(f"{k}/user", "") or "")
        self.header_name.setText(s.value(f"{k}/header", p.get("header_name", "Authorization")) or "")
        remember = str(s.value(f"{k}/remember", False)).lower() == "true"
        self.remember.setChecked(remember)
        self.secret.setText((s.value(f"{k}/secret", "") or "") if remember else "")
        self._loading = False
        self._auth_changed()
        notes = p.get("notes", "")
        if p.get("file"):
            notes += f"   (profile file: {os.path.basename(p['file'])})"
        self.notes.setText(notes)
        self.notes.setVisible(bool(notes))

        self._fill_tree()
        self.client.close_session()
        self._update_base_label()

    def _fill_tree(self):
        p = self.profile
        self.tree.clear()
        groups = {}
        src = "file" if p.get("file") else "builtin"
        items = [dict(x, _src=src) for x in p.get("presets", [])] + \
                [dict(x, _src="mine") for x in self._my_commands(p["name"])]
        for preset in items:
            g = groups.get(preset["group"])
            if g is None:
                g = QTreeWidgetItem([preset["group"]])
                f = g.font(0)
                f.setBold(True)
                g.setFont(0, f)
                g.setFlags(g.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                self.tree.addTopLevelItem(g)
                groups[preset["group"]] = g
            it = QTreeWidgetItem([("★ " if preset["_src"] == "mine" else "") + preset["name"]])
            tip = f"{preset['method']} {preset['path']}" + (f"\n\n{preset['help']}" if preset.get("help") else "")
            tip += {"mine": "\n\n★ your command - right-click to edit", "file": "\n\nright-click to edit",
                    "builtin": "\n\nbuilt-in - right-click to copy and edit"}[preset["_src"]]
            it.setToolTip(0, tip)
            if preset.get("confirm"):
                it.setForeground(0, QColor("#c2410c"))
            it.setData(0, Qt.ItemDataRole.UserRole, preset)
            g.addChild(it)
        self.tree.expandAll()
        if not items:
            self.preset_help.setText("No commands yet - click <b>New command…</b> below.")
        else:
            self.preset_help.setText("Click a command to load it, double-click to send it. Orange = asks first, "
                                     "★ = yours. Right-click to edit.")

    # ------------------------------------------------------------ command storage
    # built-in profiles: your commands are kept in settings (★)
    # your own profiles: commands live in the profile's .json file (shared with the file)
    def _mykey(self, name):
        return "rest/mycmds/" + "".join(c if c.isalnum() else "_" for c in name)

    def _my_commands(self, name):
        try:
            v = json.loads(settings().value(self._mykey(name), "[]") or "[]")
            return [restio.clean_preset(x) for x in v if isinstance(x, dict)]
        except (TypeError, ValueError):
            return []

    def _set_my_commands(self, name, cmds):
        clean = [restio.clean_preset({k: v for k, v in c.items() if not k.startswith("_")}) for c in cmds]
        settings().setValue(self._mykey(name), json.dumps(clean))

    def _default_store(self):
        return "file" if self.profile.get("file") else "mine"

    def _get_cmds(self, store):
        if store == "file":
            return [restio.clean_preset(x) for x in self.profile.get("presets", [])]
        return self._my_commands(self.profile["name"])

    def _put_cmds(self, store, cmds):
        cmds = [restio.clean_preset({k: v for k, v in c.items() if not k.startswith("_")}) for c in cmds]
        if store == "file":
            path = self.profile["file"]
            try:
                data = restio.load_file(path)
            except (OSError, ValueError):
                data = restio.clean_profile(self.profile)
            data["presets"] = cmds
            try:
                restio.save_file(path, data)
            except OSError as e:
                QMessageBox.warning(self, "Save", f"Could not write {os.path.basename(path)}:\n{e}")
                return False
            self.profile["presets"] = cmds
        else:
            self._set_my_commands(self.profile["name"], cmds)
        self._fill_tree()
        return True

    def _groups(self):
        out = []
        for x in list(self.profile.get("presets", [])) + self._my_commands(self.profile["name"]):
            if x["group"] not in out:
                out.append(x["group"])
        return out

    def _request_now(self):
        return {"method": self.method.currentText().strip().upper() or "GET", "path": self.path.text().strip(),
                "body_type": self.body_type.currentData(), "body": self.body.toPlainText(),
                "headers": self.req_headers.toPlainText().strip()}

    def _where(self, store):
        return (f"profile file {os.path.basename(self.profile['file'])}" if store == "file"
                else f"your ★ commands for {self.profile['name']}")

    def _open_command_dialog(self, title, cmd, store, original=None):
        groups = self._groups() or ["Commands"]
        if store == "mine" and "My commands" not in groups:
            groups = ["My commands"] + groups
        dlg = CommandDialog(self, title, cmd, groups, self._request_now, self._where(store))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        new = dlg.values()
        cmds = self._get_cmds(store)
        if original is not None:
            cmds = [c for c in cmds if (c["group"], c["name"]) != (original["group"], original["name"])]
        elif any((c["group"], c["name"]) == (new["group"], new["name"]) for c in cmds) and QMessageBox.question(
                self, title, f"'{new['name']}' already exists in {new['group']}. Replace it?") \
                != QMessageBox.StandardButton.Yes:
            return None
        cmds, _a, _r = restio.merge_commands(cmds, [new])
        if self._put_cmds(store, cmds):
            self.current_preset = dict(new, _src=store)
            self.status.setText(f"Saved '{new['name']}' in {new['group']}")
            self.status.setStyleSheet("color: #1a7f37")
            return new
        return None

    def new_command(self):
        if not self.profile:
            return
        store = self._default_store()
        cmd = {"group": "My commands" if store == "mine" else (self._groups() or ["Commands"])[0], "method": "GET"}
        self._open_command_dialog("New command", cmd, store)

    def save_command(self):
        """Save the request editor as a command (opens the same form, already filled in)."""
        if not self.profile:
            return
        store = self._default_store()
        cur = self.current_preset if self.current_preset and self.current_preset.get("_src") == store else None
        cmd = dict(self._request_now())
        if cur:
            cmd.update({k: cur.get(k, "") for k in ("group", "name", "confirm", "help")})
        else:
            cmd["group"] = "My commands" if store == "mine" else (self._groups() or ["Commands"])[0]
        self._open_command_dialog("Save as command", cmd, store)

    def _tree_menu(self, pos):
        item = self.tree.itemAt(pos)
        preset = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        menu = QMenu(self)
        if preset and preset.get("_src") in ("mine", "file"):
            menu.addAction("Edit…", lambda: self._edit_cmd(preset))
            menu.addAction("Duplicate", lambda: self._dup_cmd(preset))
            menu.addAction("Rename…", lambda: self._rename_cmd(preset))
            menu.addAction("Move to group…", lambda: self._regroup_cmd(preset))
            menu.addSeparator()
            menu.addAction("Delete", lambda: self._delete_cmd(preset))
            menu.addSeparator()
        elif preset:
            menu.addAction("Copy and edit…", lambda: self._copy_edit(preset))
            menu.addAction("Copy to My commands", lambda: self._copy_to_mine(preset))
            menu.addSeparator()
        menu.addAction("New command…", lambda: self.new_command())
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _change_cmd(self, preset, change):
        store = preset["_src"]
        cmds = self._get_cmds(store)
        for i, c in enumerate(cmds):
            if (c["group"], c["name"]) == (preset["group"], preset["name"]):
                new = change(dict(c))
                if new is None:
                    del cmds[i]
                else:
                    cmds[i] = restio.clean_preset(new)
                break
        self._put_cmds(store, cmds)

    def _edit_cmd(self, preset):
        clean = {k: v for k, v in preset.items() if not k.startswith("_")}
        self._open_command_dialog("Edit command", clean, preset["_src"], original=clean)

    def _dup_cmd(self, preset):
        cmds = self._get_cmds(preset["_src"])
        names = {(c["group"], c["name"]) for c in cmds}
        n, i = f"{preset['name']} (copy)", 2
        while (preset["group"], n) in names:
            n, i = f"{preset['name']} (copy {i})", i + 1
        cmds.append(restio.clean_preset({k: v for k, v in dict(preset, name=n).items() if not k.startswith("_")}))
        self._put_cmds(preset["_src"], cmds)

    def _rename_cmd(self, preset):
        text, ok = QInputDialog.getText(self, "Rename", "New name:", text=preset["name"])
        if ok and text.strip():
            self._change_cmd(preset, lambda c: dict(c, name=text.strip()))

    def _regroup_cmd(self, preset):
        groups = self._groups()
        text, ok = QInputDialog.getItem(self, "Move to group", "Group (pick or type a new one):", groups,
                                        max(0, groups.index(preset["group"])) if preset["group"] in groups else 0, True)
        if ok and text.strip():
            self._change_cmd(preset, lambda c: dict(c, group=text.strip()))

    def _delete_cmd(self, preset):
        if QMessageBox.question(self, "Delete", f"Delete the command '{preset['name']}'?") == QMessageBox.StandardButton.Yes:
            self._change_cmd(preset, lambda c: None)

    def _copy_edit(self, preset):
        clean = {k: v for k, v in preset.items() if not k.startswith("_")}
        store = self._default_store()
        clean["name"] = clean["name"] + " (my version)"
        if store == "mine":
            clean["group"] = "My commands"
        self._open_command_dialog("Copy and edit", clean, store)

    def _copy_to_mine(self, preset):
        clean = {k: v for k, v in preset.items() if not k.startswith("_")}
        store = self._default_store()
        cmds, _a, _r = restio.merge_commands(self._get_cmds(store),
                                             [restio.clean_preset(dict(clean, group="My commands"))])
        self._put_cmds(store, cmds)

    # ------------------------------------------------------------ profiles: new / edit / duplicate / delete
    def _profile_menu(self):
        menu = QMenu(self)
        menu.addAction("New profile…", lambda: self.new_profile())
        custom = bool(self.profile and self.profile.get("file"))
        a = menu.addAction("Edit profile…", self.edit_profile)
        a.setEnabled(custom)
        if not custom:
            a.setText("Edit profile…  (built-in: use Duplicate)")
        menu.addAction("Duplicate profile…", self.duplicate_profile)
        d = menu.addAction("Delete profile…", self.delete_profile)
        d.setEnabled(custom)
        menu.addSeparator()
        menu.addAction("Export this profile…", lambda: self.export_profile())
        menu.addAction("Import…", lambda: self.import_file())
        menu.addSeparator()
        menu.addAction("Open profiles folder", self._open_profiles_folder)
        menu.addAction("Reload profiles", lambda: self._reload_profiles(select=self.profile_combo.currentText()))
        menu.exec(self.profile_menu_btn.mapToGlobal(self.profile_menu_btn.rect().bottomLeft()))

    def _taken(self, except_name=""):
        return [p["name"] for p in self.profiles if p["name"] != except_name]

    def _write_new_profile(self, data):
        path = os.path.join(profiles_dir(appdata_dir()), restio.safe_filename(data["name"]) + ".json")
        base, i = path[:-5], 2
        while os.path.exists(path):
            path = f"{base} ({i}).json"
            i += 1
        restio.save_file(path, restio.profile_for_disk(data))
        return path

    def new_profile(self, template=None, title="New profile"):
        dlg = ProfileDialog(self, title, template or {"scheme": "http", "auth": "none"},
                            copy_choices=[p["name"] for p in self.profiles], taken=self._taken())
        if template:
            dlg.copy_from.setCurrentIndex(max(0, dlg.copy_from.findData(template.get("_copy_from", ""))))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        data = dlg.values()
        src = dlg.copy_source()
        cmds = []
        if src:
            sp = next((p for p in self.profiles if p["name"] == src), None)
            if sp:
                cmds = [restio.clean_preset(x) for x in sp.get("presets", [])] + self._my_commands(src)
        data["presets"] = cmds
        try:
            self._write_new_profile(data)
        except OSError as e:
            QMessageBox.warning(self, title, f"Could not save the profile:\n{e}")
            return
        self._save_device()
        self._reload_profiles(select=data["name"])
        self.status.setText(f"Profile '{data['name']}' created - enter the device IP, then add commands "
                            "with New command…")
        self.status.setStyleSheet("color: #1a7f37")

    def export_profile(self):
        """Save the current profile (built-in or yours) as one profile file."""
        p = self.profile
        if not p:
            return
        mine = self._my_commands(p["name"])
        data = restio.clean_profile(p)
        data["presets"] = [restio.clean_preset(x) for x in p.get("presets", [])]
        if mine and QMessageBox.question(
                self, "Export this profile", f"Also include your {len(mine)} ★ command(s) for this profile?") \
                == QMessageBox.StandardButton.Yes:
            data["presets"], _a, _r = restio.merge_commands(data["presets"], mine)
        if not p.get("file"):
            data["builtin"] = True
        last_dir = settings().value("rest/io_dir", os.path.expanduser("~/Documents")) or ""
        fn, _ = QFileDialog.getSaveFileName(self, "Export this profile",
                                            os.path.join(last_dir, restio.safe_filename(p["name"]) + ".json"),
                                            "Profile file (*.json);;All files (*)")
        if not fn:
            return
        try:
            restio.save_file(fn, data)
        except OSError as e:
            QMessageBox.warning(self, "Export this profile", f"Could not save the file:\n{e}")
            return
        settings().setValue("rest/io_dir", os.path.dirname(fn))
        self.status.setText(f"Exported profile '{p['name']}' ({len(data['presets'])} commands) to {os.path.basename(fn)}")
        self.status.setStyleSheet("color: #1a7f37")

    def duplicate_profile(self):
        t = restio.clean_profile(self.profile)
        t["name"] = f"{self.profile['name']} (copy)"
        t["_copy_from"] = self.profile["name"]
        self.new_profile(t, "Duplicate profile")

    def edit_profile(self):
        p = self.profile
        if not p or not p.get("file"):
            return
        old = p["name"]
        dlg = ProfileDialog(self, "Edit profile", p, taken=self._taken(old))
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        vals = dlg.values()
        try:
            data = restio.load_file(p["file"])
        except (OSError, ValueError):
            data = restio.clean_profile(p)
        data.update(vals)
        try:
            restio.save_file(p["file"], data)
        except OSError as e:
            QMessageBox.warning(self, "Edit profile", f"Could not save the profile:\n{e}")
            return
        self._save_device()
        if vals["name"] != old:                    # keep this device's IP/login and ★ commands
            s = settings()
            ok, nk = self._pkey(old), self._pkey(vals["name"])
            for key in ("host", "port", "https", "insecure", "auth", "user", "header", "remember", "secret"):
                if s.value(f"{ok}/{key}") is not None:
                    s.setValue(f"{nk}/{key}", s.value(f"{ok}/{key}"))
                    s.remove(f"{ok}/{key}")
            mine = s.value(self._mykey(old))
            if mine:
                s.setValue(self._mykey(vals["name"]), mine)
                s.remove(self._mykey(old))
        # login type / protocol changed in the profile: use them for the device too
        k = self._pkey(vals["name"])
        settings().setValue(f"{k}/auth", vals["auth"])
        settings().setValue(f"{k}/https", vals["scheme"] == "https")
        settings().setValue(f"{k}/header", vals["header_name"])
        self._reload_profiles(select=vals["name"])

    def delete_profile(self):
        p = self.profile
        if not p or not p.get("file"):
            return
        if QMessageBox.question(self, "Delete profile",
                                f"Delete the profile '{p['name']}' and its {len(p.get('presets', []))} command(s)?\n\n"
                                "Tip: Export it first if you might want it back.") != QMessageBox.StandardButton.Yes:
            return
        try:
            os.remove(p["file"])
        except OSError as e:
            QMessageBox.warning(self, "Delete profile", f"Could not delete the file:\n{e}")
            return
        settings().remove(self._mykey(p["name"]))
        self._reload_profiles(select="")

    # ------------------------------------------------------------ import / export
    def _device(self, name):
        s = settings()
        k = self._pkey(name)
        d = {"host": s.value(f"{k}/host", "") or "", "port": int(s.value(f"{k}/port", 0) or 0),
             "https": str(s.value(f"{k}/https", "")).lower() == "true",
             "insecure": str(s.value(f"{k}/insecure", True)).lower() == "true",
             "auth": s.value(f"{k}/auth", "") or "", "user": s.value(f"{k}/user", "") or "",
             "header": s.value(f"{k}/header", "") or ""}
        if s.value(f"{k}/https") is None:
            prof = next((p for p in self.profiles if p["name"] == name), {})
            d["https"] = prof.get("scheme") == "https"
        if str(s.value(f"{k}/remember", False)).lower() == "true":
            d["secret"] = s.value(f"{k}/secret", "") or ""
        if self.profile and name == self.profile["name"] and self.secret.text():
            d["secret"] = self.secret.text()         # what is typed right now
        return d

    def _write_device(self, name, d):
        s = settings()
        k = self._pkey(name)
        for key in ("host", "port", "https", "insecure", "auth", "user", "header"):
            if key in d:
                s.setValue(f"{k}/{key}", d[key])
        if d.get("secret"):
            s.setValue(f"{k}/remember", True)
            s.setValue(f"{k}/secret", d["secret"])

    def export_file(self):
        self._save_device()
        dlg = QDialog(self)
        dlg.setWindowTitle("Export REST API")
        dlg.setMinimumWidth(560)
        v = QVBoxLayout(dlg)
        own = QRadioButton("Portal System file — device connections, your ★ commands and custom profiles\n"
                           "(backup, move to another PC, share with a colleague who uses Portal System)")
        pm = QRadioButton(f"Postman collection — the commands of the current profile only\n"
                          f"({self.profile['name']}; opens in Postman, Insomnia and similar tools)")
        own.setChecked(True)
        v.addWidget(own)
        v.addWidget(pm)
        v.addWidget(QLabel("Profiles to include:"))
        lst = QListWidget()
        for p in self.profiles:
            mine = len(self._my_commands(p["name"]))
            host = self._device(p["name"]).get("host")
            bits = [b for b in (host and f"device {host}", mine and f"{mine} ★ command(s)",
                                f"{'your' if p.get('file') else 'built-in'} profile, "
                                f"{len(p.get('presets', []))} commands") if b]
            it = QListWidgetItem(p["name"] + (f"   —   {', '.join(bits)}" if bits else "   —   nothing to export"))
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if bits else Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, p["name"])
            lst.addItem(it)
        v.addWidget(lst, 1)
        with_builtin = QCheckBox("Include full copies of the built-in profiles (Generic, Crestron VC-4, Crestron DM NVX)")
        with_builtin.setChecked(True)
        with_builtin.setToolTip("Untick to save only your connections and ★ commands for them "
                                "(the other PC has the built-in profiles already)")
        v.addWidget(with_builtin)
        secrets = QCheckBox("Include passwords and tokens")
        warn = QLabel("⚠ Anyone who gets the file can log in to those devices. Only for your own backup.")
        warn.setStyleSheet("color: #b45309")
        warn.setVisible(False)
        secrets.toggled.connect(warn.setVisible)
        v.addWidget(secrets)
        v.addWidget(warn)
        pm.toggled.connect(lambda on: (lst.setEnabled(not on), with_builtin.setEnabled(not on)))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Save).setText("Export…")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        stamp = datetime.date.today().isoformat()
        last_dir = settings().value("rest/io_dir", os.path.expanduser("~/Documents")) or ""
        if pm.isChecked():
            p = self.profile
            cmds = list(p.get("presets", [])) + self._my_commands(p["name"])
            data = restio.to_postman(p, cmds, self._device(p["name"]), include_secrets=secrets.isChecked())
            default = f"{restio.safe_filename(p['name'])}.postman_collection.json"
            filt = "Postman collection (*.postman_collection.json *.json)"
            summary = f"{len(cmds)} requests from '{p['name']}'"
        else:
            chosen = [lst.item(i).data(Qt.ItemDataRole.UserRole) for i in range(lst.count())
                      if lst.item(i).checkState() == Qt.CheckState.Checked]
            if not chosen:
                QMessageBox.information(self, "Export", "Tick at least one profile.")
                return
            profs = [p for p in self.profiles if p["name"] in chosen]
            data = restio.build_export(f"{APP_NAME} {APP_VERSION}",
                                       [p for p in profs if p.get("file")] +
                                       ([dict(p, builtin=True) for p in profs if not p.get("file")]
                                        if with_builtin.isChecked() else []),
                                       {p["name"]: self._device(p["name"]) for p in profs},
                                       {p["name"]: self._my_commands(p["name"]) for p in profs},
                                       include_secrets=secrets.isChecked())
            default = f"PortalSystem-REST-{stamp}.json"
            filt = "Portal System REST file (*.json)"
            summary = (f"{len(data['devices'])} device connection(s), "
                       f"{sum(len(x) for x in data['my_commands'].values())} ★ command(s), "
                       f"{len(data['profiles'])} custom profile(s)")
        fn, _ = QFileDialog.getSaveFileName(self, "Export", os.path.join(last_dir, default), filt + ";;All files (*)")
        if not fn:
            return
        try:
            restio.save_file(fn, data)
        except OSError as e:
            QMessageBox.warning(self, "Export", f"Could not save the file:\n{e}")
            return
        settings().setValue("rest/io_dir", os.path.dirname(fn))
        self.status.setText(f"Exported {summary} to {os.path.basename(fn)}")
        self.status.setStyleSheet("color: #1a7f37")

    def import_file(self):
        last_dir = settings().value("rest/io_dir", os.path.expanduser("~/Documents")) or ""
        fn, _ = QFileDialog.getOpenFileName(self, "Import REST API", last_dir,
                                            "REST files (*.json);;All files (*)")
        if not fn:
            return
        settings().setValue("rest/io_dir", os.path.dirname(fn))
        try:
            data = restio.parse_import(restio.load_file(fn))
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Import", f"Could not import {os.path.basename(fn)}:\n\n{e}")
            return
        builtin = {p["name"] for p in self.profiles if not p.get("file")}
        existing_files = {p["name"]: p["file"] for p in self.profiles if p.get("file")}
        local = {p["name"]: p for p in self.profiles}
        uptodate = []
        for prof in list(data["profiles"]):               # never overwrite a built-in profile
            if prof["name"] not in builtin:
                continue
            old = prof["name"]
            if prof.get("builtin"):
                if restio.same_commands(prof, local[old]):
                    uptodate.append(old)                  # identical: nothing to do
                    data["profiles"].remove(prof)
                    continue
                prof["name"] = f"{old} (imported)"        # different version: optional separate copy
                prof["_optional"] = True                  # its device connection stays with the built-in one
            else:                                         # e.g. a Postman collection of a built-in profile
                prof["name"] = f"{old} (imported)"
                if old in data["devices"]:
                    data["devices"][prof["name"]] = data["devices"].pop(old)

        dlg = QDialog(self)
        dlg.setWindowTitle(f"Import {os.path.basename(fn)}")
        dlg.setMinimumWidth(560)
        v = QVBoxLayout(dlg)
        kind = {"portal": "Portal System REST file", "profile": "Device profile",
                "postman": "Postman collection"}.get(data["kind"], data["kind"])
        v.addWidget(QLabel(f"<b>{kind}</b>"))
        boxes = []

        def add(text, key, detail=""):
            cb = QCheckBox(text)
            cb.setChecked(True)
            v.addWidget(cb)
            if detail:
                d = QLabel(detail)
                d.setStyleSheet("color: gray; margin-left: 22px")
                d.setWordWrap(True)
                v.addWidget(d)
            boxes.append((cb, key))
        if uptodate:
            same = QLabel("Already built in here, same commands - nothing to import: " + ", ".join(uptodate))
            same.setWordWrap(True)
            same.setStyleSheet("color: gray")
            v.addWidget(same)
        for prof in data["profiles"]:
            n = len(prof.get("presets", []))
            if prof.get("_optional"):
                add(f"Copy of built-in profile as '{prof['name']}' — {n} commands", ("profile", prof["name"]),
                    "The file has a different version of this built-in profile (changed, or from another "
                    "release). Tick to keep it as a separate profile next to the built-in one.")
                boxes[-1][0].setChecked(False)
            elif prof["name"] in existing_files:
                add(f"Profile '{prof['name']}' — {n} commands", ("profile", prof["name"]),
                    "Replaces your existing profile with the same name.")
            else:
                add(f"New profile '{prof['name']}' — {n} commands", ("profile", prof["name"]))
        for name, d in data["devices"].items():
            if not d.get("host"):
                continue
            cur = self._device(name).get("host")
            extra = (f"Replaces {cur}." if cur and cur != d["host"] else "") + \
                    (" Includes the password/token (saved, 'Remember' on)." if d.get("secret") else "")
            add(f"Device connection for '{name}': {d['host']}"
                f"{':' + str(d['port']) if d.get('port') else ''}  ({d.get('auth') or 'login unchanged'})",
                ("device", name), extra.strip())
        for name, cmds in data["my_commands"].items():
            if not cmds:
                continue
            have = {(c["group"], c["name"]) for c in self._my_commands(name)}
            same = sum(1 for c in cmds if (c["group"], c["name"]) in have)
            add(f"{len(cmds)} ★ command(s) for '{name}'", ("mine", name),
                f"{same} replace commands with the same name." if same else "")
        if data["warnings"]:
            w = QLabel("Notes:\n• " + "\n• ".join(data["warnings"]))
            w.setWordWrap(True)
            w.setStyleSheet("color: #b45309")
            v.addWidget(w)
        if not boxes:
            QMessageBox.information(self, "Import", "The file has nothing new to import." +
                                    (f"\n\nAlready built in here: {', '.join(uptodate)}" if uptodate else ""))
            return
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Import")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        chosen = {key for cb, key in boxes if cb.isChecked()}
        done, select = [], None
        try:
            for prof in data["profiles"]:
                if ("profile", prof["name"]) not in chosen:
                    continue
                path = existing_files.get(prof["name"]) or os.path.join(
                    profiles_dir(appdata_dir()), restio.safe_filename(prof["name"]) + ".json")
                base, i = path[:-5], 2
                while path not in existing_files.values() and os.path.exists(path):   # don't clobber other files
                    path = f"{base} ({i}).json"
                    i += 1
                restio.save_file(path, restio.profile_for_disk(prof))
                done.append(f"profile '{prof['name']}'")
                select = select or prof["name"]
            for name, d in data["devices"].items():
                if ("device", name) in chosen:
                    self._write_device(name, d)
                    done.append(f"device {d.get('host')}")
                    select = select or name
            for name, cmds in data["my_commands"].items():
                if ("mine", name) in chosen:
                    merged, a, r = restio.merge_commands(self._my_commands(name), cmds)
                    self._set_my_commands(name, merged)
                    done.append(f"{a + r} ★ command(s)")
                    select = select or name
        except OSError as e:
            QMessageBox.warning(self, "Import", f"Could not write the profile file:\n{e}")
        self._reload_profiles(select=select or self.profile_combo.currentText())
        if done:
            self.status.setText("Imported " + ", ".join(done))
            self.status.setStyleSheet("color: #1a7f37")

    def _save_device(self, *_):
        if self._loading or not self.profile:
            return
        s = settings()
        k = self._pkey()
        s.setValue(f"{k}/host", self.host.text().strip())
        s.setValue(f"{k}/port", self.port.value())
        s.setValue(f"{k}/https", self.https.isChecked())
        s.setValue(f"{k}/insecure", self.insecure.isChecked())
        s.setValue(f"{k}/auth", self.auth.currentData())
        s.setValue(f"{k}/user", self.user.text().strip())
        s.setValue(f"{k}/header", self.header_name.text().strip())
        s.setValue(f"{k}/remember", self.remember.isChecked())
        s.setValue(f"{k}/secret", self.secret.text() if self.remember.isChecked() else "")
        self._update_base_label()

    def _auth_changed(self, *_):
        a = self.auth.currentData()
        need_user = a in ("basic", "digest", "cresnext")
        need_secret = a != "none"
        self.user.setEnabled(need_user)
        self.secret.setEnabled(need_secret)
        self.remember.setEnabled(need_secret)
        self.header_name.setVisible(a == "header")
        label = {"bearer": "Token", "header": "Token"}.get(a, "Password")
        if self.profile and a == self.profile.get("auth"):
            label = self.profile.get("secret_label", label)
        self.secret_label.setText(label)
        self._save_device()

    def _base_url(self):
        host = self.host.text().strip().rstrip("/")
        for pre in ("https://", "http://"):
            if host.lower().startswith(pre):
                host = host[len(pre):]
        if not host:
            return ""
        scheme = "https" if self.https.isChecked() else "http"
        port = self.port.value()
        hostport = f"[{host}]" if ":" in host and not host.startswith("[") else host
        if port:
            hostport += f":{port}"
        return f"{scheme}://{hostport}{self.profile.get('base_path', '') if self.profile else ''}"

    def _update_base_label(self):
        b = self._base_url()
        self.base_label.setText(f"Paths are relative to  {b}" if b else "Enter the device's IP address above")

    def _open_profiles_folder(self):
        d = profiles_dir(appdata_dir())
        ex = os.path.join(d, "example-profile.json.txt")
        if not os.path.exists(ex):
            with open(ex, "w", encoding="utf-8") as f:
                f.write("Copy this file, rename it to something.json, edit it, then click ⟳ in NetworkPocketknife.\n"
                        "Placeholders:  ${Name}  ${Name|choice1,choice2}  ${Name:file}  ${Name:nvxstream}\n"
                        "Body types: none, json, multipart, form, raw.   Auth: none, basic, digest, bearer, "
                        "header, cresnext\n\n" + example_profile_json())
        if sys.platform == "win32":
            os.startfile(d)
        else:
            subprocess.Popen(["xdg-open", d])

    # ------------------------------------------------------------ commands
    def _preset_clicked(self, item, _col=0):
        preset = item.data(0, Qt.ItemDataRole.UserRole)
        if not preset:
            return
        self.method.setCurrentText(preset["method"])
        self.path.setText(preset["path"])
        bi = self.body_type.findData(preset.get("body_type", "none"))
        self.body_type.setCurrentIndex(max(0, bi))
        self.body.setPlainText(preset.get("body", ""))
        self.req_headers.setPlainText(preset.get("headers", ""))
        self.current_preset = preset
        help_t = preset.get("help") or ""
        self.preset_help.setText(f"<b>{preset['name']}</b><br>{help_t}" if help_t else f"<b>{preset['name']}</b>")

    def _preset_double(self, item, _col=0):
        if item.data(0, Qt.ItemDataRole.UserRole):
            self._preset_clicked(item)
            self.send()

    def _body_type_changed(self, *_):
        hints = {"json": '{\n  "key": "value"\n}', "multipart": "name=value\nFile=@C:\\path\\to\\file.lpz",
                 "form": "name=value\nother=value", "raw": "raw text", "none": "(no body)"}
        self.body.setPlaceholderText(hints.get(self.body_type.currentData(), ""))
        self.body.setEnabled(self.body_type.currentData() != "none")

    def _format_json(self):
        try:
            self.body.setPlainText(json.dumps(json.loads(self.body.toPlainText()), indent=2))
        except ValueError as e:
            QMessageBox.warning(self, "Format JSON", f"Not valid JSON yet:\n{e}\n\n"
                                "(Placeholders like ${Name} must be inside quotes to format.)")

    def _configure_client(self):
        base = self._base_url()
        if not base:
            raise ValueError("enter the device's IP address or host name first")
        self.client.configure(base, self.auth.currentData(), self.user.text().strip(), self.secret.text(),
                              self.header_name.text().strip() or "Authorization",
                              verify=not self.insecure.isChecked())

    def _fetch_streams(self):
        return self.client.request("GET", "/Device/DiscoveredStreams", log_curl=False)

    # ------------------------------------------------------------ send
    def send(self):
        if self.busy:
            return
        method = self.method.currentText().strip().upper() or "GET"
        path, body = self.path.text().strip(), self.body.toPlainText()
        btype = self.body_type.currentData()
        preset = self.current_preset
        if preset and (preset["path"] != path or preset.get("body", "") != body or preset["method"] != method):
            preset = None                         # user edited it
        try:
            self._configure_client()
            headers = parse_header_lines(self.req_headers.toPlainText())
        except Exception as e:
            self._show_error(str(e))
            return
        params = find_placeholders(path, body)
        if params:
            dlg = ParamDialog(self, preset["name"] if preset else "Fill in values", params,
                              preset.get("help", "") if preset else "", self._fetch_streams)
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            vals = dlg.values()
            missing = [k for k, v in vals.items() if not v and k.lower() != "notes"]
            if missing and QMessageBox.question(self, "Empty values", "These are empty:\n  " + "\n  ".join(missing) +
                                                "\n\nSend anyway?") != QMessageBox.StandardButton.Yes:
                return
            path = fill_placeholders(path, vals)
            body = fill_placeholders(body, vals, json_escape=(btype == "json"))
        else:
            vals = {}
        if preset and preset.get("confirm"):
            text = fill_placeholders(preset["confirm"], vals)
            if QMessageBox.question(self, preset["name"], text) != QMessageBox.StandardButton.Yes:
                return
        self.busy = True
        self.send_btn.setEnabled(False)
        self.send_btn.setText("Sending…")
        self.status.setText(f"Sending {method} …")
        self.status.setStyleSheet("")
        ctx = {"method": method, "path": path, "body_type": btype, "body": body,
               "headers": self.req_headers.toPlainText(), "name": preset["name"] if preset else ""}
        run_async(self.sig, lambda: self.client.request(method, path, headers, btype, body), ctx)

    def _on_result(self, res, ctx):
        self.busy = False
        self.send_btn.setEnabled(True)
        self.send_btn.setText("Send")
        self._show_result(res, ctx)
        when = datetime.datetime.now().strftime("%H:%M:%S")
        outcome = f"{res.status}" if res.ok else "ERROR"
        label = ctx["name"] or f"{ctx['method']} {ctx['path']}"
        item = QListWidgetItem(f"{when}  {outcome:>5}  {label}")
        item.setToolTip(f"{res.method} {res.url}\n{res.elapsed_ms} ms")
        item.setForeground(QColor(self._status_color(res)))
        item.setData(Qt.ItemDataRole.UserRole, len(self.history))
        self.history.append((res, ctx))
        self.hist.insertItem(0, item)
        if len(self.history) > 300:
            self.history = self.history[-300:]
            for i in range(self.hist.count() - 1, 299, -1):
                self.hist.takeItem(i)

    @staticmethod
    def _status_color(res):
        if not res.ok or res.status >= 400:
            return "#d1242f"
        if res.status >= 300:
            return "#b45309"
        if any("ERROR" in n or "not supported" in n for n in res.notes):
            return "#b45309"
        return "#1a7f37"

    def _show_error(self, msg):
        self.status.setText(f"✖  {msg}")
        self.status.setStyleSheet("color: #d1242f")

    def _show_result(self, res, ctx):
        self.last, self.last_ctx = res, ctx
        color = self._status_color(res)
        if res.ok:
            self.status.setText(f"{res.status} {res.reason}   ·   {res.elapsed_ms} ms   ·   {human_size(len(res.content))}"
                                f"   ·   {res.method} {res.url}")
        else:
            self.status.setText(f"✖  {res.error}")
        self.status.setStyleSheet(f"color: {color}")
        notes = list(res.notes)
        if res.ok and res.status == 401:
            notes.append("401 = not authorised: check the token / password and the Login type.")
        elif res.ok and res.status == 404:
            notes.append("404 = path not found on this device / firmware.")
        self.resp_notes.setText("\n".join("• " + n for n in notes))
        self.resp_notes.setVisible(bool(notes))
        ctype = next((v for k, v in res.headers.items() if k.lower() == "content-type"), "")
        kind, text = pretty_body(res.content, ctype) if res.ok else ("text", "")
        if kind == "image":
            pm = QPixmap()
            pm.loadFromData(res.content)
            self.resp_image.setPixmap(pm)
            self.resp_stack.setCurrentIndex(1)
        else:
            self.resp_body.setPlainText(text)
            self.resp_stack.setCurrentIndex(0)
        self.resp_headers.setPlainText("\n".join(f"{k}: {v}" for k, v in res.headers.items()))
        sent = [f"{res.method} {res.url}", ""]
        for k, v in res.req_headers.items():
            if k.lower() in ("authorization", "cookie") or k.lower() == self.header_name.text().strip().lower():
                v = "****"
            sent.append(f"{k}: {v}")
        if ctx.get("body") and ctx.get("body_type") != "none":
            sent += ["", ctx["body"]]
        if res.curl:
            sent += ["", "# cURL (secrets hidden):", res.curl]
        self.resp_sent.setPlainText("\n".join(sent))

    def _history_clicked(self, item):
        i = item.data(Qt.ItemDataRole.UserRole)
        if i is None or i >= len(self.history):
            return
        res, ctx = self.history[i]
        self.current_preset = None
        self.method.setCurrentText(ctx["method"])
        self.path.setText(ctx["path"])
        self.body_type.setCurrentIndex(max(0, self.body_type.findData(ctx["body_type"])))
        self.body.setPlainText(ctx["body"])
        self.req_headers.setPlainText(ctx["headers"])
        self._show_result(res, ctx)

    def _copy_curl(self):
        try:
            self._configure_client()
            headers = parse_header_lines(self.req_headers.toPlainText())
            url = self.client.full_url(self.path.text().strip())
            hdrs = dict(headers)
            if self.body_type.currentData() == "json":
                hdrs.setdefault("Content-Type", "application/json")
            text = self.client.to_curl(self.method.currentText().strip().upper() or "GET", url, hdrs,
                                       self.body_type.currentData(), self.body.toPlainText())
        except Exception as e:
            self._show_error(str(e))
            return
        QGuiApplication.clipboard().setText(text)
        self.status.setText("cURL command copied to the clipboard (password/token shown as ****)")
        self.status.setStyleSheet("")

    def _save_response(self):
        if not self.last or not self.last.ok:
            return
        ctype = next((v for k, v in self.last.headers.items() if k.lower() == "content-type"), "")
        ext = ".jpg" if "jpeg" in ctype else ".png" if "png" in ctype else ".json" if "json" in ctype else ".txt"
        fn, _ = QFileDialog.getSaveFileName(self, "Save response", f"response{ext}", "All files (*)")
        if fn:
            with open(fn, "wb") as f:
                if self.resp_stack.currentIndex() == 0 and ext in (".json", ".txt"):
                    f.write(self.resp_body.toPlainText().encode("utf-8"))
                else:
                    f.write(self.last.content)

    def shutdown(self):
        threading.Thread(target=self.client.logout, daemon=True).start()
