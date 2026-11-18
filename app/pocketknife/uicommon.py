"""Shared UI helpers for NetworkPocketknife."""
import datetime
import os

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit, QSpinBox

APP_NAME = "NetworkPocketknife"
APP_COMPANY = "Portal System"
APP_VERSION = "1.4"



def appdata_dir():
    d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_COMPANY, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def settings():
    return QSettings(APP_COMPANY, APP_NAME)


def mono_font(size=10):
    families = set(QFontDatabase.families())
    for fam in ("Cascadia Mono", "Consolas", "Courier New"):
        if fam in families:
            f = QFont(fam, size)
            break
    else:
        f = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        f.setPointSize(size)
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setFixedPitch(True)
    return f


def port_spin(value):
    s = QSpinBox()
    s.setRange(0, 65535)
    s.setValue(value)
    s.setMinimumWidth(80)
    return s


def persist(widget, key):
    """Restore a widget's value from settings and save it whenever it changes."""
    s = settings()
    v = s.value(key)
    if isinstance(widget, QLineEdit):
        if v is not None:
            widget.setText(str(v))
        widget.textChanged.connect(lambda t: settings().setValue(key, t))
    elif isinstance(widget, QSpinBox):
        if v is not None:
            try:
                widget.setValue(int(v))
            except (TypeError, ValueError):
                pass
        widget.valueChanged.connect(lambda x: settings().setValue(key, x))
    elif isinstance(widget, QCheckBox):
        if v is not None:
            widget.setChecked(str(v).lower() in ("true", "1"))
        widget.toggled.connect(lambda x: settings().setValue(key, x))
    elif isinstance(widget, QComboBox):
        if v is not None:
            if widget.isEditable():
                widget.setCurrentText(str(v))
            else:
                i = widget.findText(str(v))
                if i >= 0:
                    widget.setCurrentIndex(i)
        widget.currentTextChanged.connect(lambda t: settings().setValue(key, t))
    return widget


def now_ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


class Bridge(QObject):
    """Carries callbacks from worker threads into the GUI thread."""
    data = Signal(object, str)
    status = Signal(str)
    error = Signal(str)
    closed = Signal(int)
    clients = Signal(object)
    hostkey = Signal(str, str, str, object)


