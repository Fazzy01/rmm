"""
Minimal cross-platform RMM agent prototype (Python).

Runs on Windows and macOS from this one file. Linux is untested but the
shell/screen-view/register/heartbeat paths below don't touch any
platform-specific API, so it likely already works for those; only remote
mouse/keyboard control and the isolated desktop are Windows/macOS-gated.

Platform support matrix:
                          Windows   macOS
  connect/register/shell    yes      yes
  screen view ("user")      yes      yes  (needs Screen Recording grant)
  remote mouse/keyboard     yes      yes  (needs Accessibility grant)
  isolated desktop          yes      no   (no macOS equivalent - see
                                            create_isolated_desktop below)

See installer/install-macos.sh for the macOS install (LaunchAgent +
one-time permission grants; Apple's TCC subsystem does not allow those
grants to be scripted, same one-time-consent model used for Android
elsewhere in this project) vs installer/rmm-agent.iss for Windows.

Connects to the Django Channels backend, listens for admin commands, and
executes them:
  - uninstall                  : removes the installer's startup Scheduled
                                  Task and exits immediately - the remote
                                  "stop this connection, don't come back"
                                  command. Also sent automatically by the
                                  dashboard's device-delete action when the
                                  device is currently online.
  - run_powershell / run_shell : executes a command, returns stdout/stderr.
                                  Always runs invisibly (no console flash on
                                  the user's screen), regardless of desktop
                                  mode.
  - start_screen_stream        : begins sending PNG screen captures for one
    stop_screen_stream           view, named by payload.view:
                                    "user"     - the user's real, physical
                                               screen (mss).
                                    "isolated" - the private admin desktop
                                               (only meaningful once
                                               create_isolated_desktop has
                                               run); composites whatever
                                               windows the admin has opened
                                               there via PrintWindow, never
                                               touching the user's screen.
                                  Both can be streaming at once - each
                                  screen_frame reply carries its own
                                  payload.view so the dashboard can show
                                  them side by side independently.
  - create_isolated_desktop    : (Windows only) spawns a separate Win32
                                  desktop object so admin actions in that
                                  desktop don't appear on the user's screen,
                                  and starts the background capture worker
                                  for the "isolated" view above.
  - exit_isolated_desktop      : stops watching/capturing the isolated
                                  desktop, but leaves whatever's running in
                                  it alone - Windows keeps the desktop
                                  object alive as long as those processes
                                  hold a reference to it, so re-enabling
                                  later resumes the exact same session.
  - clear_isolated_desktop     : the destructive counterpart - actually
                                  terminates everything running in the
                                  isolated desktop, then immediately
                                  creates a fresh empty one. One click to
                                  close all the folders/apps that piled up
                                  and start over, distinct from exit above.
  - mouse_move/mouse_down/     : remote-control input. x/y are normalized
    mouse_up/mouse_wheel         (0.0-1.0), resolution-independent. Routed
    key_down/key_up              by payload.view (defaults to "user"):
                                    "user"     -> SendInput on the real
                                               desktop (only place SendInput
                                               can target).
                                    "isolated" -> PostMessage directly to
                                               the window under the cursor,
                                               inside the isolated desktop
                                               only. Fully independent of
                                               the real desktop: the admin
                                               can open/use/close anything
                                               there (confirmed working
                                               even for Chromium/Electron
                                               content, not just classic
                                               Win32 controls) without the
                                               user seeing or losing focus
                                               on anything on their own
                                               screen, and vice versa.

Run:
    pip install -r requirements.txt
    python agent.py --server ws://localhost:8000 --device test-device-1
"""

import argparse
import asyncio
import base64
import io
import json
import os
import platform
import queue
import socket
import subprocess
import sys
import threading
import time

import websockets

IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"

# --- Optional Windows-only isolated desktop support ---------------------
if IS_WINDOWS:
    try:
        import ctypes
        import ctypes.wintypes as wt
        import win32con
        import win32process
        import win32security
        import win32service
        HAVE_PYWIN32 = True
    except ImportError:
        HAVE_PYWIN32 = False
else:
    HAVE_PYWIN32 = False

# --- Optional macOS-only remote mouse/keyboard control -------------------
# Uses Quartz (pyobjc-framework-Quartz), the same low-level event-injection
# API macOS's own Accessibility APIs are built on. Requires the process to
# be granted "Accessibility" (to post events) and "Screen Recording" (for
# mss to capture frames) once, by hand, in System Settings > Privacy &
# Security - macOS's TCC subsystem does not allow a script to grant these
# to itself, by design, same as Android's consent dialogs. See
# installer/install-macos.sh for the one-time setup that opens those panes.
if IS_MACOS:
    try:
        import Quartz
        HAVE_QUARTZ = True
    except ImportError:
        HAVE_QUARTZ = False
else:
    HAVE_QUARTZ = False

CREATE_NO_WINDOW = 0x08000000  # hide the console flash for shell commands

if IS_WINDOWS and HAVE_PYWIN32:
    _user32 = ctypes.windll.user32
    _gdi32 = ctypes.windll.gdi32

    # ctypes.windll defaults every return value to a 32-bit c_int, which
    # silently truncates real 64-bit HANDLE/HDC/HBITMAP pointers on a
    # 64-bit process and corrupts every GDI call downstream. Every
    # handle-returning or handle-taking prototype must be set explicitly.
    _user32.SetThreadDesktop.argtypes = [ctypes.c_void_p]
    _user32.SetThreadDesktop.restype = wt.BOOL
    _user32.CloseDesktop.argtypes = [ctypes.c_void_p]
    _user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
    _user32.PrintWindow.restype = wt.BOOL
    _user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
    _user32.GetDC.restype = wt.HDC
    _user32.GetDC.argtypes = [wt.HWND]
    _user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
    _user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
    _user32.PostMessageW.restype = wt.BOOL
    _user32.ScreenToClient.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
    _user32.WindowFromPoint.argtypes = [wt.POINT]
    _user32.WindowFromPoint.restype = wt.HWND
    _user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    _user32.GetWindowThreadProcessId.restype = wt.DWORD
    _user32.BringWindowToTop.argtypes = [wt.HWND]
    _user32.SetForegroundWindow.argtypes = [wt.HWND]
    _user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
    _user32.GetAncestor.restype = wt.HWND
    _GA_ROOT = 2

    _WM_MOUSEMOVE = 0x0200
    _WM_LBUTTONDOWN, _WM_LBUTTONUP, _WM_LBUTTONDBLCLK = 0x0201, 0x0202, 0x0203
    _WM_RBUTTONDOWN, _WM_RBUTTONUP, _WM_RBUTTONDBLCLK = 0x0204, 0x0205, 0x0206
    _WM_MBUTTONDOWN, _WM_MBUTTONUP, _WM_MBUTTONDBLCLK = 0x0207, 0x0208, 0x0209
    _WM_MOUSEWHEEL = 0x020A
    _WM_KEYDOWN, _WM_KEYUP, _WM_CHAR = 0x0100, 0x0101, 0x0102
    _MK_LBUTTON, _MK_RBUTTON, _MK_MBUTTON = 0x0001, 0x0002, 0x0010
    _MOUSE_DOWN_MSGS = {
        "left": (_WM_LBUTTONDOWN, _MK_LBUTTON), "right": (_WM_RBUTTONDOWN, _MK_RBUTTON),
        "middle": (_WM_MBUTTONDOWN, _MK_MBUTTON),
    }
    _MOUSE_UP_MSGS = {
        "left": (_WM_LBUTTONUP, 0), "right": (_WM_RBUTTONUP, 0), "middle": (_WM_MBUTTONUP, 0),
    }
    # Windows only auto-promotes a fast down/up/down/up into a real double-
    # click for genuine hardware input dispatched through the normal input
    # pipeline - a directly-PostMessage'd pair of clicks is NOT recognized
    # as one, so double-click needs this explicit message.
    _MOUSE_DBLCLK_MSGS = {
        "left": (_WM_LBUTTONDBLCLK, _MK_LBUTTON), "right": (_WM_RBUTTONDBLCLK, _MK_RBUTTON),
        "middle": (_WM_MBUTTONDBLCLK, _MK_MBUTTON),
    }

    _gdi32.CreateCompatibleDC.restype = wt.HDC
    _gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
    _gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
    _gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
    _gdi32.SelectObject.restype = ctypes.c_void_p
    _gdi32.SelectObject.argtypes = [wt.HDC, ctypes.c_void_p]
    _gdi32.GetDIBits.restype = ctypes.c_int
    _gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p,
                                  ctypes.c_void_p, wt.UINT]
    _gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    _gdi32.DeleteDC.argtypes = [wt.HDC]

    _WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    _PW_RENDERFULLCONTENT = 0x00000002

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
            ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
            ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG),
            ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
        ]

    class _BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]

    # --- SendInput plumbing (mouse/keyboard remote control) -------------
    # SendInput always targets whichever desktop is the current *input*
    # desktop - i.e. the real/Default one the user is looking at. It has
    # no effect on a background desktop (like the isolated one), which is
    # why remote control below is gated to the "user" screen view only.
    _ULONG_PTR = ctypes.c_size_t

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                    ("dwFlags", wt.DWORD), ("time", wt.DWORD), ("dwExtraInfo", _ULONG_PTR)]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                    ("time", wt.DWORD), ("dwExtraInfo", _ULONG_PTR)]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]

    class _INPUT_UNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wt.DWORD), ("u", _INPUT_UNION)]

    _user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
    _user32.SendInput.restype = wt.UINT
    _user32.VkKeyScanW.argtypes = [wt.WCHAR]
    _user32.VkKeyScanW.restype = ctypes.c_short

    _INPUT_MOUSE = 0
    _INPUT_KEYBOARD = 1

    _MOUSEEVENTF_MOVE = 0x0001
    _MOUSEEVENTF_ABSOLUTE = 0x8000
    _MOUSEEVENTF_WHEEL = 0x0800
    _MOUSE_BUTTON_FLAGS = {
        "left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040),
    }
    _KEYEVENTF_KEYUP = 0x0002

    _SPECIAL_KEYS = {
        "Enter": 0x0D, "Backspace": 0x08, "Tab": 0x09, "Escape": 0x1B, " ": 0x20,
        "Delete": 0x2E, "ArrowUp": 0x26, "ArrowDown": 0x28, "ArrowLeft": 0x25, "ArrowRight": 0x27,
        "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22, "Insert": 0x2D,
        "Shift": 0x10, "Control": 0x11, "Alt": 0x12, "CapsLock": 0x14, "Meta": 0x5B,
        "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
        "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    }

    def _key_to_vk(key):
        """Shared by both input paths (SendInput for the real desktop,
        PostMessage for the isolated one)."""
        if key in _SPECIAL_KEYS:
            return _SPECIAL_KEYS[key], False
        if len(key) == 1:
            res = _user32.VkKeyScanW(key)
            if res == -1:
                return None, False
            return res & 0xFF, bool((res >> 8) & 1)
        return None, False

    class RemoteInput:
        """Injects mouse/keyboard events via SendInput. Coordinates are
        normalized (0.0-1.0) so they're resolution-independent - matches
        the client's screen_frame images regardless of actual pixel size,
        and keeps this side of the protocol reusable by a future
        non-Windows (e.g. mobile) agent even though the injection code
        underneath would differ."""

        @staticmethod
        def _send(inputs):
            arr = (_INPUT * len(inputs))(*inputs)
            _user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))

        @classmethod
        def mouse_event(cls, x, y, extra_flags=0, mouse_data=0):
            inp = _INPUT(type=_INPUT_MOUSE)
            inp.u.mi = _MOUSEINPUT(
                int(max(0.0, min(1.0, x)) * 65535),
                int(max(0.0, min(1.0, y)) * 65535),
                mouse_data,
                _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | extra_flags,
                0, 0,
            )
            cls._send([inp])

        @classmethod
        def mouse_down(cls, x, y, button):
            down, _ = _MOUSE_BUTTON_FLAGS.get(button, _MOUSE_BUTTON_FLAGS["left"])
            cls.mouse_event(x, y, down)

        @classmethod
        def mouse_up(cls, x, y, button):
            _, up = _MOUSE_BUTTON_FLAGS.get(button, _MOUSE_BUTTON_FLAGS["left"])
            cls.mouse_event(x, y, up)

        @classmethod
        def mouse_wheel(cls, x, y, delta):
            cls.mouse_event(x, y, _MOUSEEVENTF_WHEEL, mouse_data=int(delta))

        @classmethod
        def key_event(cls, key, key_up):
            vk, need_shift = _key_to_vk(key)
            if vk is None:
                return
            flags = _KEYEVENTF_KEYUP if key_up else 0
            inputs = []
            if need_shift and not key_up:
                shift = _INPUT(type=_INPUT_KEYBOARD)
                shift.u.ki = _KEYBDINPUT(0x10, 0, 0, 0, 0)
                inputs.append(shift)
            main = _INPUT(type=_INPUT_KEYBOARD)
            main.u.ki = _KEYBDINPUT(vk, 0, flags, 0, 0)
            inputs.append(main)
            if need_shift and key_up:
                shift_up = _INPUT(type=_INPUT_KEYBOARD)
                shift_up.u.ki = _KEYBDINPUT(0x10, 0, _KEYEVENTF_KEYUP, 0, 0)
                inputs.append(shift_up)
            cls._send(inputs)


if IS_MACOS and HAVE_QUARTZ:
    # Layout-independent virtual keycodes for the same non-printable keys
    # _SPECIAL_KEYS covers on Windows. Printable characters don't go
    # through this table at all - see key_event() below.
    _MAC_SPECIAL_KEYS = {
        "Enter": 0x24, "Tab": 0x30, " ": 0x31, "Backspace": 0x33, "Escape": 0x35,
        "Meta": 0x37, "Shift": 0x38, "CapsLock": 0x39, "Alt": 0x3A, "Control": 0x3B,
        "Home": 0x73, "PageUp": 0x74, "Delete": 0x75, "End": 0x77, "PageDown": 0x79,
        "F1": 0x7A, "F2": 0x78, "F3": 0x63, "F4": 0x76, "F5": 0x60, "F6": 0x61,
        "F7": 0x62, "F8": 0x64, "F9": 0x65, "F10": 0x6D, "F11": 0x67, "F12": 0x6F,
        "ArrowLeft": 0x7B, "ArrowRight": 0x7C, "ArrowDown": 0x7D, "ArrowUp": 0x7E,
    }
    _MAC_BUTTONS = {
        "left": (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGMouseButtonLeft),
        "right": (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp, Quartz.kCGMouseButtonRight),
        "middle": (Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp, Quartz.kCGMouseButtonCenter),
    }

    class MacRemoteInput:
        """Same normalized-coordinate interface as RemoteInput above, built
        on CGEvent injection instead of SendInput. Posted at the HID event
        tap so it behaves like real hardware input to every app, the same
        guarantee SendInput gives on Windows."""

        @staticmethod
        def _screen_size():
            bounds = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
            return bounds.size.width, bounds.size.height

        @classmethod
        def _point(cls, x, y):
            w, h = cls._screen_size()
            return Quartz.CGPointMake(max(0.0, min(1.0, x)) * w, max(0.0, min(1.0, y)) * h)

        @classmethod
        def _post(cls, event):
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)

        @classmethod
        def mouse_event(cls, x, y):
            event = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventMouseMoved, cls._point(x, y), Quartz.kCGMouseButtonLeft
            )
            cls._post(event)

        @classmethod
        def _click(cls, x, y, button, down, click_count=1):
            down_type, up_type, cg_button = _MAC_BUTTONS.get(button, _MAC_BUTTONS["left"])
            event = Quartz.CGEventCreateMouseEvent(
                None, down_type if down else up_type, cls._point(x, y), cg_button
            )
            if click_count > 1:
                Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventClickState, click_count)
            cls._post(event)

        @classmethod
        def mouse_down(cls, x, y, button):
            cls._click(x, y, button, down=True)

        @classmethod
        def mouse_up(cls, x, y, button):
            cls._click(x, y, button, down=False)

        @classmethod
        def mouse_dblclick(cls, x, y, button):
            # Quartz recognizes a double-click via the click-state field on
            # the event itself, unlike Windows which needs a distinct
            # WM_*DBLCLK message - one down/up pair with click_count=2 is
            # enough for the receiving app to treat it as a double-click.
            cls._click(x, y, button, down=True, click_count=2)
            cls._click(x, y, button, down=False, click_count=2)

        @classmethod
        def mouse_wheel(cls, x, y, delta):
            event = Quartz.CGEventCreateScrollWheelEvent(
                None, Quartz.kCGScrollEventUnitPixel, 1, int(delta)
            )
            cls._post(event)

        @classmethod
        def key_event(cls, key, key_up):
            if key in _MAC_SPECIAL_KEYS:
                event = Quartz.CGEventCreateKeyboardEvent(None, _MAC_SPECIAL_KEYS[key], not key_up)
            elif len(key) == 1:
                # keycode 0 + CGEventKeyboardSetUnicodeString: injects the
                # character directly, independent of keyboard layout -
                # simpler and more reliable than Windows' VkKeyScanW path,
                # which needs a real virtual-key/shift mapping per layout.
                event = Quartz.CGEventCreateKeyboardEvent(None, 0, not key_up)
                Quartz.CGEventKeyboardSetUnicodeString(event, len(key), key)
            else:
                return
            cls._post(event)


class IsolatedDesktopCapture:
    """
    Dedicated OS thread pinned (via SetThreadDesktop) to the isolated
    desktop for its whole lifetime, so it can PrintWindow-capture whatever
    windows live there without ever touching the real/default desktop the
    user is looking at.

    A background (non-input) desktop's GDI surface can't be BitBlt'd
    directly - only the currently-visible desktop composites that way.
    PrintWindow(..., PW_RENDERFULLCONTENT) works regardless, because it
    asks each window to render itself into our buffer rather than reading
    a screen surface.

    Exposes only the latest frame (via a lock-protected attribute), not a
    queue: a queue with maxsize=1 fed by put_nowait() gets stuck holding
    whatever frame arrived first (typically an empty desktop, captured
    before anything was launched into it) because every later put fails
    silently once it's full - the opposite of the "always show the newest
    frame" behavior a live view needs.
    """

    def __init__(self, hdesk):
        self._hdesk_int = int(hdesk)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest_frame = None
        self._input_queue = queue.Queue()
        self._focus_hwnd = None  # last-clicked window, used as the keyboard target
        self._last_activated = None  # avoids redundant activation calls every click
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def get_latest_frame(self):
        with self._lock:
            return self._latest_frame

    def post_input(self, kind, payload):
        """Thread-safe: called from the asyncio loop's thread, consumed by
        the worker thread below."""
        self._input_queue.put((kind, payload))

    def _run(self):
        if not _user32.SetThreadDesktop(self._hdesk_int):
            return
        last_capture = 0.0
        while not self._stop.is_set():
            self._drain_input()
            now = time.time()
            if now - last_capture >= 0.5:
                png = self._capture_composite()
                if png is not None:
                    with self._lock:
                        self._latest_frame = png
                last_capture = now
            time.sleep(0.02)  # keep input responsive between capture cycles

    def _drain_input(self):
        while True:
            try:
                kind, payload = self._input_queue.get_nowait()
            except queue.Empty:
                return
            self._handle_input(kind, payload)

    def _window_at(self, x_px, y_px):
        """Topmost visible window at an absolute point on this desktop -
        WindowFromPoint only sees windows on the calling thread's current
        desktop, which is this one since we SetThreadDesktop'd above."""
        pt = wt.POINT(x_px, y_px)
        hwnd = _user32.WindowFromPoint(pt)
        return hwnd or None

    def _activate_if_needed(self, hwnd):
        """Bring the clicked window's top-level owner to the front of this
        desktop's own z-order on first click. Confirmed by testing: some
        modern apps (Windows 11 File Explorer's tab strip/address bar/
        command bar, built with WinUI) simply don't render ANY content
        until activated at least once - PrintWindow captures a blank strip
        above the file list otherwise, and clicks are unreliable too. This
        only reorders windows within our own isolated desktop and has no
        effect on the user's real desktop."""
        top = _user32.GetAncestor(hwnd, _GA_ROOT) or hwnd
        if top != self._last_activated:
            _user32.BringWindowToTop(top)
            _user32.SetForegroundWindow(top)  # often fails quietly here; harmless
            self._last_activated = top

    def _handle_input(self, kind, payload):
        width = _user32.GetSystemMetrics(0)
        height = _user32.GetSystemMetrics(1)
        x_px = int(max(0.0, min(1.0, payload.get("x", 0.5))) * width)
        y_px = int(max(0.0, min(1.0, payload.get("y", 0.5))) * height)

        if kind in ("mouse_move", "mouse_down", "mouse_up", "mouse_dblclick", "mouse_wheel"):
            hwnd = self._window_at(x_px, y_px)
            if hwnd is None:
                return
            pt = wt.POINT(x_px, y_px)
            _user32.ScreenToClient(hwnd, ctypes.byref(pt))
            lparam = (pt.y << 16) | (pt.x & 0xFFFF)
            if kind == "mouse_move":
                _user32.PostMessageW(hwnd, _WM_MOUSEMOVE, 0, lparam)
            elif kind == "mouse_down":
                self._activate_if_needed(hwnd)
                self._focus_hwnd = hwnd
                msg, wparam = _MOUSE_DOWN_MSGS.get(payload.get("button", "left"), (_WM_LBUTTONDOWN, _MK_LBUTTON))
                _user32.PostMessageW(hwnd, msg, wparam, lparam)
            elif kind == "mouse_up":
                msg, wparam = _MOUSE_UP_MSGS.get(payload.get("button", "left"), (_WM_LBUTTONUP, 0))
                _user32.PostMessageW(hwnd, msg, wparam, lparam)
            elif kind == "mouse_dblclick":
                # Real double-click sequence is down, up, DBLCLK, up - the
                # first down/up already went out as an ordinary click; this
                # message stands in for the second down.
                self._focus_hwnd = hwnd
                msg, wparam = _MOUSE_DBLCLK_MSGS.get(payload.get("button", "left"), (_WM_LBUTTONDBLCLK, _MK_LBUTTON))
                _user32.PostMessageW(hwnd, msg, wparam, lparam)
            elif kind == "mouse_wheel":
                # WM_MOUSEWHEEL is the one mouse message that wants SCREEN
                # (not client) coordinates in lParam.
                screen_lparam = (y_px << 16) | (x_px & 0xFFFF)
                wparam = (int(payload.get("delta", 0)) & 0xFFFF) << 16
                _user32.PostMessageW(hwnd, _WM_MOUSEWHEEL, wparam, screen_lparam)

        elif kind in ("key_down", "key_up"):
            hwnd = self._focus_hwnd
            if hwnd is None:
                return
            key = payload.get("key", "")
            vk, _need_shift = _key_to_vk(key)
            if vk is None:
                return
            if kind == "key_down":
                _user32.PostMessageW(hwnd, _WM_KEYDOWN, vk, 0)
                if len(key) == 1:  # printable - also send WM_CHAR so text controls see it
                    _user32.PostMessageW(hwnd, _WM_CHAR, ord(key), 0)
            else:
                _user32.PostMessageW(hwnd, _WM_KEYUP, vk, 0)

    def _enum_visible_windows(self):
        found = []

        def cb(hwnd, lparam):
            if _user32.IsWindowVisible(hwnd) and not _user32.IsIconic(hwnd):
                rect = wt.RECT()
                _user32.GetWindowRect(hwnd, ctypes.byref(rect))
                w, h = rect.right - rect.left, rect.bottom - rect.top
                if w > 50 and h > 50:
                    found.append((hwnd, rect.left, rect.top, w, h))
            return True

        _user32.EnumDesktopWindows(self._hdesk_int, _WNDENUMPROC(cb), 0)
        return found  # topmost-first (Z-order)

    def _print_window(self, hwnd, w, h):
        ref_dc = _user32.GetDC(0)
        mem_dc = _gdi32.CreateCompatibleDC(ref_dc)
        bmp = _gdi32.CreateCompatibleBitmap(ref_dc, w, h)
        _gdi32.SelectObject(mem_dc, bmp)
        try:
            if not _user32.PrintWindow(hwnd, mem_dc, _PW_RENDERFULLCONTENT):
                return None
            bmi = _BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = w
            bmi.bmiHeader.biHeight = -h
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0
            buf = (ctypes.c_ubyte * (w * h * 4))()
            if not _gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0):
                return None
            from PIL import Image
            return Image.frombuffer("RGB", (w, h), bytes(buf), "raw", "BGRX", 0, 1)
        finally:
            _gdi32.DeleteObject(bmp)
            _gdi32.DeleteDC(mem_dc)
            _user32.ReleaseDC(0, ref_dc)

    def _capture_composite(self):
        from PIL import Image
        width = _user32.GetSystemMetrics(0)
        height = _user32.GetSystemMetrics(1)
        canvas = Image.new("RGB", (width, height), (32, 32, 32))
        windows = self._enum_visible_windows()
        for hwnd, x, y, w, h in reversed(windows):  # draw bottommost first
            img = self._print_window(hwnd, w, h)
            if img is not None:
                canvas.paste(img, (x, y))
        buf = io.BytesIO()
        canvas.save(buf, format="PNG")
        return buf.getvalue()


class IsolatedDesktop:
    """
    Wraps the Win32 CreateDesktop APIs.

    NOTE: this is a same-session virtual desktop, not a second Windows
    login session. It's the practical, licensing-safe way to run admin
    tooling (PowerShell, file browsing, even GUI apps) without it
    appearing on the user's visible desktop - confirmed: windows created
    here never show up when enumerating the real/Default desktop, and we
    never call SwitchDesktop, so the user's physical screen is untouched.
    """

    DESKTOP_NAME = "RmmAdminDesktop"

    def __init__(self):
        self.hdesk = None
        self.capture = None

    def create(self):
        if not HAVE_PYWIN32:
            raise RuntimeError("pywin32 not available / not on Windows")
        desired_access = (
            win32con.DESKTOP_CREATEMENU
            | win32con.DESKTOP_CREATEWINDOW
            | win32con.DESKTOP_ENUMERATE
            | win32con.DESKTOP_HOOKCONTROL
            | win32con.DESKTOP_READOBJECTS
            | win32con.DESKTOP_WRITEOBJECTS
            | win32con.DESKTOP_SWITCHDESKTOP
            | win32con.GENERIC_ALL
        )
        self.hdesk = win32service.CreateDesktop(self.DESKTOP_NAME, 0, desired_access, None)
        self.capture = IsolatedDesktopCapture(self.hdesk)
        self.capture.start()
        return self.hdesk

    def launch(self, command):
        """Launch a process into the isolated desktop instead of the user's."""
        if self.hdesk is None:
            raise RuntimeError("isolated desktop not created yet")
        startupinfo = win32process.STARTUPINFO()
        startupinfo.lpDesktop = self.DESKTOP_NAME
        win32process.CreateProcess(
            None, command, None, None, False, 0, None, None, startupinfo
        )

    def _running_pids(self):
        """PIDs of every process with a window on this desktop.
        EnumDesktopWindows/GetWindowThreadProcessId work from any thread
        given an explicit HDESK - no SetThreadDesktop needed, unlike the
        GDI capture calls."""
        if self.hdesk is None:
            return set()
        pids = set()

        def cb(hwnd, lparam):
            pid = wt.DWORD()
            _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                pids.add(pid.value)
            return True

        _user32.EnumDesktopWindows(int(self.hdesk), _WNDENUMPROC(cb), 0)
        return pids

    def kill_all(self):
        """Terminate every process currently running in this desktop.
        Windows keeps a desktop object alive as long as any process still
        holds a reference to it, even after we CloseDesktop our own handle
        - without this, closed sessions kept lingering and their windows
        (extra Notepad tabs, old Explorer windows, ...) reappeared the
        next time the desktop was (re-)created under the same name."""
        for pid in self._running_pids():
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True)

    def close(self, kill=False):
        """kill=False (plain exit): stops watching but leaves whatever's
        running - Windows keeps the desktop object alive as long as those
        processes hold a reference to it, so re-creating it under the same
        name later reattaches to the same session, exactly as left.
        kill=True (clear): actually terminates everything first, so the
        next create() is genuinely empty."""
        if kill:
            self.kill_all()
        if self.capture is not None:
            self.capture.stop()
            self.capture = None
        if self.hdesk is not None:
            # pywin32 doesn't wrap CloseDesktop; call the Win32 API directly.
            ctypes.windll.user32.CloseDesktop(int(self.hdesk))
            self.hdesk = None


class Agent:
    def __init__(self, server, device_id, token=""):
        self.ws_url = f"{server}/ws/agent/{device_id}/"
        if token:
            self.ws_url += f"?token={token}"
        self.device_id = device_id
        self.streaming = {"user": False, "isolated": False}  # independent per view
        self.isolated_desktop = IsolatedDesktop() if IS_WINDOWS else None

    async def run(self):
        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    print(f"[agent] connected as {self.device_id} -> {self.ws_url}")
                    await ws.send(json.dumps({
                        "type": "register",
                        "payload": {"hostname": socket.gethostname(), "os": platform.system()},
                    }))
                    await asyncio.gather(
                        self._receiver(ws),
                        self._heartbeat(ws),
                        self._screen_loop(ws),
                    )
            except (websockets.ConnectionClosed, OSError) as e:
                print(f"[agent] disconnected ({e}), retrying in 3s...")
                await asyncio.sleep(3)

    async def _heartbeat(self, ws):
        while True:
            await ws.send(json.dumps({"type": "log", "payload": {"message": "heartbeat"}}))
            await asyncio.sleep(15)

    async def _receiver(self, ws):
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await self._handle_command(ws, data)

    async def _handle_command(self, ws, data):
        cmd_type = data.get("type")
        payload = data.get("payload", {}) or {}

        if cmd_type == "uninstall":
            # Remote "stop and forget me": remove the startup task (so we
            # don't silently reconnect at next logon) and exit right away.
            # Task name is hardcoded to match installer/rmm-agent.iss's
            # #define TaskName - fine for this project's single-task setup.
            await self._reply(ws, "log", {"message": "uninstalling - removing startup task and disconnecting"})
            if IS_WINDOWS:
                subprocess.run(["schtasks", "/Delete", "/TN", "RmmAgent", "/F"], capture_output=True)
            elif IS_MACOS:
                # Matches the LaunchAgent label installer/install-macos.sh
                # registers. bootout (or the legacy unload fallback) stops
                # it immediately instead of waiting for the next logout.
                plist = os.path.expanduser("~/Library/LaunchAgents/com.rmm.agent.plist")
                subprocess.run(
                    ["launchctl", "bootout", f"gui/{os.getuid()}/com.rmm.agent"], capture_output=True
                )
                subprocess.run(["launchctl", "unload", plist], capture_output=True)
                try:
                    os.remove(plist)
                except OSError:
                    pass
            await ws.close()
            os._exit(0)

        elif cmd_type in ("run_powershell", "run_shell"):
            await self._run_command(ws, data.get("command", ""))

        elif cmd_type == "start_screen_stream":
            self.streaming[payload.get("view", "user")] = True

        elif cmd_type == "stop_screen_stream":
            self.streaming[payload.get("view", "user")] = False

        elif cmd_type == "create_isolated_desktop":
            if not IS_WINDOWS:
                # No macOS/Linux equivalent of a background Win32 desktop
                # object exists - the closest analog (switching to another
                # OS user account) visibly locks the current session and
                # can't run invisibly alongside it, so it isn't implemented.
                await self._reply(ws, "log", {"message": "isolated desktop is Windows-only"})
                return
            if not HAVE_PYWIN32:
                await self._reply(ws, "log", {"message": "pywin32 not installed on agent"})
                return
            self.isolated_desktop.create()
            await self._reply(ws, "log", {"message": "isolated desktop created"})

        elif cmd_type == "exit_isolated_desktop":
            # Just stops watching - whatever's open there keeps running in
            # the background. Re-enabling later reattaches to the same
            # session, exactly as it was left.
            if self.isolated_desktop:
                self.isolated_desktop.close(kill=False)
            self.streaming["isolated"] = False
            await self._reply(ws, "log", {"message": "isolated desktop closed (still running - re-enable to resume)"})

        elif cmd_type == "clear_isolated_desktop":
            if not IS_WINDOWS or not HAVE_PYWIN32:
                await self._reply(ws, "log", {"message": "isolated desktop is Windows-only"})
                return
            # Actually terminates everything, then immediately recreates a
            # genuinely empty one - a one-click "close everything and
            # start over", distinct from the no-kill exit above.
            self.isolated_desktop.close(kill=True)
            self.isolated_desktop.create()
            await self._reply(ws, "log", {"message": "isolated desktop cleared - fresh session started"})

        elif cmd_type in (
            "mouse_move", "mouse_down", "mouse_up", "mouse_dblclick", "mouse_wheel",
            "key_down", "key_up",
        ):
            self._handle_input(cmd_type, payload)

    def _handle_input(self, cmd_type, payload):
        if IS_WINDOWS and HAVE_PYWIN32:
            # Isolated view -> PostMessage into that background desktop
            # only; never touches the real desktop, so the user is never
            # interrupted.
            if payload.get("view", "user") == "isolated":
                if self.isolated_desktop and self.isolated_desktop.capture:
                    self.isolated_desktop.capture.post_input(cmd_type, payload)
                return
            backend = RemoteInput
        elif IS_MACOS and HAVE_QUARTZ:
            # No isolated view on macOS - only "user" input is ever routed
            # here (create_isolated_desktop is rejected up front above).
            backend = MacRemoteInput
        else:
            return

        x, y = payload.get("x", 0.5), payload.get("y", 0.5)
        if cmd_type == "mouse_move":
            backend.mouse_event(x, y)
        elif cmd_type == "mouse_down":
            backend.mouse_down(x, y, payload.get("button", "left"))
        elif cmd_type == "mouse_up":
            backend.mouse_up(x, y, payload.get("button", "left"))
        elif cmd_type == "mouse_dblclick":
            if backend is MacRemoteInput:
                backend.mouse_dblclick(x, y, payload.get("button", "left"))
            else:
                # SendInput has no explicit "double-click" primitive - the
                # OS's own input pipeline recognizes the double-click
                # pattern itself from two down/up cycles close together in
                # time, so this is just the second click-down (touchend
                # still sends the final up).
                backend.mouse_down(x, y, payload.get("button", "left"))
        elif cmd_type == "mouse_wheel":
            backend.mouse_wheel(x, y, payload.get("delta", 0))
        elif cmd_type == "key_down":
            backend.key_event(payload.get("key", ""), key_up=False)
        elif cmd_type == "key_up":
            backend.key_event(payload.get("key", ""), key_up=True)

    async def _run_command(self, ws, command):
        shell_cmd = ["powershell", "-NoProfile", "-Command", command] if IS_WINDOWS else ["bash", "-c", command]
        try:
            # Runs invisibly on the user's real desktop (no console flash).
            # A GUI program the command starts (e.g. Start-Process msedge)
            # inherits the isolated desktop assignment from the agent
            # process itself only if the agent's own desktop is isolated -
            # it isn't, so use isolated_desktop.launch() below for that case.
            if IS_WINDOWS and self.isolated_desktop and self.isolated_desktop.hdesk:
                self.isolated_desktop.launch(" ".join(shell_cmd))
                await self._reply(ws, "command_result", {
                    "output": "(running in the isolated desktop - watch the 'isolated' screen view for output)"
                })
                return
            kwargs = {"creationflags": CREATE_NO_WINDOW} if IS_WINDOWS else {}
            result = subprocess.run(shell_cmd, capture_output=True, text=True, timeout=30, **kwargs)
            output = result.stdout or result.stderr or "(no output)"
            await self._reply(ws, "command_result", {"output": output})
        except Exception as e:
            await self._reply(ws, "command_result", {"error": str(e)})

    async def _screen_loop(self, ws):
        while True:
            if self.streaming["user"]:
                frame_b64 = self._capture_frame()
                if frame_b64:
                    await self._reply(ws, "screen_frame", {"view": "user", "image_b64": frame_b64})

            if self.streaming["isolated"]:
                frame_bytes = self._next_isolated_frame()
                if frame_bytes:
                    await self._reply(ws, "screen_frame", {
                        "view": "isolated", "image_b64": base64.b64encode(frame_bytes).decode("ascii")
                    })

            if self.streaming["user"] or self.streaming["isolated"]:
                await asyncio.sleep(0.5)  # ~2 fps per active view; tune once you test perf
            else:
                await asyncio.sleep(0.2)

    def _next_isolated_frame(self):
        if self.isolated_desktop and self.isolated_desktop.capture:
            return self.isolated_desktop.capture.get_latest_frame()
        return None

    def _capture_frame(self):
        try:
            import mss
            from PIL import Image
        except ImportError:
            return None
        with mss.mss() as sct:
            monitor = sct.monitors[1]
            shot = sct.grab(monitor)
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode("ascii")

    async def _reply(self, ws, msg_type, payload):
        await ws.send(json.dumps({"type": msg_type, "payload": payload}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="ws://localhost:8000")
    parser.add_argument("--device", default="test-device-1")
    parser.add_argument("--token", default=os.environ.get("RMM_TOKEN", ""),
                         help="shared secret; required if the server sets RMM_SHARED_SECRET")
    args = parser.parse_args()

    agent = Agent(args.server, args.device, args.token)
    try:
        asyncio.run(agent.run())
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
