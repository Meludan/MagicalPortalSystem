"""
Windows notification-area (system tray) icon for Magical Portal System.
Pure ctypes - nothing to install. Imported only on Windows.

Left-click  : open the control page
Right-click : menu (open, start all, stop all, shut down)
Hover       : how many portals are running
Pop-ups     : signal lost / restored, portal stopped with an error
"""

import ctypes
import threading
import time
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

WM_DESTROY, WM_CLOSE, WM_COMMAND, WM_TIMER, WM_NULL = 0x0002, 0x0010, 0x0111, 0x0113, 0x0000
WM_LBUTTONUP, WM_RBUTTONUP, WM_LBUTTONDBLCLK = 0x0202, 0x0205, 0x0203
WM_APP = 0x8000
WM_TRAY = WM_APP + 1      # mouse events from the icon
WM_NOTIFY_POP = WM_APP + 2  # show a queued balloon
NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP, NIF_INFO = 0x01, 0x02, 0x04, 0x10
NIIF_INFO, NIIF_WARNING = 0x01, 0x02
IMAGE_ICON, LR_LOADFROMFILE, LR_DEFAULTSIZE = 1, 0x10, 0x40
IDI_APPLICATION = 32512
MF_STRING, MF_GRAYED, MF_SEPARATOR = 0x0000, 0x0001, 0x0800
TPM_RIGHTBUTTON, TPM_NONOTIFY, TPM_RETURNCMD = 0x0002, 0x0080, 0x0100
SM_CXSMICON, SM_CYSMICON = 49, 50


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8)]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND), ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT), ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD), ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256), ("uVersion", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64), ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", GUID), ("hBalloonIcon", wintypes.HICON)]


# --- function signatures (needed for 64-bit Windows) ---
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.LoadImageW.restype = wintypes.HANDLE
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPVOID]
user32.LoadIconW.restype = wintypes.HICON
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.RegisterWindowMessageW.restype = wintypes.UINT
user32.CreatePopupMenu.restype = wintypes.HMENU
user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.SetMenuDefaultItem.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.UINT]
user32.TrackPopupMenu.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                  wintypes.HWND, wintypes.LPVOID]
user32.TrackPopupMenu.restype = wintypes.BOOL
user32.DestroyMenu.argtypes = [wintypes.HMENU]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE

ID_OPEN, ID_START, ID_STOP, ID_STATUS, ID_QUIT = 1, 2, 3, 4, 9


class TrayIcon:
    def __init__(self, icon_path, on_open, on_start_all, on_stop_all, on_quit, status_text):
        self.icon_path = icon_path
        self.on_open, self.on_start_all, self.on_stop_all, self.on_quit = on_open, on_start_all, on_stop_all, on_quit
        self.status_text = status_text          # () -> str
        self.hwnd = None
        self._nid = None
        self._pending = []                       # queued balloons
        self._lock = threading.Lock()
        self._last_open = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="tray", daemon=True)

    # ------------------------------------------------------------ public
    def start(self):
        self._thread.start()
        self._ready.wait(5)

    def stop(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
            self._thread.join(3)

    def notify(self, title, message, warning=False):
        with self._lock:
            self._pending.append((title[:63], message[:255], warning))
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_NOTIFY_POP, 0, 0)

    # ------------------------------------------------------------ internals
    def _bg(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    def _load_icon(self):
        cx, cy = user32.GetSystemMetrics(SM_CXSMICON), user32.GetSystemMetrics(SM_CYSMICON)
        h = user32.LoadImageW(None, self.icon_path, IMAGE_ICON, cx, cy, LR_LOADFROMFILE)
        return h or user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))

    def _tip(self):
        try:
            return ("Magical Portal System\n" + self.status_text())[:127]
        except Exception:
            return "Magical Portal System"

    def _add_icon(self):
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self.hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAY
        nid.hIcon = self._hicon
        nid.szTip = self._tip()
        shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid))
        self._nid = nid

    def _update_tip(self):
        if not self._nid:
            return
        self._nid.uFlags = NIF_TIP
        self._nid.szTip = self._tip()
        shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

    def _show_pending(self):
        with self._lock:
            items, self._pending = self._pending, []
        for title, msg, warn in items[-3:]:   # never spam more than a few at once
            self._nid.uFlags = NIF_INFO
            self._nid.szInfoTitle = title
            self._nid.szInfo = msg or " "
            self._nid.dwInfoFlags = NIIF_WARNING if warn else NIIF_INFO
            shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(self._nid))

    def _menu(self):
        menu = user32.CreatePopupMenu()
        user32.AppendMenuW(menu, MF_STRING, ID_OPEN, "Open control page")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_START, "Start all senders")
        user32.AppendMenuW(menu, MF_STRING, ID_STOP, "Stop all portals")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        try:
            status = self.status_text()
        except Exception:
            status = ""
        user32.AppendMenuW(menu, MF_STRING | MF_GRAYED, ID_STATUS, status or "Magical Portal System")
        user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
        user32.AppendMenuW(menu, MF_STRING, ID_QUIT, "Shut down Magical Portal System")
        user32.SetMenuDefaultItem(menu, ID_OPEN, 0)
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        user32.SetForegroundWindow(self.hwnd)   # so the menu closes when clicking elsewhere
        cmd = user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY,
                                    pt.x, pt.y, 0, self.hwnd, None)
        user32.PostMessageW(self.hwnd, WM_NULL, 0, 0)
        user32.DestroyMenu(menu)
        if cmd == ID_OPEN:
            self._open()
        elif cmd == ID_START:
            self._bg(self.on_start_all)
        elif cmd == ID_STOP:
            self._bg(self.on_stop_all)
        elif cmd == ID_QUIT:
            self._bg(self.on_quit)

    def _open(self):
        if time.time() - self._last_open > 1.5:   # one click = one browser tab
            self._last_open = time.time()
            self._bg(self.on_open)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_TRAY:
                if lparam in (WM_LBUTTONUP, WM_LBUTTONDBLCLK):
                    self._open()
                elif lparam == WM_RBUTTONUP:
                    self._menu()
                return 0
            if msg == WM_TIMER:
                self._update_tip()
                return 0
            if msg == WM_NOTIFY_POP:
                self._show_pending()
                return 0
            if msg == self._taskbar_created:   # Explorer restarted - put the icon back
                self._add_icon()
                return 0
            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                if self._nid:
                    shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._nid))
                    self._nid = None
                user32.PostQuitMessage(0)
                return 0
        except Exception as e:
            print(f"[tray] {e}")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run(self):
        try:
            hinst = kernel32.GetModuleHandleW(None)
            self._proc = WNDPROC(self._wndproc)        # keep a reference
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._proc
            wc.hInstance = hinst
            wc.lpszClassName = "MagicalPortalSystemTray"
            user32.RegisterClassW(ctypes.byref(wc))
            self._taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
            self.hwnd = user32.CreateWindowExW(0, wc.lpszClassName, "Magical Portal System", 0,
                                               0, 0, 0, 0, None, None, hinst, None)
            self._hicon = self._load_icon()
            self._add_icon()
            user32.SetTimer(self.hwnd, 1, 3000, None)  # refresh the hover text every 3 s
        except Exception as e:
            print(f"[tray] could not create tray icon: {e}")
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
