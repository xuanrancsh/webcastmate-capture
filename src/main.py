# -*- coding: utf-8 -*-
"""WebcastMate Capture - entry point.

Boots the local HTTP service, then shows the UI inside a WebView2 window.
If WebView2 is missing the UI falls back to the default browser, so the tool
is never a black box.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server                                    # noqa: E402
from core import is_admin, is_running, find_install, data_dir  # noqa: E402

APP_ID = "WebcastMateCapture.Desktop.1"
MUTEX_NAME = "Global\\WebcastMateCapture_SingleInstance"


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


def main():
    _set_app_id()
    _enable_dpi()

    if not _single_instance():
        try:
            ctypes.windll.user32.MessageBoxW(
                None, "抓取直播伴侣画面 已在运行中。", "WebcastMate Capture", 0x40)
        except Exception:
            pass
        return 0

    httpd, port = server.serve(0)
    url = "http://127.0.0.1:%d/" % port
    app = server.APP
    engine = app.engine

    # optional auto-start: --start
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

    try:
        import webview
    except Exception:
        webview = None

    if webview is not None:
        try:
            window = webview.create_window(
                "抓取直播伴侣画面",
                url,
                width=1060, height=700,
                min_size=(880, 600),
                frameless=True,
                easy_drag=True,
                background_color="#0a0c12",
                confirm_close=False,
            )

            def watcher():
                while not app.quit_evt.is_set():
                    time.sleep(0.2)
                try:
                    window.destroy()
                except Exception:
                    pass

            threading.Thread(target=watcher, daemon=True).start()

            def _close():
                app.quit_evt.set()
                return {"ok": True}

            def _minimize():
                try:
                    window.minimize()
                except Exception:
                    pass
                return {"ok": True}

            def _browse():
                try:
                    res = window.create_file_dialog(webview.FOLDER_DIALOG)
                except Exception as ex:
                    return {"ok": False, "message": "无法打开对话框: %s" % ex}
                if not res:
                    return {"ok": False, "message": "已取消"}
                folder = res[0] if isinstance(res, (list, tuple)) else res
                cand = os.path.join(folder, "resources", "app", "index.js")
                if not os.path.isfile(cand):
                    found = None
                    try:
                        for name in os.listdir(folder):
                            p = os.path.join(folder, name, "resources", "app",
                                             "index.js")
                            if os.path.isfile(p):
                                found = p
                                break
                    except OSError:
                        pass
                    cand = found
                if not cand or not os.path.isfile(cand):
                    return {"ok": False,
                            "message": "该目录下没有 resources\\app\\index.js"}
                server.save_settings({"index_js": cand})
                engine.index_js = cand
                return {"ok": True, "index_js": cand}

            server.WIN_CTL.update(close=_close, minimize=_minimize, browse=_browse)

            webview.start(gui="edgechromium", private_mode=False,
                          storage_path=os.path.join(data_dir(), "webview"))
            _shutdown()
            return 0
        except Exception as e:
            print("webview failed: %s" % e, flush=True)

    # ---- fallback: default browser ----
    webbrowser.open(url)
    while not app.quit_evt.is_set():
        time.sleep(0.3)
    _shutdown()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
