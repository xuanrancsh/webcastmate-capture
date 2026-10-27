# -*- coding: utf-8 -*-
"""WebcastMate Capture - entry point.

Boots a local HTTP service, then opens the UI in the system browser's
"app mode" (Edge / Chrome ``--app=``).  Nothing is embedded, so there is no
renderer that can crash: if the browser exists, the UI shows up.

The app lives as long as its window does - we watch the window itself rather
than the launcher process, because Chromium re-parents itself on start.

Fallbacks, in order: Edge -> Chrome -> default browser.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server                                    # noqa: E402
from core import is_admin, data_dir               # noqa: E402

APP_ID = "WebcastMateCapture.Desktop.1"
MUTEX_NAME = "Global\\WebcastMateCapture_SingleInstance"
UI_TITLE = "抓取直播伴侣画面"

WIN_W, WIN_H = 1080, 720


def _set_app_id():
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def _single_instance():
    """Return a handle if we are the first instance, else None."""
    try:
        k = ctypes.windll.kernel32
        h = k.CreateMutexW(None, False, MUTEX_NAME)
        if k.GetLastError() == 183:       # ERROR_ALREADY_EXISTS
            return None
        return h
    except Exception:
        return True


def _enable_dpi():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_LOG_FP = None


def _setup_log():
    """Windowed builds have no stdout; route everything to a log file."""
    global _LOG_FP
    try:
        d = data_dir()
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "main.log")
        _LOG_FP = open(p, "w", encoding="utf-8", buffering=1)
        sys.stdout = _LOG_FP
        sys.stderr = _LOG_FP
        print("=== WebcastMate Capture start ===")
        print("frozen=%s exe=%s" % (getattr(sys, "frozen", False), sys.executable))
        print("admin=%s" % is_admin())
    except Exception:
        pass


def _log(msg):
    try:
        print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)
    except Exception:
        pass


# --------------------------------------------------------------- browser

BROWSER_REL = (
    ("Microsoft", "Edge", "Application", "msedge.exe"),
    ("Microsoft", "Edge Beta", "Application", "msedge.exe"),
    ("Microsoft", "Edge Dev", "Application", "msedge.exe"),
    ("Google", "Chrome", "Application", "chrome.exe"),
    ("Chromium", "Application", "chrome.exe"),
)


def find_browser():
    """Locate a Chromium-based browser.  Exhaustive but cheap."""
    env = os.environ
    roots = []
    for k in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432",
              "LOCALAPPDATA"):
        v = env.get(k)
        if v and v not in roots:
            roots.append(v)

    for root in roots:
        for parts in BROWSER_REL:
            p = os.path.join(root, *parts)
            if os.path.isfile(p):
                return p

    # registry App Paths (catches custom install locations)
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for exe in ("msedge.exe", "chrome.exe"):
                try:
                    k = winreg.OpenKey(
                        hive,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion"
                        r"\App Paths\%s" % exe)
                    v = (winreg.QueryValue(k, None) or "").strip().strip('"')
                    k.Close()
                    if v and os.path.isfile(v):
                        return v
                except OSError:
                    pass
    except Exception:
        pass
    return None


def _profile_dir():
    """Per-user browser profile - never ProgramData (permission traps)."""
    base = os.environ.get("LOCALAPPDATA") or data_dir()
    d = os.path.join(base, "WebcastMateCapture", "browser")
    try:
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        return os.path.join(os.environ.get("TEMP", "."), "wmc-browser")


def launch_app_window(exe, url):
    """Open *url* as a standalone app window.  Returns the Popen or None."""
    args = [
        exe,
        "--app=" + url,
        "--user-data-dir=" + _profile_dir(),
        "--window-size=%d,%d" % (WIN_W, WIN_H),
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,msEdgeSplitScreen,WebContentsForceDark",
        "--disable-background-mode",
        "--no-service-autorun",
        "--disable-component-update",
        # the UI only ever talks to 127.0.0.1; never let a system proxy
        # (Clash / v2ray / corporate) intercept that and answer 502
        "--no-proxy-server",
    ]
    kw = {"close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008          # DETACHED_PROCESS
    try:
        return subprocess.Popen(args, **kw)
    except Exception as e:
        _log("launch failed: %r" % (e,))
        return None


_ENUM_CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


def find_ui_window():
    """Return the HWND of our app window, or None."""
    u = ctypes.windll.user32
    found = []

    def _each(h, _):
        try:
            n = u.GetWindowTextLengthW(h)
            if not n:
                return True
            b = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(h, b, n + 1)
            if UI_TITLE in b.value and u.IsWindowVisible(h):
                found.append(int(h))
                return False
        except Exception:
            pass
        return True

    try:
        u.EnumWindows(_ENUM_CB(_each), 0)
    except Exception as e:
        _log("enum failed: %r" % (e,))
        return None
    return found[0] if found else None


_DARK_DONE = set()


def dark_titlebar(hwnd):
    """Paint the native caption bar dark so it matches the UI."""
    if hwnd in _DARK_DONE:
        return
    ok = False
    try:
        h = wt.HWND(hwnd)
        dwm = ctypes.windll.dwmapi
        u = ctypes.windll.user32

        # 1) explicit caption colour (Win11 22H2+): COLORREF is 0x00BBGGRR
        for attr, colour in ((35, 0x00120C0A),      # background #0a0c12
                             (36, 0x00F5EDE9)):     # text       #e9edf5
            v = ctypes.c_int(colour)
            try:
                if dwm.DwmSetWindowAttribute(h, attr, ctypes.byref(v),
                                             ctypes.sizeof(v)) == 0:
                    ok = True
            except Exception:
                pass

        # 2) immersive dark mode flag (Win10 20H1+)
        v2 = ctypes.c_int(1)
        for attr in (20, 19):
            try:
                if dwm.DwmSetWindowAttribute(h, attr, ctypes.byref(v2),
                                             ctypes.sizeof(v2)) == 0:
                    ok = True
                    break
            except Exception:
                pass

        if ok:
            u.SetWindowPos(h, None, 0, 0, 0, 0,
                           0x0002 | 0x0001 | 0x0004 | 0x0010)
            u.RedrawWindow(h, None, None, 0x0001 | 0x0080 | 0x0400)
            _DARK_DONE.add(hwnd)
            _log("dark titlebar ok")
    except Exception as e:
        _log("dark titlebar failed: %r" % (e,))


# --------------------------------------------------------------- main

def main():
    _setup_log()
    _set_app_id()
    _enable_dpi()

    if not _single_instance():
        try:
            ctypes.windll.user32.MessageBoxW(
                None, "抓取直播伴侣画面 已经在运行了。", "WebcastMate Capture", 0x40)
        except Exception:
            pass
        return 0

    httpd, port = server.serve(0)
    url = "http://127.0.0.1:%d/" % port
    _log("http server on %s" % url)
    app = server.APP
    engine = app.engine

    if "--start" in sys.argv:
        def _auto():
            time.sleep(1.5)
            engine.start()
        threading.Thread(target=_auto, daemon=True).start()

    def _shutdown():
        try:
            engine.stop(restore_app=True)
        except Exception:
            pass
        try:
            httpd.shutdown()
        except Exception:
            pass

    browser = find_browser()
    _log("browser: %s" % (browser or "none"))

    proc = None
    if browser and "--browser" not in sys.argv:
        proc = launch_app_window(browser, url)
        if proc:
            _log("app window pid=%s" % proc.pid)
    if proc is None:
        _log("fallback: default browser")
        try:
            webbrowser.open(url)
        except Exception:
            pass

    # ---- stay alive as long as the window does -------------------------
    boot_deadline = time.time() + 30
    window_seen = False
    missing_since = None
    last_hit = time.time()
    tick = 0

    while not app.quit_evt.is_set():
        time.sleep(0.5)
        tick += 1

        hwnd = find_ui_window()
        if tick % 8 == 0:
            _log("watch hwnd=%s seen=%s proc=%s hits=%d"
                 % (hwnd, window_seen,
                    proc.poll() if proc is not None else "-",
                    server.LAST_HIT - last_hit))
        if hwnd:
            if not window_seen:
                window_seen = True
                _log("window up hwnd=%s" % hwnd)
            dark_titlebar(hwnd)
            missing_since = None
        elif window_seen:
            # vanished -> give it a moment (page reload keeps the hwnd)
            if missing_since is None:
                missing_since = time.time()
            elif time.time() - missing_since > 4:
                _log("window closed")
                break
        elif proc is not None and proc.poll() is not None:
            _log("browser exited before showing a window")
            break
        elif time.time() > boot_deadline:
            _log("no window after 30s, giving up")
            break

        if server.LAST_HIT > last_hit:
            last_hit = server.LAST_HIT

        # last-resort watchdog: the browser throttles background timers hard,
        # so this only fires when the page is genuinely gone
        if window_seen and time.time() - last_hit > 180:
            _log("ui silent for 180s, exiting")
            break

    _log("shutting down")
    _shutdown()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
