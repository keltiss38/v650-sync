# -*- mode: python ; coding: utf-8 -*-
# Construction de l'exécutable Windows: .venv\Scripts\pyinstaller v650sync.spec
from PyInstaller.utils.hooks import collect_all, collect_submodules

hidden = collect_submodules("zeroconf")
datas = [("v650sync/index.html", "v650sync"), ("v650sync/icon.ico", "v650sync"), ("v650sync/icon.png", "v650sync"),
         ("v650sync/mapdata/tags_polar.json", "v650sync/mapdata"), ("v650sync/mapdata/zoom_polar.json", "v650sync/mapdata"), ("v650sync/i18n_en.json", "v650sync")]
binaries = []
# Fenêtre native (pywebview + WebView2, via pythonnet): facultative, sans elle le programme ouvre une fenêtre Edge/Chrome
for pkg in ("webview", "clr_loader", "pythonnet", "proxy_tools", "bottle"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hidden += h
    except Exception:
        pass
hidden += ["clr", "webview.platforms.edgechromium", "webview.platforms.winforms"]

a = Analysis(
    ["v650sync_app.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "pydoc"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="V650Sync",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # pas de fenêtre console (erreurs dans %LOCALAPPDATA%\V650Sync\journal.txt)
    icon="v650sync/icon.ico",
    disable_windowed_traceback=False,
)
