# pyinstaller spec for Meeting Notetaker. Run via `pyinstaller meeting_notetaker.spec`.
# --windowed --onedir: produces dist/meeting-notetaker/ directory containing
# meeting-notetaker.exe + sibling .pyd/.dll/data files. The Inno Setup installer
# (installer.iss) recurses the whole tree into the install dir. Onedir avoids
# the onefile self-extraction to %TEMP% on every launch -- faster startup, no
# orphan _MEI* directories on crash, fewer antivirus heuristic hits.
# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all, collect_data_files


datas = []
binaries = []
hiddenimports = []

# SpeechBrain resolves model + module classes from string paths at runtime
# (yaml-driven hparam loading), so PyInstaller's static analysis misses
# huge swaths of its tree. Same story for torch/torchaudio backends and
# silero_vad's bundled ONNX weights. collect_all walks each package
# top-to-bottom and pulls every submodule + data file + binary, which is
# heavy but the only reliable way to get speaker-id working in a frozen
# build. Without this the runtime fails with
# "speaker-embedding model unavailable (SpeechBrain not installed)".
for pkg in (
    "PyQt6",
    "faster_whisper",
    "speechbrain",
    "torch",
    "torchaudio",
    "silero_vad",
    "huggingface_hub",
    "scipy",
):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

datas += collect_data_files("ctranslate2")
datas += [("meeting_notetaker/resources/prompts", "meeting_notetaker/resources/prompts")]
# Synthesis automation extension (v0.6.3+). The bundle contains the
# unpacked Chrome extension folder; the installer extracts it to
# %LOCALAPPDATA%\MeetingNotetaker\automation\extension when the user
# enables the feature and clicks Install in Settings.
datas += [("meeting_notetaker/resources/extension", "meeting_notetaker/resources/extension")]

# SpeechBrain's hparam yaml constructs classes via `!new:speechbrain...`
# tags resolved at runtime. PyInstaller cannot see those, so the relevant
# leaf modules need an explicit hint here.
hiddenimports += [
    "speechbrain.inference",
    "speechbrain.inference.speaker",
    "speechbrain.utils.fetching",
    "speechbrain.lobes.features",
    "speechbrain.lobes.models.ECAPA_TDNN",
    "speechbrain.processing.features",
    "speechbrain.nnet.containers",
    "speechbrain.nnet.normalization",
    "speechbrain.nnet.linear",
    "speechbrain.nnet.activations",
    "speechbrain.nnet.pooling",
    "speechbrain.nnet.CNN",
    "sentencepiece",
]

# Modules only reached via dynamic importlib lookups (dependency_check.py
# probes them via importlib.import_module to verify the bundle is intact;
# without a static import here, PyInstaller never sees them and drops
# them from the bundle, so the runtime check reports MISSING even though
# the package is in the venv). Add to this list whenever a new entry in
# dependency_check._GROUPS doesn't have a matching static import path in
# production code.
hiddenimports += [
    "sounddevice",
]

# pywin32 submodules that win32com loads lazily via __getattr__ at
# runtime when a COM property returns a typed value (date, currency,
# variant). Without an explicit hint, PyInstaller drops them from the
# bundle and the live app crashes the moment Outlook's calendar items
# are walked (item.Start triggers win32timezone resolution).
# See: ModuleNotFoundError reports against outlook_calendar
# _item_to_info -> win32com.client.dynamic.__getattr__.
hiddenimports += [
    "win32timezone",
    "pywintypes",
    "pythoncom",
]


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

# Onedir split: the EXE block produces only the launcher .exe (no binaries
# bundled in), then COLLECT walks the binaries + zipfiles + datas next to it
# in dist/meeting-notetaker/. The installer ships the whole directory tree.
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="meeting-notetaker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="meeting-notetaker",
)
