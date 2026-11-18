"""
NetworkPocketknife by Portal System - UDP / TCP client / TCP server / Serial / SSH / REST API tool for Windows.
Run:  python networkpocketknife.py      (needs: PySide6, pyserial, paramiko, pyte, requests)
"""
import datetime
import json
import math
import os
import sys
import threading
from collections import deque

from PySide6.QtCore import QCoreApplication, QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QColor, QFont, QFontMetricsF, QIcon, QGuiApplication,
                           QPainter, QPen, QTextCharFormat, QTextCursor)
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QMainWindow,
                               QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox,
                               QSplitter, QTabWidget, QToolButton, QVBoxLayout, QWidget, QAbstractItemView)

from netcore import (SerialTransport, SshTransport, TcpClientTransport, TcpServerTransport, UdpTransport,
                     fmt_ascii, fmt_hex, list_serial_ports, parse_ascii, parse_hex)

try:
    import pyte
except ImportError:
    pyte = None

from uicommon import (APP_COMPANY, APP_NAME, APP_VERSION, Bridge, appdata_dir, mono_font, now_ts, persist,
                      port_spin, settings)
from resttab import RestTab
from portaltab import PortalTab, find_portal_dir
from PySide6.QtNetwork import QLocalServer, QLocalSocket

SUITE_NAME = "Portal System"


# ============================================================ log view ====

class LogView(QWidget):
    COLORS = {"RX": "#2da44e", "TX": "#3b82f6", "info": "#8b949e", "error": "#e5534b"}

    def __init__(self, key):
        super().__init__()
        self.rx_bytes = self.tx_bytes = 0
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Display:"))
        self.mode = persist(self._combo(["ASCII", "HEX", "ASCII + HEX"]), f"{key}/log_mode")
        bar.addWidget(self.mode)
        self.ts = persist(QCheckBox("Timestamps"), f"{key}/log_ts")
        self.show_tx = persist(QCheckBox("Show sent"), f"{key}/log_tx")
        self.autoscroll = QCheckBox("Auto-scroll")
        self.autoscroll.setChecked(True)
        for cb, k in ((self.ts, "log_ts"), (self.show_tx, "log_tx")):
            if settings().value(f"{key}/{k}") is None:
                cb.setChecked(True)
        for cb in (self.ts, self.show_tx, self.autoscroll):
            bar.addWidget(cb)
        bar.addStretch(1)
        self.counters = QLabel()
        bar.addWidget(self.counters)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.clear)
        save = QPushButton("Save log…")
        save.clicked.connect(self.save)
        bar.addWidget(clear)
        bar.addWidget(save)
        v.addLayout(bar)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(mono_font(10))
        self.text.setMaximumBlockCount(50000)
        self.text.setUndoRedoEnabled(False)
        v.addWidget(self.text, 1)
        self.formats = {}
        for k, c in self.COLORS.items():
            f = QTextCharFormat()
            f.setForeground(QColor(c))
            self.formats[k] = f
        self._update_counters()

    @staticmethod
    def _combo(items):
        c = QComboBox()
        c.addItems(items)
        return c

    def _prefix(self):
        return f"[{now_ts()}] " if self.ts.isChecked() else ""

    def _append(self, text, kind):
        cur = QTextCursor(self.text.document())
        cur.movePosition(QTextCursor.MoveOperation.End)
        if not self.text.document().isEmpty():
            cur.insertBlock()
        cur.insertText(text, self.formats[kind])
        if self.autoscroll.isChecked():
            sb = self.text.verticalScrollBar()
            sb.setValue(sb.maximum())

    def add(self, direction, data, peer=""):
        if direction == "TX":
            self.tx_bytes += len(data)
            self._update_counters()
            if not self.show_tx.isChecked():
                return
        else:
            self.rx_bytes += len(data)
            self._update_counters()
        where = (" from " if direction == "RX" else " to ") + peer if peer else ""
        head = f"{self._prefix()}{direction}{where} [{len(data)}]: "
        mode = self.mode.currentIndex()
        if mode == 0:
            body = fmt_ascii(data)
        elif mode == 1:
            body = fmt_hex(data)
        else:
            body = fmt_ascii(data) + "\n" + " " * len(head) + fmt_hex(data)
        self._append(head + body, direction)

    def note(self, msg, kind="info"):
        self._append(f"{self._prefix()}*** {msg}", kind)

    def clear(self):
        self.text.clear()
        self.rx_bytes = self.tx_bytes = 0
        self._update_counters()

    def _update_counters(self):
        self.counters.setText(f"RX: {self.rx_bytes:,} B   TX: {self.tx_bytes:,} B")

    def save(self):
        fn, _ = QFileDialog.getSaveFileName(self, "Save log", f"networkpocketknife-log-{datetime.date.today()}.txt",
                                            "Text files (*.txt);;All files (*)")
        if fn:
            with open(fn, "w", encoding="utf-8") as f:
                f.write(self.text.toPlainText())


# ============================================================ send panel ==

class SendPanel(QGroupBox):
    ENDINGS = [("Nothing", b""), ("CR  \\r", b"\r"), ("LF  \\n", b"\n"), ("CR+LF  \\r\\n", b"\r\n")]
    ROWS = 5

    def __init__(self, key, send_cb, error_cb):
        super().__init__("Send")
        self.send_cb, self.error_cb = send_cb, error_cb
        self.last_row = None
        grid = QGridLayout(self)
        self.edits, self.hexes = [], []
        mono = mono_font(10)
        for i in range(self.ROWS):
            e = persist(QLineEdit(), f"{key}/send{i}")
            e.setFont(mono)
            h = persist(QCheckBox("HEX"), f"{key}/hex{i}")
            b = QPushButton("Send")
            b.setDefault(False)
            b.setAutoDefault(False)
            self.edits.append(e)
            self.hexes.append(h)
            h.toggled.connect(lambda _v, i=i: self._placeholder(i))
            b.clicked.connect(lambda _c=False, i=i: self.send_row(i))
            e.returnPressed.connect(lambda i=i: self.send_row(i))
            grid.addWidget(e, i, 0)
            grid.addWidget(h, i, 1)
            grid.addWidget(b, i, 2)
            self._placeholder(i)
        opts = QHBoxLayout()
        opts.addWidget(QLabel("Append to text rows:"))
        self.ending = QComboBox()
        self.ending.addItems([n for n, _ in self.ENDINGS])
        persist(self.ending, f"{key}/ending")
        opts.addWidget(self.ending)
        opts.addSpacing(20)
        self.repeat = QCheckBox("Repeat last sent row every")
        self.interval = QSpinBox()
        self.interval.setRange(1, 3_600_000)
        self.interval.setSuffix(" ms")
        persist(self.interval, f"{key}/interval")
        if settings().value(f"{key}/interval") is None:
            self.interval.setValue(1000)
        opts.addWidget(self.repeat)
        opts.addWidget(self.interval)
        opts.addStretch(1)
        grid.addLayout(opts, self.ROWS, 0, 1, 3)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.repeat.toggled.connect(self._repeat_toggled)
        self.interval.valueChanged.connect(lambda v: self.timer.setInterval(v))

    def _placeholder(self, i):
        self.edits[i].setPlaceholderText(
            "Hex bytes, e.g. 01 03 00 00 00 0A C5 CD" if self.hexes[i].isChecked()
            else "Text — escapes: \\r \\n \\t \\0 \\e \\xHH  or <HH> e.g. <0D><0A>")

    def build(self, i):
        txt = self.edits[i].text()
        if self.hexes[i].isChecked():
            return parse_hex(txt)
        return parse_ascii(txt) + self.ENDINGS[self.ending.currentIndex()][1]

    def send_row(self, i):
        try:
            data = self.build(i)
        except ValueError as e:
            self.error_cb(f"Row {i + 1}: {e}")
            self.repeat.setChecked(False)
            return
        if not data:
            return
        self.last_row = i
        if not self.send_cb(data):
            self.repeat.setChecked(False)

    def _repeat_toggled(self, on):
        if on:
            if self.last_row is None:
                self.last_row = 0
            self.timer.start(self.interval.value())
        else:
            self.timer.stop()

    def _tick(self):
        if self.last_row is not None:
            self.send_row(self.last_row)


# ============================================================ base tab ====

class BaseTab(QWidget):
    key = "base"
    start_text = ("Start", "Stop")

    def __init__(self):
        super().__init__()
        self.transport = None
        self.running = False
        self.gen = 0
        self.bridge = Bridge()
        self.bridge.data.connect(self.on_data)
        self.bridge.status.connect(lambda m: self.log.note(m))
        self.bridge.error.connect(lambda m: self.log.note(m, "error"))
        self.bridge.closed.connect(self._on_closed)
        self.bridge.clients.connect(self.on_clients)

        root = QVBoxLayout(self)
        self.cfg = QGroupBox("Settings")
        self.grid = QGridLayout(self.cfg)
        root.addWidget(self.cfg)
        self.log = LogView(self.key)
        self.send = SendPanel(self.key, self.do_send, lambda m: self.log.note(m, "error"))
        self.split = QSplitter(Qt.Orientation.Vertical)
        self.split.addWidget(self.build_center())
        self.split.addWidget(self.send)
        self.split.setStretchFactor(0, 3)
        self.split.setStretchFactor(1, 1)
        root.addWidget(self.split, 1)

        self.toggle_btn = QPushButton(self.start_text[0])
        self.toggle_btn.setMinimumWidth(110)
        self.toggle_btn.clicked.connect(self.toggle)
        self.cfg_widgets = []
        self.build_settings()

    # subclass hooks -------------------------------------------------------
    def build_center(self):
        return self.log

    def build_settings(self):
        pass

    def create_transport(self, cb):
        raise NotImplementedError

    def transport_send(self, data):
        return self.transport.send(data)

    def on_clients(self, clients):
        pass

    # ---------------------------------------------------------------------
    def callbacks(self):
        gen = self.gen
        b = self.bridge
        return dict(on_data=b.data.emit, on_status=b.status.emit, on_error=b.error.emit,
                    on_closed=lambda: b.closed.emit(gen), on_clients=b.clients.emit)

    def toggle(self):
        if self.running:
            self.stop()
        else:
            self.gen += 1
            try:
                self.transport = self.create_transport(self.callbacks())
            except Exception as e:
                self.transport = None
                self.log.note(f"Could not start: {e}", "error")
                return
            self.set_running(True)

    def stop(self):
        t, self.transport = self.transport, None
        self.set_running(False)
        if t:
            t.stop()
            self.log.note("Stopped")

    def _on_closed(self, gen):
        if gen == self.gen and self.running:
            self.transport = None
            self.set_running(False)

    def set_running(self, on):
        self.running = on
        self.toggle_btn.setText(self.start_text[1] if on else self.start_text[0])
        for w in self.cfg_widgets:
            w.setEnabled(not on)
        if not on:
            self.send.repeat.setChecked(False)

    def do_send(self, data):
        if not self.transport or not self.running:
            self.log.note(f"Not connected — press {self.start_text[0]} first", "error")
            return False
        try:
            peer = self.transport_send(data)
        except Exception as e:
            self.log.note(f"Send failed: {e}", "error")
            return False
        self.log.add("TX", data, peer)
        return True

    def on_data(self, data, peer):
        self.log.add("RX", data, peer)

    def shutdown(self):
        if self.transport:
            try:
                self.transport.stop()
            except Exception:
                pass
            self.transport = None

    def add_row(self, row, *pairs):
        """pairs of (label, widget); label None = widget only."""
        col = 0
        for label, w in pairs:
            if label:
                self.grid.addWidget(QLabel(label), row, col)
                col += 1
            self.grid.addWidget(w, row, col)
            col += 1
        return col


# ============================================================ UDP =========

class UdpTab(BaseTab):
    key = "udp"

    def build_settings(self):
        k = self.key
        self.local_ip = persist(QLineEdit("0.0.0.0"), f"{k}/local_ip")
        self.listen_port = persist(port_spin(5000), f"{k}/listen_port")
        self.remote_ip = persist(QLineEdit("127.0.0.1"), f"{k}/remote_ip")
        self.remote_port = persist(port_spin(5001), f"{k}/remote_port")
        self.sep = persist(QCheckBox("Send from a different local port:"), f"{k}/sep")
        self.send_port = persist(port_spin(0), f"{k}/send_port")
        self.send_port.setSpecialValueText("auto")
        self.reply = persist(QCheckBox("Reply to last sender"), f"{k}/reply")
        self.bcast = persist(QCheckBox("Allow broadcast"), f"{k}/bcast")
        self.sep.toggled.connect(self.send_port.setEnabled)
        self.send_port.setEnabled(self.sep.isChecked())

        self.add_row(0, ("Local IP", self.local_ip), ("Listen port", self.listen_port),
                     ("Remote IP", self.remote_ip), ("Remote port", self.remote_port))
        self.grid.addWidget(self.toggle_btn, 0, 8)
        h = QHBoxLayout()
        for w in (self.sep, self.send_port, self.reply, self.bcast):
            h.addWidget(w)
        h.addStretch(1)
        self.grid.addLayout(h, 1, 0, 1, 8)
        self.grid.setColumnStretch(1, 1)
        self.grid.setColumnStretch(5, 1)
        self.cfg_widgets = [self.local_ip, self.listen_port, self.sep, self.send_port, self.bcast]
        # remote IP/port and reply-mode stay editable while running
        self.remote_ip.textChanged.connect(self._update_remote)
        self.remote_port.valueChanged.connect(self._update_remote)
        self.reply.toggled.connect(self._update_remote)

    def set_running(self, on):
        super().set_running(on)
        self.send_port.setEnabled(not on and self.sep.isChecked())

    def _update_remote(self, *_):
        t = self.transport
        if t and self.running:
            t.remote = (self.remote_ip.text().strip(), self.remote_port.value())
            t.reply_to_sender = self.reply.isChecked()

    def create_transport(self, cb):
        t = UdpTransport(**cb)
        t.start(self.local_ip.text().strip(), self.listen_port.value(),
                self.remote_ip.text().strip(), self.remote_port.value(),
                separate_send=self.sep.isChecked(), send_port=self.send_port.value(),
                broadcast=self.bcast.isChecked(), reply_to_sender=self.reply.isChecked())
        return t


# ============================================================ TCP client ==

class TcpClientTab(BaseTab):
    key = "tcpc"
    start_text = ("Connect", "Disconnect")

    def build_settings(self):
        k = self.key
        self.host = persist(QLineEdit("127.0.0.1"), f"{k}/host")
        self.port = persist(port_spin(23), f"{k}/port")
        self.local_port = persist(port_spin(0), f"{k}/local_port")
        self.local_port.setSpecialValueText("auto")
        self.add_row(0, ("Host / IP", self.host), ("Port", self.port), ("Local port", self.local_port))
        self.grid.addWidget(self.toggle_btn, 0, 6)
        self.grid.setColumnStretch(1, 1)
        self.cfg_widgets = [self.host, self.port, self.local_port]

    def create_transport(self, cb):
        host = self.host.text().strip()
        if not host:
            raise ValueError("enter a host or IP address")
        t = TcpClientTransport(**cb)
        t.start(host, self.port.value(), self.local_port.value())
        return t


# ============================================================ TCP server ==

class TcpServerTab(BaseTab):
    key = "tcps"
    start_text = ("Start listening", "Stop listening")

    def build_center(self):
        w = QSplitter(Qt.Orientation.Horizontal)
        w.addWidget(self.log)
        box = QGroupBox("Connected clients")
        v = QVBoxLayout(box)
        self.client_list = QListWidget()
        self.client_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        v.addWidget(self.client_list, 1)
        h = QHBoxLayout()
        h.addWidget(QLabel("Send to:"))
        self.target = QComboBox()
        self.target.addItems(["All clients", "Selected clients"])
        h.addWidget(self.target, 1)
        v.addLayout(h)
        kick = QPushButton("Disconnect selected")
        kick.clicked.connect(self._kick)
        v.addWidget(kick)
        w.addWidget(box)
        w.setStretchFactor(0, 4)
        w.setStretchFactor(1, 1)
        return w

    def build_settings(self):
        k = self.key
        self.bind_ip = persist(QLineEdit("0.0.0.0"), f"{k}/bind_ip")
        self.port = persist(port_spin(5000), f"{k}/port")
        self.add_row(0, ("Listen on IP", self.bind_ip), ("Port", self.port))
        self.grid.addWidget(self.toggle_btn, 0, 4)
        self.grid.setColumnStretch(1, 1)
        hint = QLabel("0.0.0.0 = all network adapters")
        hint.setStyleSheet("color: gray")
        self.grid.addWidget(hint, 1, 1)
        self.cfg_widgets = [self.bind_ip, self.port]

    def create_transport(self, cb):
        t = TcpServerTransport(**cb)
        t.start(self.bind_ip.text().strip(), self.port.value())
        return t

    def on_clients(self, clients):
        selected = {i.text() for i in self.client_list.selectedItems()}
        self.client_list.clear()
        for c in clients:
            self.client_list.addItem(c)
            if c in selected:
                self.client_list.item(self.client_list.count() - 1).setSelected(True)
        self.client_list.parentWidget().setTitle(f"Connected clients ({len(clients)})")

    def transport_send(self, data):
        if self.target.currentIndex() == 1:
            peers = [i.text() for i in self.client_list.selectedItems()]
            if not peers:
                raise ValueError("no client selected in the list")
            return self.transport.send(data, peers)
        return self.transport.send(data)

    def _kick(self):
        if self.transport:
            for i in self.client_list.selectedItems():
                self.transport.disconnect(i.text())


# ============================================================ Serial ======

class SerialTab(BaseTab):
    key = "serial"
    start_text = ("Open", "Close")
    BAUDS = ["300", "1200", "2400", "4800", "9600", "14400", "19200", "38400", "57600",
             "115200", "230400", "460800", "921600"]
    PARITY = [("None", "N"), ("Even", "E"), ("Odd", "O"), ("Mark", "M"), ("Space", "S")]
    STOP = [("1", 1), ("1.5", 1.5), ("2", 2)]
    FLOW = [("None", "none"), ("RTS/CTS", "rtscts"), ("XON/XOFF", "xonxoff")]

    def build_settings(self):
        k = self.key
        self.port = QComboBox()
        self.port.setEditable(True)
        self.port.setMinimumWidth(220)
        refresh = QToolButton()
        refresh.setText("⟳")
        refresh.setToolTip("Refresh port list")
        refresh.clicked.connect(self.refresh_ports)
        self.baud = QComboBox()
        self.baud.setEditable(True)
        self.baud.addItems(self.BAUDS)
        self.baud.setCurrentText("115200")
        persist(self.baud, f"{k}/baud")
        self.bits = persist(self._combo(["5", "6", "7", "8"], "8"), f"{k}/bits")
        self.parity = persist(self._combo([n for n, _ in self.PARITY]), f"{k}/parity")
        self.stop_bits = persist(self._combo([n for n, _ in self.STOP]), f"{k}/stop")
        self.flow = persist(self._combo([n for n, _ in self.FLOW]), f"{k}/flow")
        self.dtr = persist(QCheckBox("DTR"), f"{k}/dtr")
        self.rts = persist(QCheckBox("RTS"), f"{k}/rts")
        if settings().value(f"{k}/dtr") is None:
            self.dtr.setChecked(True)
        if settings().value(f"{k}/rts") is None:
            self.rts.setChecked(True)
        self.dtr.toggled.connect(lambda v: self._line("dtr", v))
        self.rts.toggled.connect(lambda v: self._line("rts", v))

        h = QHBoxLayout()
        h.addWidget(self.port, 1)
        h.addWidget(refresh)
        self.grid.addWidget(QLabel("Port"), 0, 0)
        self.grid.addLayout(h, 0, 1)
        self.grid.addWidget(QLabel("Baud"), 0, 2)
        self.grid.addWidget(self.baud, 0, 3)
        self.grid.addWidget(self.toggle_btn, 0, 6)
        h2 = QHBoxLayout()
        for label, w in (("Data bits", self.bits), ("Parity", self.parity), ("Stop bits", self.stop_bits),
                         ("Flow control", self.flow)):
            h2.addWidget(QLabel(label))
            h2.addWidget(w)
            h2.addSpacing(10)
        h2.addWidget(self.dtr)
        h2.addWidget(self.rts)
        h2.addStretch(1)
        self.grid.addLayout(h2, 1, 0, 1, 7)
        self.grid.setColumnStretch(1, 1)
        self.cfg_widgets = [self.port, refresh, self.baud, self.bits, self.parity, self.stop_bits, self.flow]
        self._saved_port = settings().value(f"{k}/port")
        self.port.currentTextChanged.connect(lambda t: settings().setValue(f"{k}/port", t))
        self.refresh_ports()

    @staticmethod
    def _combo(items, default=None):
        c = QComboBox()
        c.addItems(items)
        if default:
            c.setCurrentText(default)
        return c

    def refresh_ports(self):
        current = self.port.currentText() or self._saved_port or ""
        self.port.blockSignals(True)
        self.port.clear()
        ports = list_serial_ports()
        for dev, desc in ports:
            self.port.addItem(f"{dev}  —  {desc}" if desc and desc != dev else dev, dev)
        self.port.blockSignals(False)
        if current:
            dev = current.split()[0]
            idx = self.port.findData(dev)
            if idx >= 0:
                self.port.setCurrentIndex(idx)
            else:
                self.port.setCurrentText(current)
        try:
            import serial  # noqa: F401
        except ImportError:
            self.log.note("pyserial is not installed — run run.bat (or: pip install pyserial)", "error")
            return
        if not ports:
            self.log.note("No serial ports found. Plug in the adapter and click ⟳, or type the port (e.g. COM5).")

    def _selected_port(self):
        text = self.port.currentText().strip()
        idx = self.port.currentIndex()
        if idx >= 0 and self.port.itemText(idx) == text:
            return self.port.itemData(idx)
        return text.split()[0] if text else ""

    def _line(self, which, on):
        if self.transport and self.running:
            try:
                getattr(self.transport, f"set_{which}")(on)
            except Exception as e:
                self.log.note(f"Could not set {which.upper()}: {e}", "error")

    def create_transport(self, cb):
        port = self._selected_port()
        if not port:
            raise ValueError("choose a serial port")
        baud = int(self.baud.currentText())
        t = SerialTransport(**cb)
        t.start(port, baud, int(self.bits.currentText()),
                self.PARITY[self.parity.currentIndex()][1], self.STOP[self.stop_bits.currentIndex()][1],
                self.FLOW[self.flow.currentIndex()][1], self.dtr.isChecked(), self.rts.isChecked())
        return t


# ============================================================ terminal ====

if pyte is not None:
    from pyte import modes as pyte_modes

    class TermScreen(pyte.Screen):
        """pyte screen that keeps a scrollback buffer and can answer terminal queries."""

        def __init__(self, columns, lines, history=5000):
            self.history = deque(maxlen=history)
            self.pushed = 0
            self.reply = None
            super().__init__(columns, lines)

        def index(self):
            m = self.margins
            top, bottom = (m.top, m.bottom) if m else (0, self.lines - 1)
            if top == 0 and self.cursor.y == bottom:
                self.history.append(dict(self.buffer[0]))
                self.pushed += 1
            super().index()

        def write_process_input(self, data):
            if self.reply:
                self.reply(data)


def _k(name):
    v = getattr(Qt.Key, name)
    return v.value if hasattr(v, "value") else int(v)


class TerminalWidget(QWidget):
    send_bytes = Signal(object)
    size_changed = Signal(int, int)

    PAL = {
        "black": "#0c0c0c", "red": "#c50f1f", "green": "#13a10e", "brown": "#c19c00", "yellow": "#c19c00",
        "blue": "#0037da", "magenta": "#881798", "cyan": "#3a96dd", "white": "#cccccc",
        "brightblack": "#767676", "brightred": "#e74856", "brightgreen": "#16c60c", "brightbrown": "#f9f1a5",
        "brightyellow": "#f9f1a5", "brightblue": "#3b78ff", "brightmagenta": "#b4009e",
        "brightcyan": "#61d6d6", "brightwhite": "#f2f2f2",
    }
    BASE = {"black", "red", "green", "brown", "yellow", "blue", "magenta", "cyan", "white"}

    KEYS = None

    def __init__(self, font_size=11):
        super().__init__()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(300, 150)
        self.fg = QColor("#cccccc")
        self.bg = QColor("#0c0c0c")
        self.bold_fg = QColor("#f2f2f2")
        self._colors = {k: QColor(v) for k, v in self.PAL.items()}
        self._hex_cache = {}
        self.screen = TermScreen(80, 24)
        self.screen.reply = lambda s: self.send_bytes.emit(s.encode("utf-8"))
        self.stream = pyte.ByteStream(self.screen)
        self.scroll = 0
        self.sel = None
        self.selecting = False
        if TerminalWidget.KEYS is None:
            TerminalWidget._build_keys()
        self.set_font_size(font_size)

    @classmethod
    def _build_keys(cls):
        cls.KEYS = {
            _k("Key_Insert"): "\x1b[2~", _k("Key_Delete"): "\x1b[3~",
            _k("Key_PageUp"): "\x1b[5~", _k("Key_PageDown"): "\x1b[6~",
            _k("Key_F1"): "\x1bOP", _k("Key_F2"): "\x1bOQ", _k("Key_F3"): "\x1bOR", _k("Key_F4"): "\x1bOS",
            _k("Key_F5"): "\x1b[15~", _k("Key_F6"): "\x1b[17~", _k("Key_F7"): "\x1b[18~",
            _k("Key_F8"): "\x1b[19~", _k("Key_F9"): "\x1b[20~", _k("Key_F10"): "\x1b[21~",
            _k("Key_F11"): "\x1b[23~", _k("Key_F12"): "\x1b[24~",
            _k("Key_Backtab"): "\x1b[Z", _k("Key_Escape"): "\x1b",
            _k("Key_Return"): "\r", _k("Key_Enter"): "\r", _k("Key_Tab"): "\t",
        }
        cls.ARROWS = {_k("Key_Up"): "A", _k("Key_Down"): "B", _k("Key_Right"): "C", _k("Key_Left"): "D",
                      _k("Key_Home"): "H", _k("Key_End"): "F"}
        cls.KA, cls.KZ = _k("Key_A"), _k("Key_Z")
        cls.CTRL_SPECIAL = {_k("Key_Space"): "\x00", _k("Key_BracketLeft"): "\x1b",
                            _k("Key_Backslash"): "\x1c", _k("Key_BracketRight"): "\x1d",
                            _k("Key_2"): "\x00", _k("Key_6"): "\x1e", _k("Key_Minus"): "\x1f"}

    # -------------------------------------------------------------- setup
    def set_font_size(self, pt):
        self.tfont = mono_font(pt)
        self.bfont = QFont(self.tfont)
        self.bfont.setBold(True)
        fm = QFontMetricsF(self.tfont)
        self.cw = fm.horizontalAdvance("W")
        self.ch = math.ceil(fm.lineSpacing())
        self.asc = fm.ascent()
        self._recalc()
        self.update()

    def resizeEvent(self, ev):
        self._recalc()
        super().resizeEvent(ev)

    def _recalc(self):
        cols = max(20, int(self.width() // self.cw))
        rows = max(5, int(self.height() // self.ch))
        if (cols, rows) != (self.screen.columns, self.screen.lines):
            self.screen.resize(rows, cols)
            self.size_changed.emit(cols, rows)

    def reset(self):
        self.screen.reset()
        self.screen.history.clear()
        self.scroll = 0
        self.sel = None
        self.update()

    def feed(self, data: bytes):
        before = self.screen.pushed
        self.stream.feed(data)
        if self.scroll:
            self.scroll = min(self.scroll + self.screen.pushed - before, len(self.screen.history))
        self.update()

    def message(self, text, color="33"):
        self.feed(f"\r\n\x1b[{color}m*** {text}\x1b[0m\r\n".encode("utf-8"))

    # ------------------------------------------------------------ drawing
    def _color(self, name, is_fg, bold):
        if name == "default":
            if is_fg:
                return self.bold_fg if bold else self.fg
            return self.bg
        key = name.lower()
        if bold and is_fg and key in self.BASE:
            key = "bright" + key
        c = self._colors.get(key)
        if c is not None:
            return c
        c = self._hex_cache.get(name)
        if c is None:
            c = QColor("#" + name) if len(name) == 6 else (self.fg if is_fg else self.bg)
            if not c.isValid():
                c = self.fg if is_fg else self.bg
            self._hex_cache[name] = c
        return c

    def _line(self, absrow):
        h = len(self.screen.history)
        if absrow < h:
            return self.screen.history[absrow]
        return self.screen.buffer[absrow - h]

    def _top(self):
        return len(self.screen.history) - self.scroll

    def _sel_range(self):
        if not self.sel or self.sel[0] == self.sel[1]:
            return None
        a, b = sorted(self.sel)
        return a, b

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), self.bg)
        scr = self.screen
        dc = scr.default_char
        top = self._top()
        sel = self._sel_range()
        cw, ch, asc = self.cw, self.ch, self.asc
        for y in range(scr.lines):
            absr = top + y
            line = self._line(absr)
            run = []
            run_attr = None
            run_x = 0

            def flush():
                if not run:
                    return
                fg, bg, bold, under = run_attr
                text = "".join(run)
                rect = QRectF(run_x * cw, y * ch, len(run) * cw, ch)
                if bg != self.bg:
                    p.fillRect(rect, bg)
                if text.strip():
                    p.setFont(self.bfont if bold else self.tfont)
                    p.setPen(fg)
                    p.drawText(QPointF(run_x * cw, y * ch + asc), text)
                if under:
                    p.setPen(fg)
                    p.drawLine(QPointF(run_x * cw, y * ch + ch - 1), QPointF((run_x + len(run)) * cw, y * ch + ch - 1))

            for x in range(scr.columns):
                c = line.get(x, dc)
                if c.data == "":
                    continue  # second half of a wide character
                fg = self._color(c.fg, True, c.bold)
                bg = self._color(c.bg, False, False)
                if c.reverse:
                    fg, bg = bg, fg
                if sel and sel[0] <= (absr, x) <= sel[1]:
                    fg, bg = QColor("#000000"), QColor("#9ec5fe")
                attr = (fg, bg, c.bold, c.underscore)
                if attr != run_attr:
                    flush()
                    run = []
                    run_attr = attr
                    run_x = x
                run.append(c.data)
            flush()

        if self.scroll == 0 and not scr.cursor.hidden:
            r = QRectF(scr.cursor.x * cw, scr.cursor.y * ch, cw, ch)
            if self.hasFocus():
                p.fillRect(r, QColor(204, 204, 204, 170))
            else:
                p.setPen(QPen(QColor("#cccccc")))
                p.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))
        if self.scroll:
            p.setPen(QColor("#f9f1a5"))
            p.setFont(self.tfont)
            label = f" scrollback -{self.scroll} "
            w = QFontMetricsF(self.tfont).horizontalAdvance(label)
            r = QRectF(self.width() - w - 6, 2, w + 4, ch)
            p.fillRect(r, QColor(40, 40, 40, 220))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, label)
        p.end()

    # -------------------------------------------------------- clipboard
    def selected_text(self):
        rng = self._sel_range()
        if not rng:
            return ""
        (r0, c0), (r1, c1) = rng
        dc = self.screen.default_char
        out = []
        for r in range(r0, r1 + 1):
            line = self._line(r)
            a = c0 if r == r0 else 0
            b = c1 if r == r1 else self.screen.columns - 1
            out.append("".join(line.get(x, dc).data for x in range(a, b + 1)).rstrip())
        return "\n".join(out)

    def copy(self):
        t = self.selected_text()
        if t:
            QGuiApplication.clipboard().setText(t)

    def paste(self):
        t = QGuiApplication.clipboard().text()
        if t:
            t = t.replace("\r\n", "\r").replace("\n", "\r")
            self.send_bytes.emit(t.encode("utf-8"))

    # ------------------------------------------------------------ mouse
    def _cell_at(self, pos):
        col = min(max(int(pos.x() // self.cw), 0), self.screen.columns - 1)
        row = min(max(int(pos.y() // self.ch), 0), self.screen.lines - 1)
        return (self._top() + row, col)

    def mousePressEvent(self, ev):
        self.setFocus()
        if ev.button() == Qt.MouseButton.LeftButton:
            c = self._cell_at(ev.position())
            self.sel = [c, c]
            self.selecting = True
            self.update()
        elif ev.button() == Qt.MouseButton.RightButton:
            self.paste()                      # PuTTY style: right-click pastes

    def mouseMoveEvent(self, ev):
        if self.selecting:
            self.sel[1] = self._cell_at(ev.position())
            self.update()

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton and self.selecting:
            self.selecting = False
            if self._sel_range():
                self.copy()                   # PuTTY style: selecting copies
            else:
                self.sel = None
            self.update()

    def wheelEvent(self, ev):
        steps = ev.angleDelta().y() / 120
        self.scroll = int(min(max(self.scroll + steps * 3, 0), len(self.screen.history)))
        self.update()

    # ------------------------------------------------------------ keys
    def focusNextPrevChild(self, nxt):
        return False

    def event(self, ev):
        if ev.type() == QEvent.Type.KeyPress and hasattr(ev, "key") and \
                ev.key() in (_k("Key_Tab"), _k("Key_Backtab")):
            self.keyPressEvent(ev)
            return True
        if ev.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self.update()
        return super().event(ev)

    def keyPressEvent(self, ev):
        k = ev.key()
        m = ev.modifiers()
        ctrl = bool(m & Qt.KeyboardModifier.ControlModifier)
        shift = bool(m & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(m & Qt.KeyboardModifier.AltModifier)
        text = ev.text()

        if ctrl and shift and k == _k("Key_C"):
            self.copy()
            return
        if ctrl and shift and k == _k("Key_V"):
            self.paste()
            return
        if shift and k in (_k("Key_PageUp"), _k("Key_PageDown")):
            page = max(1, self.screen.lines - 1)
            d = page if k == _k("Key_PageUp") else -page
            self.scroll = min(max(self.scroll + d, 0), len(self.screen.history))
            self.update()
            return
        if shift and k == _k("Key_Insert"):
            self.paste()
            return

        seq = None
        if ctrl and alt and text and not text.isspace() and ord(text[0]) >= 32:
            seq = text                                     # AltGr characters
            alt = False
        elif k in self.ARROWS:
            letter = self.ARROWS[k]
            mod = 1 + (1 if shift else 0) + (2 if alt else 0) + (4 if ctrl else 0)
            if mod > 1:
                seq = f"\x1b[1;{mod}{letter}"
                alt = False
            elif pyte_modes.DECCKM in self.screen.mode:
                seq = "\x1bO" + letter
            else:
                seq = "\x1b[" + letter
        elif k == _k("Key_Backspace"):
            seq = "\x08" if ctrl else "\x7f"
        elif k in self.KEYS:
            seq = self.KEYS[k]
        elif ctrl and self.KA <= k <= self.KZ:
            seq = chr(k - self.KA + 1)
        elif ctrl and k in self.CTRL_SPECIAL:
            seq = self.CTRL_SPECIAL[k]
        elif text:
            seq = text
        if seq is None:
            super().keyPressEvent(ev)
            return
        if alt and len(seq) == 1:
            seq = "\x1b" + seq
        self.scroll = 0
        self.sel = None
        self.send_bytes.emit(seq.encode("utf-8"))
        self.update()


# ============================================================ SSH =========

class SshTab(QWidget):
    key = "ssh"

    def __init__(self):
        super().__init__()
        self.transport = None
        self.running = False
        self.gen = 0
        self.bridge = Bridge()
        root = QVBoxLayout(self)

        if pyte is None:
            root.addWidget(QLabel("The SSH tab needs the 'pyte' and 'paramiko' packages.\n"
                                  "Run run.bat again, or: pip install paramiko pyte"))
            self.term = None
            return

        box = QGroupBox("SSH session")
        g = QGridLayout(box)
        k = self.key
        self.sessions = QComboBox()
        self.sessions.setMinimumWidth(180)
        self.sessions.setPlaceholderText("Saved sessions…")
        self.sessions.activated.connect(self._load_session)
        save = QPushButton("Save session…")
        save.clicked.connect(self._save_session)
        delete = QPushButton("Delete")
        delete.clicked.connect(self._delete_session)
        self.host = persist(QLineEdit(), f"{k}/host")
        self.host.setPlaceholderText("hostname or IP")
        self.port = persist(port_spin(22), f"{k}/port")
        self.user = persist(QLineEdit(), f"{k}/user")
        self.user.setPlaceholderText("username")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("password / key passphrase")
        self.keyfile = persist(QLineEdit(), f"{k}/keyfile")
        self.keyfile.setPlaceholderText("private key file (optional)")
        browse = QToolButton()
        browse.setText("…")
        browse.clicked.connect(self._browse_key)
        self.font_size = QSpinBox()
        self.font_size.setRange(6, 32)
        persist(self.font_size, f"{k}/font")
        if settings().value(f"{k}/font") is None:
            self.font_size.setValue(11)
        self.font_size.setPrefix("Font ")
        self.btn = QPushButton("Connect")
        self.btn.setMinimumWidth(110)
        self.btn.clicked.connect(self.toggle)
        for w in (self.host, self.user, self.password):
            w.returnPressed.connect(self._enter)

        g.addWidget(QLabel("Host"), 0, 0)
        g.addWidget(self.host, 0, 1)
        g.addWidget(QLabel("Port"), 0, 2)
        g.addWidget(self.port, 0, 3)
        g.addWidget(QLabel("User"), 0, 4)
        g.addWidget(self.user, 0, 5)
        g.addWidget(QLabel("Password"), 0, 6)
        g.addWidget(self.password, 0, 7)
        g.addWidget(self.btn, 0, 8)
        kh = QHBoxLayout()
        kh.addWidget(self.keyfile, 1)
        kh.addWidget(browse)
        g.addWidget(QLabel("Key"), 1, 0)
        g.addLayout(kh, 1, 1, 1, 3)
        sh = QHBoxLayout()
        sh.addWidget(self.sessions, 1)
        sh.addWidget(save)
        sh.addWidget(delete)
        sh.addWidget(self.font_size)
        g.addLayout(sh, 1, 4, 1, 5)
        g.setColumnStretch(1, 2)
        g.setColumnStretch(5, 1)
        g.setColumnStretch(7, 1)
        root.addWidget(box)
        self.cfg_widgets = [self.host, self.port, self.user, self.password, self.keyfile, browse,
                            self.sessions, save, delete]

        self.term = TerminalWidget(self.font_size.value())
        root.addWidget(self.term, 1)
        tip = QLabel("Select text to copy · Right-click or Shift+Insert to paste · "
                     "Ctrl+Shift+C / Ctrl+Shift+V · Mouse wheel or Shift+PgUp/PgDn to scroll back")
        tip.setStyleSheet("color: gray")
        root.addWidget(tip)

        self.font_size.valueChanged.connect(self.term.set_font_size)
        self.term.send_bytes.connect(self._term_input)
        self.term.size_changed.connect(self._resize)
        self.bridge.data.connect(lambda d, _p: self.term.feed(d))
        self.bridge.status.connect(lambda m: self.term.message(m, "33"))
        self.bridge.error.connect(lambda m: self.term.message(m, "91"))
        self.bridge.closed.connect(self._on_closed)
        self.bridge.hostkey.connect(self._ask_hostkey_gui)
        self._refresh_sessions()
        self.term.message("Enter host, user and password (or key file), then press Connect.", "36")

    # -------------------------------------------------------- sessions
    def _all_sessions(self):
        try:
            return json.loads(settings().value("ssh/sessions", "{}") or "{}")
        except (TypeError, ValueError):
            return {}

    def _refresh_sessions(self):
        self.sessions.clear()
        for name in sorted(self._all_sessions(), key=str.lower):
            self.sessions.addItem(name)
        self.sessions.setCurrentIndex(-1)

    def _load_session(self, idx):
        s = self._all_sessions().get(self.sessions.itemText(idx))
        if s:
            self.host.setText(s.get("host", ""))
            self.port.setValue(int(s.get("port", 22)))
            self.user.setText(s.get("user", ""))
            self.keyfile.setText(s.get("keyfile", ""))
            self.password.clear()
            self.password.setFocus()

    def _save_session(self):
        name, ok = QInputDialog.getText(self, "Save session", "Session name:",
                                        text=f"{self.user.text()}@{self.host.text()}".strip("@"))
        if ok and name.strip():
            all_s = self._all_sessions()
            all_s[name.strip()] = {"host": self.host.text().strip(), "port": self.port.value(),
                                   "user": self.user.text().strip(), "keyfile": self.keyfile.text().strip()}
            settings().setValue("ssh/sessions", json.dumps(all_s))
            self._refresh_sessions()
            self.sessions.setCurrentText(name.strip())

    def _delete_session(self):
        name = self.sessions.currentText()
        all_s = self._all_sessions()
        if name in all_s and QMessageBox.question(self, "Delete session", f"Delete saved session '{name}'?") \
                == QMessageBox.StandardButton.Yes:
            del all_s[name]
            settings().setValue("ssh/sessions", json.dumps(all_s))
            self._refresh_sessions()

    def _browse_key(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Private key", os.path.expanduser("~/.ssh"),
                                            "All files (*)")
        if fn:
            self.keyfile.setText(fn)

    # -------------------------------------------------------- connect
    def _enter(self):
        if not self.running:
            self.toggle()

    def toggle(self):
        if self.running:
            self.stop()
            return
        host, user = self.host.text().strip(), self.user.text().strip()
        if not host or not user:
            self.term.message("Host and user are required.", "91")
            return
        if self.keyfile.text().strip() and not os.path.isfile(self.keyfile.text().strip()):
            self.term.message("Key file not found.", "91")
            return
        self.gen += 1
        gen = self.gen
        b = self.bridge
        self.term.reset()
        t = SshTransport(on_data=b.data.emit, on_status=b.status.emit, on_error=b.error.emit,
                         on_closed=lambda: b.closed.emit(gen))
        self.transport = t
        self.set_running(True)
        t.start(host, self.port.value(), user, self.password.text(), self.keyfile.text().strip(),
                self.term.screen.columns, self.term.screen.lines,
                os.path.join(appdata_dir(), "known_hosts"), self._ask_hostkey_worker)
        self.term.setFocus()

    def stop(self):
        t, self.transport = self.transport, None
        self.set_running(False)
        if t:
            t.stop()
            self.term.message("Disconnected")

    def _on_closed(self, gen):
        if gen == self.gen and self.running:
            self.transport = None
            self.set_running(False)

    def set_running(self, on):
        self.running = on
        self.btn.setText("Disconnect" if on else "Connect")
        for w in self.cfg_widgets:
            w.setEnabled(not on)

    def _term_input(self, data):
        t = self.transport
        if t and self.running:
            try:
                t.send(data)
            except Exception:
                pass

    def _resize(self, cols, rows):
        if self.transport:
            self.transport.resize(cols, rows)

    # host key confirmation: worker thread blocks while the GUI asks the user
    def _ask_hostkey_worker(self, host, ktype, fp):
        holder = {"ev": threading.Event(), "ok": False}
        self.bridge.hostkey.emit(host, ktype, fp, holder)
        holder["ev"].wait()
        return holder["ok"]

    def _ask_hostkey_gui(self, host, ktype, fp, holder):
        try:
            r = QMessageBox.question(
                self, "Unknown host key",
                f"The server's host key is not in your known hosts.\n\nHost: {host}\nKey type: {ktype}\n"
                f"Fingerprint: {fp}\n\nIf you trust this host, click Yes to save the key and connect.\n"
                f"Click No to cancel.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            holder["ok"] = r == QMessageBox.StandardButton.Yes
        finally:
            holder["ev"].set()

    def shutdown(self):
        if self.transport:
            try:
                self.transport.stop()
            except Exception:
                pass
            self.transport = None


# ============================================================ main window =

HELP_TEXT = """<h3>NetworkPocketknife quick help</h3>
<p><b>Sending text</b> — escapes you can type in a text row:<br>
<code>\\r</code> CR, <code>\\n</code> LF, <code>\\t</code> TAB, <code>\\0</code> NUL, <code>\\e</code> ESC,
<code>\\\\</code> backslash, <code>\\xHH</code> any byte, or <code>&lt;HH&gt;</code> (e.g. <code>&lt;0D&gt;&lt;0A&gt;</code>).<br>
"Append to text rows" adds CR / LF / CR+LF to every text row automatically.</p>
<p><b>Sending hex</b> — tick HEX on a row and type bytes: <code>01 03 00 00 00 0A</code>,
<code>01030000000A</code> or <code>0x01,0x03</code>. Press Enter in a row to send it.</p>
<p><b>Received data</b> — printable characters are shown as text, other bytes as <code>&lt;HH&gt;</code>.
Switch Display to HEX or ASCII + HEX for raw bytes.</p>
<p><b>UDP</b> — <i>Listen port</i> is where NetworkPocketknife receives. Tick <i>Send from a different local port</i>
to send from another source port (0 = auto); replies to that port are also received.
<i>Reply to last sender</i> sends back to whoever sent the last packet.</p>
<p><b>TCP Server</b> — choose "Selected clients" to send to only the clients highlighted in the list.</p>
<p><b>SSH</b> — select text to copy, right-click to paste (like PuTTY). Host keys are stored in
<code>%APPDATA%\\Portal System\\NetworkPocketknife\\known_hosts</code>. Passwords are never saved.</p>
<p><b>REST API</b> — pick a device profile (Generic, Crestron VC-4, Crestron DM NVX), enter the IP and login,
then click a command on the left (double-click sends it). Commands with <code>${...}</code> ask for values first.
<i>VC-4:</i> create a token in the VC-4 web page (Settings → Tokens). <i>NVX:</i> use the web-page user/password;
login and cookies are handled for you. <i>Profile ▾</i> → New / Edit / Duplicate / Delete profile adds any other REST device.
<i>New command…</i> (under the list) or <i>Save as command…</i> (from the request) adds commands; right-click a
command to edit, duplicate, rename, move or delete it. Built-in Crestron commands: right-click → Copy and edit.
<i>Export…</i> writes a Portal System file (devices, ★ commands, your profiles and the built-in ones) or a
Postman collection; <i>Profile ▾ → Export this profile…</i> saves any single profile;
<i>Import…</i> reads either, or a single profile file.</p>
<p><b>Magical Portal</b> — the first top tab is your Magical Portal System control page. Closing this window does <i>not</i> stop your portals; they keep running with the tray icon.</p>
<p><b>More tabs</b> — Use <i>File → New NetworkPocketknife tab</i> to run several connections of the same kind at once.</p>
"""


class MainWindow(QMainWindow):
    TYPES = [("UDP", UdpTab), ("TCP Client", TcpClientTab), ("TCP Server", TcpServerTab),
             ("Serial", SerialTab), ("SSH", SshTab), ("REST API", RestTab)]

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{SUITE_NAME} — Magical Portal System · {APP_NAME} {APP_VERSION}")
        icon = portal_icon_path()
        if icon:
            self.setWindowIcon(QIcon(icon))

        # top level: [Magical Portal]  [NetworkPocketknife]
        self.top = QTabWidget()
        self.top.setDocumentMode(True)
        f = self.top.font()
        f.setPointSize(f.pointSize() + 1)
        f.setBold(True)
        self.top.tabBar().setFont(f)
        self.portal = PortalTab()
        self.top.addTab(self.portal, "  Magical Portal  ")
        self.tabs = QTabWidget()            # NetworkPocketknife's own tabs
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.top.addTab(self.tabs, f"  {APP_NAME}  ")
        self.setCentralWidget(self.top)
        self.counts = {}

        fm = self.menuBar().addMenu("&File")
        new = QMenu("&New NetworkPocketknife tab", self)
        for name, cls in self.TYPES:
            a = QAction(name, self)
            a.triggered.connect(lambda _c=False, n=name, c=cls: self.add_tab(n, c, focus=True))
            new.addAction(a)
        fm.addMenu(new)
        fm.addSeparator()
        quit_a = QAction("E&xit (portals keep running)", self)
        quit_a.triggered.connect(self.close)
        fm.addAction(quit_a)
        vm = self.menuBar().addMenu("&View")
        for i, label in ((0, "Magical &Portal"), (1, "&NetworkPocketknife")):
            a = QAction(label, self)
            a.setShortcut(f"Ctrl+{i + 1}")
            a.triggered.connect(lambda _c=False, i=i: self.top.setCurrentIndex(i))
            vm.addAction(a)
        hm = self.menuBar().addMenu("&Help")
        h = QAction("NetworkPocketknife &help", self)
        h.setShortcut("F1")
        h.triggered.connect(lambda: QMessageBox.information(self, f"{APP_NAME} help", HELP_TEXT))
        hm.addAction(h)
        ab = QAction("&About", self)
        ab.triggered.connect(lambda: QMessageBox.about(
            self, f"About {SUITE_NAME}",
            f"<b>{SUITE_NAME}</b> by {APP_COMPANY}<br><br>"
            f"<b>Magical Portal System</b> — UDP stream control for BrightSign<br>"
            f"<b>{APP_NAME} {APP_VERSION}</b> — UDP, TCP client/server, serial, SSH and REST API tools<br><br>"
            "Built with Python, Qt (PySide6), pyserial, paramiko, pyte and requests."))
        hm.addAction(ab)

        for name, cls in self.TYPES:
            self.add_tab(name, cls)
        self.tabs.setCurrentIndex(int(settings().value("ui/tab", 0) or 0) % self.tabs.count())
        self.top.setCurrentIndex(int(settings().value("ui/top", 0) or 0) % 2)

        geo = settings().value("ui/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(1280, 860)
        self.statusBar().showMessage("Ready — Ctrl+1 Magical Portal, Ctrl+2 NetworkPocketknife, F1 help", 6000)

    def show_portal(self):
        self.top.setCurrentIndex(0)
        self.bring_to_front()

    def bring_to_front(self):
        if self.isMinimized():
            self.showNormal()
        self.show()
        self.raise_()
        self.activateWindow()

    def add_tab(self, name, cls, focus=False):
        n = self.counts.get(name, 0) + 1
        self.counts[name] = n
        w = cls()
        idx = self.tabs.addTab(w, name if n == 1 else f"{name} {n}")
        if focus:
            self.top.setCurrentIndex(1)
            self.tabs.setCurrentIndex(idx)
        return w

    def close_tab(self, idx):
        w = self.tabs.widget(idx)
        if getattr(w, "running", False) and QMessageBox.question(
                self, "Close tab", "This tab has an active connection. Close it?") != QMessageBox.StandardButton.Yes:
            return
        w.shutdown()
        self.tabs.removeTab(idx)
        w.deleteLater()

    def closeEvent(self, ev):
        s = settings()
        s.setValue("ui/geometry", self.saveGeometry())
        s.setValue("ui/tab", self.tabs.currentIndex())
        s.setValue("ui/top", self.top.currentIndex())
        for i in range(self.tabs.count()):
            self.tabs.widget(i).shutdown()
        self.portal.shutdown()              # the portal server itself keeps running
        super().closeEvent(ev)


def portal_icon_path():
    d = find_portal_dir()
    p = os.path.join(d, "web", "portal.ico") if d else ""
    return p if p and os.path.isfile(p) else None


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PortalSystem.Suite")
        except Exception:
            pass
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)   # needed by the web view
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_COMPANY)

    # one window only: a second start (desktop icon, tray icon) just brings this one forward
    key = "PortalSystem-" + "".join(c for c in os.environ.get("USERNAME", "user") if c.isalnum())
    probe = QLocalSocket()
    probe.connectToServer(key)
    if probe.waitForConnected(300):
        probe.write(b"portal" if "--portal" in sys.argv else b"show")
        probe.waitForBytesWritten(500)
        probe.disconnectFromServer()
        return
    QLocalServer.removeServer(key)
    server = QLocalServer()
    server.listen(key)

    w = MainWindow()

    def on_connection():
        c = server.nextPendingConnection()
        if c is None:
            return

        def handle():
            msg = bytes(c.readAll()).decode(errors="ignore")
            w.show_portal() if "portal" in msg else w.bring_to_front()
            c.disconnectFromServer()
        c.readyRead.connect(handle)
    server.newConnection.connect(on_connection)

    if "--portal" in sys.argv:
        w.top.setCurrentIndex(0)
    w.show()
    QTimer.singleShot(200, w.portal.ensure_started)   # start the portal server if it isn't running
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
