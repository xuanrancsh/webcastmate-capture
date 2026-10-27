# -*- coding: utf-8 -*-
"""WebcastMate Capture - entry point.

Boots a local HTTP service, then opens the UI in the system browser's
"app mode" (Edge / Chrome ``--app=``).  Nothing is embedded, so there is no
renderer that can crash: if the browser exists, the UI shows up.

Two rules learned from real failures:

1. ``msedge.exe`` is a stub.  It forks the real browser process and exits, so
   its exit status says nothing about the UI.  We therefore watch the window
   only - never the launcher process.
2. A process started from an elevated app inherits administrator rights, and
   Chromium will not carry its sandbox that way.  The browser is started with
   the shell's unelevated token instead.

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
# "Local\\" (per session) - a normal user cannot create objects in the Global
# namespace, which used to make the app claim it was already running.
MUTEX_NAME = "Local\\WebcastMateCapture_SingleInstance"
UI_TITLE = "抓取直播伴侣画面"

WIN_W, WIN_H = 1080, 720

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_adv = ctypes.WinDLL("advapi32", use_last_error=True)
_u32 = ctypes.windll.user32


def _set_app_id():
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def _single_instance():
    """Return a truthy handle if we are the first instance, else None."""
    try:
        h = _k32.CreateMutexW(None, False, MUTEX_NAME)
        err = ctypes.get_last_error()
        if h and err == 183:              # ERROR_ALREADY_EXISTS
            return None
        return h or True                  # cannot tell -> let it run
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


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wt.DWORD),
        ("lpReserved", wt.LPWSTR),
        ("lpDesktop", wt.LPWSTR),
        ("lpTitle", wt.LPWSTR),
        ("dwX", wt.DWORD), ("dwY", wt.DWORD),
        ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
        ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD),
        ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD),
        ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wt.HANDLE),
        ("hStdOutput", wt.HANDLE),
        ("hStdError", wt.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wt.HANDLE),
        ("hThread", wt.HANDLE),
        ("dwProcessId", wt.DWORD),
        ("dwThreadId", wt.DWORD),
    ]


def _spawn_unelevated(exe, args):
    """Start *exe* with the desktop shell's (non-elevated) token.

    Returns the new pid, or None when the trick is not possible.  The caller
    falls back to a normal spawn in that case.
    """
    if not is_admin():
        return None                      # already unelevated

    hproc = tok = primary = None
    try:
        hwnd = _u32.GetShellWindow()
        if not hwnd:
            _log("unelevated: no shell window")
            return None
        pid = wt.DWORD()
        _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return None

        hproc = _k32.OpenProcess(0x1000, False, pid.value)   # QUERY_LIMITED
        if not hproc:
            _log("unelevated: OpenProcess err=%s" % ctypes.get_last_error())
            return None

        tok = wt.HANDLE()
        if not _adv.OpenProcessToken(hproc, 0x0002 | 0x0008,
                                     ctypes.byref(tok)):     # DUPLICATE|QUERY
            _log("unelevated: OpenProcessToken err=%s" % ctypes.get_last_error())
            return None

        primary = wt.HANDLE()
        if not _adv.DuplicateTokenEx(tok, 0x02000000, None, 2, 1,
                                     ctypes.byref(primary)):  # MAX_ALLOWED
            _log("unelevated: DuplicateTokenEx err=%s" % ctypes.get_last_error())
            return None

        si = _STARTUPINFOW()
        si.cb = ctypes.sizeof(si)
        pi = _PROCESS_INFORMATION()
        cmd = ctypes.create_unicode_buffer(subprocess.list2cmdline([exe] + args))

        ok = _adv.CreateProcessWithTokenW(
            primary,
            0x00000001,                  # LOGON_WITH_PROFILE
            exe,
            cmd,
            0x00000400,                  # CREATE_UNICODE_ENVIRONMENT
            None,
            None,
            ctypes.byref(si),
            ctypes.byref(pi),
        )
        if not ok:
            _log("unelevated: CreateProcessWithTokenW err=%s"
                 % ctypes.get_last_error())
            return None

        new_pid = int(pi.dwProcessId)
        for h in (pi.hThread, pi.hProcess):
            if h:
                _k32.CloseHandle(h)
        return new_pid
    except Exception as e:
        _log("unelevated failed: %r" % (e,))
        return None
    finally:
        for h in (primary, tok, hproc):
            try:
                if h:
                    _k32.CloseHandle(h)
            except Exception:
                pass


def _spawn_detached(exe, args):
    try:
        kw = {"close_fds": True}
        if os.name == "nt":
            kw["creationflags"] = 0x00000008          # DETACHED_PROCESS
        p = subprocess.Popen([exe] + args, **kw)
        return int(p.pid)
    except Exception as e:
        _log("launch failed: %r" % (e,))
        return None


def launch_app_window(exe, url):
    """Open *url* as a standalone app window.  Returns the pid or None."""
    args = [
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
    if is_admin():
        pid = _spawn_unelevated(exe, args)
        if pid:
            _log("launched with shell token pid=%s" % pid)
            return pid
        _log("shell token unavailable, spawning as-is")
    return _spawn_detached(exe, args)


_ENUM_CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)


def find_ui_window():
    """Return the HWND of our app window, or None."""
    u = _u32
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
            _u32.SetWindowPos(h, None, 0, 0, 0, 0,
                              0x0002 | 0x0001 | 0x0004 | 0x0010)
            _u32.RedrawWindow(h, None, None, 0x0001 | 0x0080 | 0x0400)
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

    pid = None
    if browser and "--browser" not in sys.argv:
        pid = launch_app_window(browser, url)
        _log("app window pid=%s" % pid)
    if pid is None:
        _log("fallback: default browser")
        try:
            webbrowser.open(url)
        except Exception:
            pass

    # ---- stay alive as long as the window does -------------------------
    # NOTE: never judge by the launcher process.  msedge.exe exits right
    # after handing off to the real browser, which is not a failure.
    boot_deadline = time.time() + 40
    retried = False
    window_seen = False
    missing_since = None
    last_hit = time.time()
    tick = 0

    while not app.quit_evt.is_set():
        time.sleep(0.5)
        tick += 1

        hwnd = find_ui_window()
        if tick % 8 == 0:
            _log("watch hwnd=%s seen=%s pid=%s"
                 % (hwnd, window_seen, pid))
        if hwnd:
            if not window_seen:
                window_seen = True
                _log("window up hwnd=%s" % hwnd)
            dark_titlebar(hwnd)
            missing_since = None
        elif window_seen:
            # vanished -> give it a moment (a reload keeps the same hwnd)
            if missing_since is None:
                missing_since = time.time()
            elif time.time() - missing_since > 4:
                _log("window closed")
                break
        elif time.time() > boot_deadline:
            if not retried:
                retried = True
                _log("no window yet, retry with default browser")
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
                boot_deadline = time.time() + 25
            else:
                _log("still no window, giving up")
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
