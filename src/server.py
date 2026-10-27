# -*- coding: utf-8 -*-
"""Local HTTP server: static UI + JSON API + MJPEG preview stream.

Everything is bound to 127.0.0.1 with a random port, so no firewall prompt
and no exposure to the LAN.
"""
from __future__ import annotations

import io
import json
import mimetypes
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from core import Engine, hook_config, web_dir, write_hook_config, \
    find_install, deploy_hook, save_settings, load_settings, is_admin, is_running

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


class App:
    def __init__(self):
        self.engine = Engine()
        self.quit_evt = threading.Event()
        self.preview = {"max_w": 1280, "quality": 72, "fps": 25}
        self._load_prefs()

    def _load_prefs(self):
        s = load_settings().get("ui") or {}
        for k in ("max_w", "quality", "fps"):
            if k in s:
                try:
                    self.preview[k] = int(s[k])
                except Exception:
                    pass

    def save_prefs(self, d):
        for k in ("max_w", "quality", "fps"):
            if k in d:
                v = int(d[k])
                v = max(1, min(120, v)) if k == "fps" else max(1, min(3840, v))
                self.preview[k] = v
        save_settings({"ui": dict(self.preview)})


APP: App | None = None
WIN_CTL: dict = {}          # {"minimize": fn, "close": fn, "browse": fn}


def pictures_dir() -> str:
    base = os.path.join(os.path.expanduser("~"), "Pictures")
    if not os.path.isdir(base):
        base = os.path.expanduser("~")
    d = os.path.join(base, "WebcastMate Capture")
    os.makedirs(d, exist_ok=True)
    return d


def jpeg(frame, max_w, quality):
    from PIL import Image
    img = Image.fromarray(frame[:, :, ::-1])          # BGR -> RGB
    if max_w and img.width > max_w:
        r = max_w / float(img.width)
        img = img.resize((max_w, max(2, int(img.height * r))), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=False,
             subsampling=2)
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "WebcastMateCapture"

    # ---------------------------------------------------------------- helpers
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _bytes(self, data: bytes, ctype: str, filename: str | None = None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if filename:
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % filename)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path: str):
        if not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "rb") as f:
            data = f.read()
        ext = os.path.splitext(path)[1].lower()
        ctype = MIME.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    # ---------------------------------------------------------------- routes
    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        try:
            if p in ("/", "/index.html"):
                return self._file(os.path.join(web_dir(), "index.html"))
            if p.startswith("/static/"):
                rel = p[len("/static/"):].replace("..", "")
                return self._file(os.path.join(web_dir(), rel))
            if p == "/favicon.ico":
                return self._file(os.path.join(web_dir(), "favicon.svg"))
            if p == "/api/state":
                e = APP.engine
                e.refresh_runtime()
                st = e.snapshot_state()
                if not st.get("index_js"):
                    st["index_js"] = load_settings().get("index_js")
                st["preview"] = dict(APP.preview)
                st["version"] = VERSION
                return self._json(st)
            if p == "/api/logs":
                q = parse_qs(u.query)
                tail = int((q.get("tail") or ["160"])[0])
                return self._json({"logs": APP.engine.get_logs(tail)})
            if p == "/api/stream":
                return self._stream()
            if p == "/api/settings":
                s = load_settings()
                s.pop("index_js", None)
                return self._json({"settings": s, "hook_config": hook_config(),
                                   "preview": dict(APP.preview)})
            self.send_error(404)
        except BrokenPipeError:
            pass
        except Exception:
            try:
                self._json({"error": traceback.format_exc()[-600:]}, 500)
            except Exception:
                pass

    def do_POST(self):
        p = urlparse(self.path).path
        body = self._read_json()
        e = APP.engine
        try:
            if p == "/api/start":
                ok, msg = e.start()
                return self._json({"ok": ok, "message": msg})
            if p == "/api/stop":
                ok, msg = e.stop(restore_app=body.get("restore", True))
                return self._json({"ok": ok, "message": msg})
            if p == "/api/snapshot":
                data, err = e.snapshot_png()
                if not data:
                    return self._json({"ok": False, "message": err or "无画面"}, 409)
                name = time.strftime("capture-%Y%m%d-%H%M%S.png")
                path = os.path.join(pictures_dir(), name)
                try:
                    with open(path, "wb") as f:
                        f.write(data)
                except OSError as ex:
                    return self._json({"ok": False, "message": "保存失败: %s" % ex}, 500)
                return self._json({"ok": True, "path": path})
            if p == "/api/win":
                act = body.get("action") or ""
                fn = WIN_CTL.get(act)
                if not fn:
                    return self._json({"ok": False, "message": "不支持的操作"}, 400)
                return self._json(fn() or {"ok": True})
            if p == "/api/locate":
                idx, src = find_install(deep=bool(body.get("deep", True)))
                if not idx:
                    return self._json({"ok": False, "message": "未找到直播伴侣"})
                save_settings({"index_js": idx})
                e.index_js = idx
                return self._json({"ok": True, "index_js": idx, "source": src})
            if p == "/api/set-path":
                path = (body.get("index_js") or "").strip().strip('"')
                if path and not path.lower().endswith("index.js"):
                    cand = os.path.join(path, "resources", "app", "index.js")
                    if os.path.isfile(cand):
                        path = cand
                if not os.path.isfile(path):
                    return self._json({"ok": False, "message": "路径无效：%s" % path}, 400)
                save_settings({"index_js": path})
                e.index_js = path
                return self._json({"ok": True, "index_js": path})
            if p == "/api/inject":
                from core import inject, deploy_hook
                deploy_hook()
                idx = e.index_js or find_install()[0]
                if not idx:
                    return self._json({"ok": False, "message": "未找到安装目录"}, 400)
                if is_running():
                    from core import kill_app
                    kill_app()
                    time.sleep(0.8)
                ok, msg = inject(idx)
                e.index_js = idx
                return self._json({"ok": ok, "message": msg})
            if p == "/api/restore":
                from core import restore, kill_app
                idx = e.index_js or find_install()[0]
                if not idx:
                    return self._json({"ok": False, "message": "未找到安装目录"}, 400)
                if is_running():
                    kill_app()
                    time.sleep(0.8)
                ok, msg = restore(idx)
                return self._json({"ok": ok, "message": msg})
            if p == "/api/settings":
                APP.save_prefs(body.get("preview") or {})
                hc = body.get("hook_config") or {}
                if hc:
                    write_hook_config(hc)
                return self._json({"ok": True, "preview": dict(APP.preview)})
            if p == "/api/open-folder":
                what = body.get("what") or "hook"
                from core import data_dir
                target = data_dir() if what == "data" else os.path.join(
                    data_dir(), "hook")
                try:
                    os.startfile(target)
                except Exception:
                    pass
                return self._json({"ok": True, "path": target})
            if p == "/api/open-url":
                url = body.get("url") or ""
                if not url.startswith(("http://", "https://")):
                    return self._json({"ok": False, "message": "仅支持 http(s) 链接"}, 400)
                import webbrowser
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
                return self._json({"ok": True})
            if p == "/api/quit":
                self._json({"ok": True})
                APP.quit_evt.set()
                return
            self.send_error(404)
        except BrokenPipeError:
            pass
        except Exception:
            try:
                self._json({"ok": False, "message": traceback.format_exc()[-600:]}, 500)
            except Exception:
                pass

    # ---------------------------------------------------------------- mjpeg
    def _stream(self):
        e = APP.engine
        cfg = APP.preview
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        last_n = -1
        interval = 1.0 / max(1, min(60, cfg["fps"]))
        try:
            idle = 0
            while not APP.quit_evt.is_set():
                frame, n = e.latest_frame(last_n)
                if frame is None:
                    idle += 1
                    if idle > 600:          # ~30s of nothing -> drop the stream
                        break
                    time.sleep(0.05)
                    continue
                idle = 0
                last_n = n
                try:
                    data = jpeg(frame, cfg["max_w"], cfg["quality"])
                except Exception:
                    time.sleep(0.1)
                    continue
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(("Content-Length: %d\r\n\r\n" % len(data)).encode())
                self.wfile.write(data)
                self.wfile.write(b"\r\n")
                e.on_frame_served()
                time.sleep(interval)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


VERSION = "1.0.0"


def serve(port: int = 0):
    global APP
    APP = App()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, httpd.server_address[1]
