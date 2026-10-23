# PyInstaller spec: one-folder build in dist/Strux (run build.ps1).
# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

hidden = (
    collect_submodules("uvicorn") + collect_submodules("starlette") + collect_submodules("webview") + collect_submodules("uiautomation")
    + collect_submodules("pystray") + collect_submodules("winotify") + ["pynvml", "comtypes.gen"]
)
datas = [("ui/dist", "ui/dist"), ("config.yaml", "."), ("assets", "assets")] + collect_data_files("webview") + collect_data_files("uiautomation") \
    + collect_data_files("faster_whisper") + collect_data_files("_sounddevice_data")      # Silero VAD model, PortAudio dll

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=collect_dynamic_libs("ctranslate2"),
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    excludes=["tkinter", "matplotlib", "scipy", "pandas"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Strux",
    console=False,          # no console window; logs go to data/watcher.log
    icon="assets/strux.ico",
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Strux")
