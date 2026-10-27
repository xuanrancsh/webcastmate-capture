# -*- coding: utf-8 -*-
"""WebcastMate Capture - core engine.

Design goals (portability first):
  * no hard-coded drive letters / install paths
  * hook lives in an ASCII-only directory so `require()` never sees CJK chars
  * injection is wrapped in try/catch, so a missing hook can NEVER break the host app
  * restore works even if the .bak file was lost (strip the injected first line)
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time

# --------------------------------------------------------------------------- const
APP_NAME = "WebcastMateCapture"
LEGACY_APP_NAME = "ZblbCap"
INJECT_MARK = "WebcastMateCapture-hook"
LEGACY_MARK = "ZblbCap-hook"
HOOK_TITLE = "wmc_capture"
OFFSCREEN = -32000

EXE_CANDIDATES = ["直播伴侣.exe", "WebcastMate.exe", "直播伴侣 Launcher.exe"]
HELPER_EXES = ["MediaSDK_Server.exe"]

SKIP_DIRS = {
    "windows", "windows.old", "$recycle.bin", "system volume information",
    "programdata", "appdata", "node_modules", ".git", "perflogs", "recovery",
    "windowsapps", "onedrivetemp", "temp",
}

_user32 = ctypes.windll.user32
_dwmapi = ctypes.windll.dwmapi
_kernel32 = ctypes.windll.kernel32
try:
    _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))   # PER_MONITOR_AWARE_V2
except Exception:
    try:
        _user32.SetProcessDPIAware()
    except Exception:
        pass


# --------------------------------------------------------------------------- paths
def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> str:
    """Directory holding bundled assets (hook_main.js / web/)."""
    if _frozen():
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        if os.path.isfile(os.path.join(base, "hook_main.js")):
            return base
        if os.path.isfile(os.path.join(base, "assets", "hook_main.js")):
            return os.path.join(base, "assets")
        return base
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, "assets"), here):
        if os.path.isfile(os.path.join(cand, "hook_main.js")):
            return cand
    return os.path.join(here, "assets")


def web_dir() -> str:
    return os.path.join(resource_dir(), "web")


def _writable(d: str) -> bool:
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".w")
        with open(probe, "w") as f:
            f.write("1")
        os.remove(probe)
        return True
    except Exception:
        return False


def data_dir() -> str:
    """Config / logs. ASCII-first so downstream tooling stays happy."""
    for base in (os.environ.get("PROGRAMDATA"), None):
        if base:
            d = os.path.join(base, APP_NAME)
            if _writable(d):
                return d
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_NAME)
    os.makedirs(d, exist_ok=True)
    return d


_hook_dir_cache = None


def hook_dir() -> str:
    global _hook_dir_cache
    env = os.environ.get("WMC_HOOK_DIR")
    if env:
        os.makedirs(env, exist_ok=True)
        return env
    if _hook_dir_cache:
        return _hook_dir_cache
    for base in (os.environ.get("PROGRAMDATA"), os.environ.get("LOCALAPPDATA"),
                 os.path.expanduser("~")):
        if not base:
            continue
        d = os.path.join(base, APP_NAME, "hook")
        if _writable(d):
            _hook_dir_cache = d
            return d
    d = os.path.join(tempfile.gettempdir(), APP_NAME, "hook")
    os.makedirs(d, exist_ok=True)
    _hook_dir_cache = d
    return d


SETTINGS_FILE = lambda: os.path.join(data_dir(), "settings.json")


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(updates: dict) -> dict:
    cur = load_settings()
    cur.update(updates or {})
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE()), exist_ok=True)
        with open(SETTINGS_FILE(), "w", encoding="utf-8") as f:
            json.dump(cur, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return cur


DEFAULT_HOOK_CFG = {
    "auto_open": True,
    "width": 1920,
    "height": 1080,
    "title": HOOK_TITLE,
    "offscreen": OFFSCREEN,
    "border_w": 18,
    "border_h": 47,
    "fps_cap": 60,
}


def hook_config() -> dict:
    cfg = dict(DEFAULT_HOOK_CFG)
    try:
        with open(os.path.join(hook_dir(), "config.json"), "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def write_hook_config(updates: dict) -> dict:
    cfg = hook_config()
    cfg.update(updates or {})
    try:
        with open(os.path.join(hook_dir(), "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return cfg


def deploy_hook(force: bool = False) -> str:
    """Copy bundled hook into hook_dir. Returns hook_dir."""
    d = hook_dir()
    src = os.path.join(resource_dir(), "hook_main.js")
    dst = os.path.join(d, "hook_main.js")
    if os.path.isfile(src):
        need = force or not os.path.isfile(dst)
        if not need:
            try:
                need = os.path.getsize(src) != os.path.getsize(dst) or \
                    _sha(src) != _sha(dst)
            except Exception:
                need = True
        if need:
            shutil.copyfile(src, dst)
    if not os.path.isfile(os.path.join(d, "config.json")):
        write_hook_config({})
    return d


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- process
class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)), ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


def list_processes(name: str | None = None):
    TH32CS_SNAPPROCESS = 0x00000002
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    out = []
    if snap in (-1, None, 0):
        return out
    try:
        pe = _PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        ok = _kernel32.Process32First(snap, ctypes.byref(pe))
        while ok:
            nm = pe.szExeFile.decode("utf-8", "ignore")
            if not nm:
                nm = pe.szExeFile.decode("gbk", "ignore")
            if name is None or nm.lower() == name.lower():
                out.append((pe.th32ProcessID, nm))
            ok = _kernel32.Process32Next(snap, ctypes.byref(pe))
    finally:
        _kernel32.CloseHandle(snap)
    return out


def process_path(pid: int):
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(2048)
        size = wt.DWORD(2048)
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
    finally:
        _kernel32.CloseHandle(h)
    return None


def is_running() -> list[int]:
    pids = []
    for nm in EXE_CANDIDATES:
        pids += [p for p, _ in list_processes(nm)]
    return pids


def kill_app() -> bool:
    """Terminate every host-app process; returns True when none is left."""
    PROCS = []
    for nm in EXE_CANDIDATES:
        PROCS += [p for p, _ in list_processes(nm)]
    PROCESS_TERMINATE = 0x0001
    for pid in PROCS:
        h = _kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if h:
            try:
                _kernel32.TerminateProcess(h, 1)
            finally:
                _kernel32.CloseHandle(h)
    for _ in range(20):
        if not is_running():
            return True
        time.sleep(0.25)
    return not is_running()


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def launch(exe: str | None = None):
    exe = exe or find_exe()
    if not exe or not os.path.isfile(exe):
        return False, "找不到 直播伴侣.exe，请在设置里手动指定安装目录"
    cwd = os.path.dirname(exe)
    if is_admin():
        try:
            DETACHED, NEWGRP = 0x00000008, 0x00000200
            import subprocess
            subprocess.Popen([exe], cwd=cwd, close_fds=True,
                             creationflags=DETACHED | NEWGRP)
            return True, "已启动"
        except Exception as e:
            return False, "启动失败: %s" % e
    try:
        r = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, None, cwd, 1)
    except Exception as e:
        return False, "启动失败: %s" % e
    if not r or r <= 32:
        return False, "提权被拒绝 (code=%s)" % r
    return True, "已请求管理员权限启动"


# --------------------------------------------------------------------------- locate
def index_from_exe(exe: str | None):
    if not exe:
        return None
    p = os.path.join(os.path.dirname(exe), "resources", "app", "index.js")
    return p if os.path.isfile(p) else None


def _registry_roots() -> list[str]:
    """Install roots from uninstall registry keys (HKLM/HKCU, 32/64 bit)."""
    roots = []
    try:
        import winreg
    except Exception:
        return roots
    sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    views = [winreg.KEY_READ]
    try:
        views.append(winreg.KEY_READ | winreg.KEY_WOW64_32KEY)
        views.append(winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
    except Exception:
        pass
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in views:
            try:
                with winreg.OpenKey(hive, sub, 0, view) as k:
                    n = winreg.QueryInfoKey(k)[0]
                    for i in range(n):
                        try:
                            name = winreg.EnumKey(k, i)
                            with winreg.OpenKey(k, name) as sk:
                                def q(v):
                                    try:
                                        return winreg.QueryValueEx(sk, v)[0]
                                    except Exception:
                                        return None
                                dn = q("DisplayName") or ""
                                if not ("直播伴侣" in dn or "webcast" in dn.lower()
                                        or "伴侣" in dn):
                                    continue
                                loc = q("InstallLocation") or ""
                                icon = q("DisplayIcon") or ""
                                for cand in (loc, os.path.dirname(icon.strip('"'))):
                                    cand = (cand or "").strip().strip('"')
                                    if cand and os.path.isdir(cand):
                                        roots.append(cand)
                        except Exception:
                            continue
            except Exception:
                continue
    return roots


def _common_roots() -> list[str]:
    roots = []
    names = ["webcast_mate", "WebcastMate", "webcastmate", "直播伴侣", "WebcastMateApp"]
    for drive in "CDEFGHIJ":
        for nm in names:
            roots.append("%s:\\%s" % (drive, nm))
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
                 os.environ.get("LOCALAPPDATA")):
        if not base:
            continue
        for nm in names:
            roots.append(os.path.join(base, nm))
    return [r for r in roots if r]


def _drives() -> list[str]:
    try:
        mask = _kernel32.GetLogicalDrives()
    except Exception:
        return []
    DRIVE_FIXED = 3
    out = []
    for i in range(26):
        if mask & (1 << i):
            root = "%s:\\" % chr(ord("A") + i)
            try:
                if _kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) == DRIVE_FIXED:
                    out.append(root)
            except Exception:
                pass
    return out


def _scan_for_index(root: str, max_depth: int = 4):
    """Breadth-first search for resources/app/index.js under root."""
    root = root.rstrip("\\/")
    if not os.path.isdir(root):
        return None
    direct = os.path.join(root, "resources", "app", "index.js")
    if os.path.isfile(direct):
        return direct
    frontier = [(root, 0)]
    seen = 0
    while frontier:
        nxt = []
        for base, depth in frontier:
            if depth >= max_depth:
                continue
            try:
                with os.scandir(base) as it:
                    for e in it:
                        seen += 1
                        if seen > 60000:
                            return None
                        try:
                            if not e.is_dir(follow_symlinks=False):
                                continue
                        except OSError:
                            continue
                        if e.name.lower() in SKIP_DIRS:
                            continue
                        p = os.path.join(e.path, "resources", "app", "index.js")
                        if os.path.isfile(p):
                            return p
                        nxt.append((e.path, depth + 1))
            except OSError:
                continue
        frontier = nxt
    return None


def find_install(deep: bool = True, on_progress=None):
    """Locate resources/app/index.js. Returns (path, source)."""
    s = load_settings()
    saved = s.get("index_js")
    if saved and os.path.isfile(saved):
        return saved, "saved"

    for nm in EXE_CANDIDATES:
        for pid, _ in list_processes(nm):
            p = index_from_exe(process_path(pid))
            if p:
                save_settings({"index_js": p})
                return p, "process"

    seen = set()
    for r in _registry_roots() + _common_roots():
        key = os.path.normcase(os.path.abspath(r))
        if key in seen:
            continue
        seen.add(key)
        if on_progress:
            on_progress(r)
        p = _scan_for_index(r, max_depth=2)
        if p:
            save_settings({"index_js": p})
            return p, "known-dir"

    if deep:
        for drv in _drives():
            if on_progress:
                on_progress("%s  (*)" % drv)
            p = _scan_for_index(drv, max_depth=4)
            if p:
                save_settings({"index_js": p})
                return p, "scan"
    return None, "missing"


def find_exe(index_js: str | None = None):
    for nm in EXE_CANDIDATES:
        for pid, _ in list_processes(nm):
            p = process_path(pid)
            if p and os.path.isfile(p):
                return p
    index_js = index_js or find_install()[0]
    if not index_js:
        return None
    base = os.path.dirname(os.path.dirname(os.path.dirname(index_js)))
    for nm in EXE_CANDIDATES:
        cand = os.path.join(base, nm)
        if os.path.isfile(cand):
            return cand
    return None


# --------------------------------------------------------------------------- inject
def _read_bytes(p):
    with open(p, "rb") as f:
        return f.read()


def _first_line_end(data: bytes) -> int:
    i = data.find(b"\r\n")
    j = data.find(b"\n")
    if i < 0:
        return j
    if j < 0:
        return i
    return min(i, j)


def _is_inject_line(line: str) -> bool:
    return INJECT_MARK in line or LEGACY_MARK in line


def injection_state(index_js: str | None) -> str:
    """clean | injected | injected-legacy | missing | unreadable"""
    if not index_js or not os.path.isfile(index_js):
        return "missing"
    try:
        head = _read_bytes(index_js)[:800]
    except OSError:
        return "unreadable"
    first = head.decode("utf-8", "ignore").split("\n")[0]
    if INJECT_MARK in first:
        return "injected"
    if LEGACY_MARK in first:
        return "injected-legacy"
    return "clean"


def _strip_inject(index_bytes: bytes) -> bytes:
    i = _first_line_end(index_bytes)
    if i < 0:
        return index_bytes
    first = index_bytes[:i].decode("utf-8", "ignore")
    if _is_inject_line(first):
        j = i
        while j < len(index_bytes) and index_bytes[j:j + 1] in (b"\r", b"\n"):
            j += 1
        return index_bytes[j:]
    return index_bytes


def _inject_line(hook_js: str) -> str:
    p = hook_js.replace("\\", "/")
    return ('try{require("%s")}catch(e){}; // %s\n' % (p, INJECT_MARK))


def restore(index_js: str):
    """Restore original index.js. Works without .bak too. Returns (ok, msg)."""
    if not index_js or not os.path.isfile(index_js):
        return False, "找不到主进程入口文件"
    bak = index_js + ".bak"
    try:
        cur = _read_bytes(index_js)
    except OSError as e:
        return False, "读取失败: %s" % e

    if not _is_inject_line(cur[:800].decode("utf-8", "ignore").split("\n")[0]):
        if os.path.isfile(bak):
            try:
                if not _is_inject_line(_read_bytes(bak)[:800].decode("utf-8", "ignore").split("\n")[0]):
                    pass
            except OSError:
                pass
        return True, "未注入，无需还原"

    clean = _strip_inject(cur)
    if len(clean) < 20:
        if os.path.isfile(bak):
            clean = _read_bytes(bak)
        else:
            return False, "无法还原：注入行之后没有内容，且没有备份"
    try:
        with open(index_js, "wb") as f:
            f.write(clean)
    except PermissionError as e:
        return False, "写入被拒绝（直播伴侣可能正在运行）: %s" % e
    try:
        if os.path.isfile(bak) and _sha(bak) == _sha(index_js):
            os.remove(bak)
    except Exception:
        pass
    return True, "已还原"


def inject(index_js: str, hook_js: str | None = None):
    """Backup + prepend require line. Returns (ok, msg)."""
    hook_js = hook_js or os.path.join(deploy_hook(), "hook_main.js")
    if not os.path.isfile(hook_js):
        return False, "钩子文件缺失: %s" % hook_js
    st = injection_state(index_js)
    if st == "missing":
        return False, "找不到主进程入口: %s" % index_js
    if st == "unreadable":
        return False, "无法读取入口文件（权限不足？）"
    if st == "injected":
        cur = _read_bytes(index_js)
        first = cur[:800].decode("utf-8", "ignore").split("\n")[0]
        if hook_js.replace("\\", "/") in first.replace("\\\\", "/"):
            return True, "已注入"
        # path changed (upgrade / different user) -> rewrite first line
        try:
            with open(index_js, "wb") as f:
                f.write(_inject_line(hook_js).encode("utf-8") + _strip_inject(cur))
            return True, "已更新注入路径"
        except PermissionError as e:
            return False, "写入被拒绝: %s" % e

    data = _read_bytes(index_js)
    clean = _strip_inject(data)
    bak = index_js + ".bak"
    need_bak = True
    if os.path.isfile(bak):
        try:
            b = _read_bytes(bak)
            first_b = b[:800].decode("utf-8", "ignore").split("\n")[0]
            if not _is_inject_line(first_b) and b == clean:
                need_bak = False
        except OSError:
            pass
    if need_bak:
        try:
            with open(bak, "wb") as f:
                f.write(clean)
        except PermissionError as e:
            return False, "无法创建备份: %s" % e

    line = _inject_line(hook_js).encode("utf-8")
    try:
        with open(index_js, "wb") as f:
            f.write(line + clean)
    except PermissionError as e:
        return False, "写入被拒绝（请先关闭直播伴侣）: %s" % e
    return True, "注入成功"


# --------------------------------------------------------------------------- channel
def _res_file():
    return os.path.join(hook_dir(), "res.txt")


def _cmd_file():
    return os.path.join(hook_dir(), "cmd.txt")


def send_cmd(obj: dict, timeout: float = 8.0):
    """Send a command to the in-process hook and wait for its reply."""
    d = hook_dir()
    cmd_f, res_f = _cmd_file(), _res_file()
    payload = dict(obj)
    payload["_n"] = "%.6f" % time.time()
    before = 0
    try:
        before = os.path.getsize(res_f)
    except OSError:
        before = 0
    try:
        tmp = cmd_f + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False))
        os.replace(tmp, cmd_f)
    except OSError as e:
        return {"error": "写命令失败: %s" % e}

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.12)
        try:
            with open(res_f, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(before)
                chunk = f.read()
        except OSError:
            continue
        for ln in reversed([x for x in chunk.splitlines() if x.strip()]):
            try:
                o = json.loads(ln)
            except Exception:
                continue
            if isinstance(o, dict) and o.get("_n") in (None, payload["_n"]):
                return o
            if isinstance(o, dict):
                return o
    return None


def read_log(tail: int = 80):
    p = os.path.join(hook_dir(), "hook.log")
    if not os.path.isfile(p):
        return []
    try:
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip().splitlines()[-tail:]
    except OSError:
        return []


def clear_log():
    p = os.path.join(hook_dir(), "hook.log")
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("")
    except OSError:
        pass


# --------------------------------------------------------------------------- window
DWMWA_EXTENDED_FRAME_BOUNDS = 9


def _app_pids():
    pids = set()
    for nm in EXE_CANDIDATES + HELPER_EXES:
        pids |= set(p for p, _ in list_processes(nm))
    return pids


def find_projector(title: str = HOOK_TITLE):
    pids = _app_pids()
    hits = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def cb(h, _):
        pid = wt.DWORD()
        _user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        if pids and pid.value not in pids:
            return True
        buf = ctypes.create_unicode_buffer(512)
        _user32.GetWindowTextW(h, buf, 512)
        if buf.value == title:
            hits.append(int(h))
        return True

    _user32.EnumWindows(cb, 0)
    return hits[0] if hits else None


def client_box(hwnd: int):
    h = wt.HWND(hwnd)
    ext = wt.RECT()
    _dwmapi.DwmGetWindowAttribute(h, DWMWA_EXTENDED_FRAME_BOUNDS,
                                  ctypes.byref(ext), ctypes.sizeof(ext))
    cr = wt.RECT()
    _user32.GetClientRect(h, ctypes.byref(cr))
    pt = wt.POINT(0, 0)
    _user32.ClientToScreen(h, ctypes.byref(pt))
    return cr.right, cr.bottom, pt.x - ext.left, pt.y - ext.top


def window_rect(hwnd: int):
    r = wt.RECT()
    _user32.GetWindowRect(wt.HWND(hwnd), ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def is_window(hwnd: int) -> bool:
    return bool(_user32.IsWindow(wt.HWND(hwnd)))


def screen_limit():
    try:
        gw, gh = _user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1)
    except Exception:
        gw, gh = 1920, 1080
    return max(320, gw - 80), max(240, gh - 120)


def fit_size(cw: int, ch: int, max_w=None, max_h=None):
    lw, lh = screen_limit()
    if max_w and max_h:
        lw, lh = max_w, max_h
    scale = min(1.0, lw / float(cw or 1), lh / float(ch or 1))
    return (max(2, int(cw * scale) // 2 * 2),
            max(2, int(ch * scale) // 2 * 2), scale)


def detect_canvas():
    r = send_cmd({"op": "detect"}, 6)
    if not r or not r.get("fit"):
        return None
    fit, size = r["fit"], r.get("size") or {}
    return {
        "canvas_w": size.get("w"), "canvas_h": size.get("h"),
        "fps": size.get("fps"), "win_w": fit.get("w"), "win_h": fit.get("h"),
        "scale": fit.get("scale", 1.0),
    }


def set_client_box(hwnd: int, tw: int, th: int, x=OFFSCREEN, y=OFFSCREEN, tries=5):
    for _ in range(tries):
        if not is_window(hwnd):
            return False
        cw, ch, _, _ = client_box(hwnd)
        sx, sy, ow, oh = window_rect(hwnd)
        if (cw, ch) == (tw, th) and (sx, sy) == (x, y):
            return True
        r = send_cmd({"op": "setPosProj", "index": 0, "x": x, "y": y,
                      "cx": tw + (ow - cw), "cy": th + (oh - ch)}, 4)
        if r is None:
            return False
        time.sleep(0.35)
    cw, ch, _, _ = client_box(hwnd)
    return (cw, ch) == (tw, th)


def ensure_projector(title: str = HOOK_TITLE, log=None):
    info = detect_canvas()
    if not info:
        return None, None
    w, h = info["win_w"], info["win_h"]

    hwnd = find_projector(title)
    if hwnd:
        cw, ch, _, _ = client_box(hwnd)
        if (cw, ch) == (w, h):
            sx, sy, _, _ = window_rect(hwnd)
            if (sx, sy) != (OFFSCREEN, OFFSCREEN):
                set_client_box(hwnd, w, h)
            return hwnd, info
        send_cmd({"op": "closeAllProj", "index": 0}, 4)
        time.sleep(0.3)

    if log:
        log("开投影窗 %dx%d (画布 %dx%d, 缩放 %.3f)"
            % (w, h, info["canvas_w"] or 0, info["canvas_h"] or 0, info["scale"]))
    r = send_cmd({"op": "openProj", "title": title, "cx": w, "cy": h,
                  "bOutput": True, "bNolimitSize": True}, 10)
    if not r or not r.get("ok"):
        if log:
            log("Projector.Open 失败: %s" % (r,))
        return None, info
    ok = r["ok"]
    hwnd = int(ok.get("childHwnd") or 0) if isinstance(ok, dict) else 0
    if not hwnd:
        return None, info
    time.sleep(0.9)
    set_client_box(hwnd, w, h)
    return hwnd, info


# --------------------------------------------------------------------------- grabber
class Grabber:
    """WGC window capture with client-area cropping."""

    def __init__(self, hwnd: int, crop: bool = True):
        from windows_capture import Frame, InternalCaptureControl, WindowsCapture
        self.hwnd = hwnd
        self.frame = None
        self.n = 0
        self.closed = False
        self._box = client_box(hwnd) if crop else None
        self.size = (self._box[0], self._box[1]) if self._box else None

        cap = WindowsCapture(cursor_capture=False, draw_border=False, window_hwnd=hwnd)

        @cap.event
        def on_frame_arrived(f: Frame, ctl: InternalCaptureControl):
            img = f.frame_buffer
            if img is None:
                return
            if img.ndim == 3 and img.shape[2] == 4:
                img = img[:, :, :3]
            if self._box:
                cw, ch, ox, oy = self._box
                hh, ww = img.shape[:2]
                if ox >= 0 and oy >= 0 and ox + cw <= ww and oy + ch <= hh:
                    img = img[oy:oy + ch, ox:ox + cw]
            self.frame = img
            self.n += 1

        @cap.event
        def on_closed():
            self.closed = True

        self._cap = cap

    def start(self):
        self._cap.start_free_threaded()
        return self

    def read(self, timeout: float = 3.0):
        t0 = time.time()
        while self.frame is None:
            if self.closed or time.time() - t0 > timeout:
                return False, None
            time.sleep(0.005)
        return True, self.frame

    def stop(self):
        try:
            self._cap.stop()
        except Exception:
            pass


# --------------------------------------------------------------------------- engine
class Engine:
    """State machine driving the whole capture pipeline."""

    IDLE, LOCATING, INJECTING, LAUNCHING, WAITING, READY, ERROR = (
        "idle", "locating", "injecting", "launching", "waiting", "ready", "error")

    def __init__(self):
        self.lock = threading.RLock()
        self.state = self.IDLE
        self.message = "就绪"
        self.progress = 0
        self.index_js = None
        self.exe_path = None
        self.hwnd = None
        self.canvas = (0, 0)
        self.win = (0, 0)
        self.scale = 1.0
        self.source_fps = 0
        self.fps = 0
        self.frames = 0
        self.injected = False
        self.running_app = False

        self._grabber = None
        self._latest = None
        self._latest_n = -1
        self._latest_ts = 0.0
        self._thread = None
        self._stop_evt = threading.Event()
        self._fps_mark = (time.time(), 0)
        self.logs = []
        self._log_lock = threading.Lock()

    # ---------------- log
    def log(self, msg: str):
        line = "%s %s" % (time.strftime("%H:%M:%S"), msg)
        with self._log_lock:
            self.logs.append(line)
            if len(self.logs) > 500:
                del self.logs[:200]
        try:
            print(line, flush=True)
        except Exception:
            pass

    def get_logs(self, tail=200):
        with self._log_lock:
            return self.logs[-tail:]

    def set(self, state=None, message=None, progress=None):
        with self.lock:
            if state is not None:
                self.state = state
            if message is not None:
                self.message = message
                self.log(message)
            if progress is not None:
                self.progress = progress

    # ---------------- snapshot / state
    def snapshot_state(self):
        with self.lock:
            alive = bool(self._grabber and not self._grabber.closed)
            return {
                "state": self.state,
                "message": self.message,
                "progress": self.progress,
                "index_js": self.index_js,
                "exe_path": self.exe_path,
                "hwnd": self.hwnd,
                "canvas_w": self.canvas[0], "canvas_h": self.canvas[1],
                "win_w": self.win[0], "win_h": self.win[1],
                "scale": self.scale,
                "source_fps": self.source_fps,
                "fps": self.fps,
                "frames": self.frames,
                "injected": self.injected,
                "app_running": self.running_app,
                "capturing": alive,
                "hook_dir": hook_dir(),
                "data_dir": data_dir(),
                "admin": is_admin(),
                "aspect": self._aspect(),
            }

    def _aspect(self):
        w, h = self.canvas
        if not w or not h:
            return "-"
        from math import gcd
        g = gcd(w, h) or 1
        return "%d:%d" % (w // g, h // g)

    def latest_frame(self, last_n=-1):
        with self.lock:
            if self._latest is None or self._latest_n == last_n:
                return None, self._latest_n
            return self._latest, self._latest_n

    def grab_frame(self):
        with self.lock:
            return self._latest

    # ---------------- main flow
    def start(self):
        with self.lock:
            if self._thread and self._thread.is_alive():
                return False, "正在运行中"
            self._stop_evt.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return True, "已开始"

    def _run(self):
        try:
            self._pipeline()
        except Exception as e:
            import traceback
            self.log("异常: %s" % e)
            self.log(traceback.format_exc()[-800:])
            self.set(self.ERROR, "出错: %s" % e, 0)

    def _pipeline(self):
        self.set(self.LOCATING, "正在定位直播伴侣…", 5)

        # 1) locate
        def prog(p):
            self.set(message="扫描 %s" % p)

        idx, src = find_install(deep=True, on_progress=prog)
        if self._stop_evt.is_set():
            return self.set(self.IDLE, "已取消")
        if not idx:
            self.set(self.ERROR,
                     "没有找到直播伴侣。请先打开一次直播伴侣，或在「设置」里手动指定安装目录。")
            return
        with self.lock:
            self.index_js = idx
        self.set(message="已定位安装目录 (%s)" % src, progress=12)

        exe = find_exe(idx)
        with self.lock:
            self.exe_path = exe

        # 2) deploy hook
        self.set(self.INJECTING, "正在部署钩子…", 20)
        deploy_hook()
        clear_log()

        # 3) kill running app (index.js cannot be swapped while loaded)
        if is_running():
            self.set(message="正在关闭直播伴侣…", progress=28)
            kill_app()
            time.sleep(0.8)
        if self._stop_evt.is_set():
            return self.set(self.IDLE, "已取消")

        # 4) inject
        ok, msg = inject(idx)
        if not ok:
            self.set(self.ERROR, msg)
            return
        with self.lock:
            self.injected = True
        self.set(message="钩子注入完成", progress=38)

        # 5) launch
        if not exe:
            self.set(self.ERROR, "找不到 直播伴侣.exe")
            return
        ok, msg = launch(exe)
        if not ok:
            self.set(self.ERROR, msg)
            return
        self.set(self.LAUNCHING, "正在启动直播伴侣…", 45)

        # 6) wait hook ready
        self.set(self.WAITING, "等待直播伴侣加载钩子…", 55)
        hwnd = self._wait_hook(120)
        if self._stop_evt.is_set():
            return self.set(self.IDLE, "已取消")
        if not hwnd:
            self.set(self.ERROR,
                     "等待画面输出超时。请确认直播伴侣已进入「直播」界面并添加了画面源，"
                     "然后点重试。")
            return

        # 7) capture
        self.set(message="开始抓取画面", progress=85)
        self._start_capture(hwnd)
        self.set(self.READY, "抓取中", 100)

    def _wait_hook(self, timeout: float):
        """Wait for autoOpen; retry once by calling the hook directly."""
        t0 = time.time()
        triggered = False
        while time.time() - t0 < timeout:
            if self._stop_evt.is_set():
                return 0
            for ln in read_log(400):
                if "[auto] READY" in ln:
                    kv = {}
                    for part in ln.split("READY", 1)[1].split():
                        if "=" in part:
                            k, v = part.split("=", 1)
                            kv[k] = v
                    try:
                        self._apply_ready(kv)
                        return int(kv.get("hwnd", 0))
                    except Exception:
                        pass
                if "[auto] open FAILED" in ln and not triggered:
                    triggered = True
                    self.set(message="首次开窗失败，正在重试…")
                    send_cmd({"op": "auto"}, 20)
            if not triggered and time.time() - t0 > 45:
                triggered = True
                self.set(message="主动请求开窗…")
                r = send_cmd({"op": "detect"}, 8)
                if r and r.get("fit"):
                    hwnd, info = ensure_projector(log=self.log)
                    if hwnd:
                        self._apply_info(info)
                        with self.lock:
                            self.hwnd = hwnd
                        return hwnd
            elapsed = time.time() - t0
            self.set(progress=min(82, 55 + int(elapsed / timeout * 27)))
            time.sleep(1.0)
        return 0

    def _apply_ready(self, kv: dict):
        try:
            sz = kv.get("canvas", "0x0").split("x")
            tg = kv.get("target", "0x0").split("x")
            with self.lock:
                self.hwnd = int(kv.get("hwnd", 0))
                self.canvas = (int(sz[0]), int(sz[1]))
                self.win = (int(tg[0]), int(tg[1]))
                self.scale = float(kv.get("scale", 1.0))
                self.source_fps = int(kv.get("fps", 0) or 0)
        except Exception:
            pass

    def _apply_info(self, info: dict):
        if not info:
            return
        with self.lock:
            self.canvas = (info.get("canvas_w") or 0, info.get("canvas_h") or 0)
            self.win = (info.get("win_w") or 0, info.get("win_h") or 0)
            self.scale = info.get("scale") or 1.0
            self.source_fps = info.get("fps") or 0

    def _start_capture(self, hwnd: int):
        with self.lock:
            self._grabber = None
        g = Grabber(hwnd)
        g.start()
        ok, frame = g.read(6.0)
        if not ok:
            raise RuntimeError("窗口采集启动失败（可能被最小化或已关闭）")
        with self.lock:
            self._grabber = g
            self._latest = frame
            self._latest_n = g.n

    def stop(self, restore_app: bool = True):
        """Stop capture, optionally restore index.js."""
        self._stop_evt.set()
        with self.lock:
            g, self._grabber = self._grabber, None
            self._latest = None
        if g:
            g.stop()
        try:
            if self.hwnd:
                send_cmd({"op": "closeAllProj", "index": 0}, 3)
        except Exception:
            pass
        msgs = []
        if restore_app and self.index_js:
            was_running = bool(is_running())
            if was_running:
                kill_app()
            ok, msg = restore(self.index_js)
            msgs.append(msg)
            with self.lock:
                self.injected = not ok or injection_state(self.index_js) != "clean"
        with self.lock:
            self.state = self.IDLE
            self.message = "；".join(msgs) or "已停止"
            self.progress = 0
            self.hwnd = 0
            self.fps = 0
            self.frames = 0
        self.log(self.message)
        return True, self.message

    def on_frame_served(self):
        with self.lock:
            self.frames += 1
            t, n = self._fps_mark
            now = time.time()
            if now - t >= 1.0:
                self.fps = int((self.frames - n) / (now - t))
                self._fps_mark = (now, self.frames)

    def snapshot_png(self):
        """Encode the current frame as PNG bytes."""
        import io

        from PIL import Image
        with self.lock:
            frame = self._latest
        if frame is None:
            return None, "还没有画面"
        try:
            buf = io.BytesIO()
            Image.fromarray(frame[:, :, ::-1]).save(buf, format="PNG")
            return buf.getvalue(), None
        except Exception as e:
            return None, "编码失败: %s" % e

    def refresh_runtime(self):
        with self.lock:
            self.running_app = bool(is_running())
            if self.index_js:
                self.injected = injection_state(self.index_js) in ("injected",
                                                                   "injected-legacy")
