# pyinstaller spec for Meeting Notetaker. Run via `pyinstaller meeting_notetaker.spec`.
# Mirrors progman-py's pattern: --windowed --onefile, collect-all for PyQt6 and faster_whisper.
# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all, collect_data_files


datas = []
binaries = []
hiddenimports = []

for pkg in ("PyQt6", "faster_whisper"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

datas += collect_data_files("ctranslate2")
datas += [("meeting_notetaker/resources/prompts", "meeting_notetaker/resources/prompts")]


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=["extra-hooks"],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="meeting-notetaker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
