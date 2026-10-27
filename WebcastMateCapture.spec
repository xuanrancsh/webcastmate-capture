# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build spec — produces a single self-contained .exe."""
import os
import sys

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.abspath(os.path.dirname(SPEC))          # noqa: F821

datas = [(os.path.join(ROOT, "src", "assets"), "assets")]
binaries = []
hiddenimports = []

for pkg in ("webview", "clr_loader"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:                              # pragma: no cover
        print("collect_all(%s) failed: %s" % (pkg, e))

hiddenimports += [
    "clr", "pythonnet", "pythonnet.runtime",
    "numpy", "PIL", "PIL.Image", "PIL._imaging",
    "windows_capture",
    "bottle",           # pywebview's http backend
    "proxy_tools",
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
]

icon = os.path.join(ROOT, "src", "assets", "icon.ico")
if not os.path.isfile(icon):
    icon = None

a = Analysis(
    [os.path.join(ROOT, "src", "main.py")],
    pathex=[os.path.join(ROOT, "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter", "unittest", "pydoc_data", "test",
        "PySide6", "PyQt5", "PyQt6", "matplotlib", "scipy",
        "cv2", "pandas", "setuptools", "pip",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="WebcastMateCapture",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
    uac_admin=True,
)
