# Override of the contrib hook for webrtcvad. The stock hook hard-codes
# `copy_metadata('webrtcvad')`, which crashes on Windows + Python 3.13
# where we install `webrtcvad-wheels` (same `import webrtcvad` module,
# different PyPI distribution name). Try both, fall through to a no-op
# if neither is registered. Our usage (audio/vad.py) never reads the
# package metadata, so an empty datas list is safe.
from PyInstaller.utils.hooks import copy_metadata

datas = []
for dist_name in ("webrtcvad", "webrtcvad-wheels"):
    try:
        datas = copy_metadata(dist_name)
        break
    except Exception:
        continue
