"""One-shot installer for the probe's Chrome native-messaging host.

Run once after sideloading the extension. It writes:

  1. A wrapper script (.cmd on Windows, .sh elsewhere) that invokes
     the current Python interpreter on relay/native_host.py.
  2. The native-messaging manifest JSON pointing at that wrapper, with
     allowed_origins restricted to the probe extension ID.
  3. (Windows only) HKCU registry keys under Chrome + Edge so the
     browser knows where to find the manifest.

Run uninstall via ``python -m relay.install_host --uninstall``.

The probe is intentionally not bundled into the prod installer; this
script is the only way it lands on a machine.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

# Allow direct invocation.
_THIS = Path(__file__).resolve()
if str(_THIS.parent.parent) not in sys.path:
    sys.path.insert(0, str(_THIS.parent.parent))

from relay import paths  # noqa: E402


NATIVE_HOST_NAME = "com.meeting_notetaker.probe"

# Derived from the RSA public key embedded in extension/manifest.json's
# `key` field. If you regenerate that key, also update this constant
# (the manifest derivation script in the README explains how).
EXTENSION_ID = "hllocpegdlgjbneinopdboclkekjljml"

# Windows HKCU registry paths Chrome + Edge consult.
_HKCU_CHROME = r"Software\Google\Chrome\NativeMessagingHosts"
_HKCU_EDGE = r"Software\Microsoft\Edge\NativeMessagingHosts"


def _wrapper_body(python_exe: Path, native_host_py: Path) -> str:
    if os.name == "nt":
        return (
            "@echo off\r\n"
            "REM Wrapper for Chrome's native-messaging host (OWA probe).\r\n"
            f'"{python_exe}" "{native_host_py}" %*\r\n'
        )
    return (
        "#!/usr/bin/env bash\n"
        f'exec "{python_exe}" "{native_host_py}" "$@"\n'
    )


def _write_wrapper(python_exe: Path, native_host_py: Path) -> Path:
    wrapper = paths.host_wrapper_path()
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(_wrapper_body(python_exe, native_host_py), encoding="utf-8")
    if os.name != "nt":
        wrapper.chmod(
            wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )
    return wrapper


def _write_manifest(wrapper: Path) -> Path:
    manifest_path = paths.native_host_manifest_path()
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": NATIVE_HOST_NAME,
        "description": (
            "Meeting Notetaker OWA Calendar Probe -- experimental "
            "bridge for issue #69 Option C. Not part of the production "
            "Meeting Notetaker install."
        ),
        "path": str(wrapper),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path


def _register_windows(manifest_path: Path, *, include_edge: bool) -> list[str]:
    if not sys.platform.startswith("win"):
        return []
    import winreg  # type: ignore[import-not-found]

    targets = [_HKCU_CHROME]
    if include_edge:
        targets.append(_HKCU_EDGE)
    written: list[str] = []
    for base in targets:
        full = rf"{base}\{NATIVE_HOST_NAME}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, full) as k:
            winreg.SetValue(k, "", winreg.REG_SZ, str(manifest_path))
        written.append(rf"HKCU\{full}")
    return written


def _unregister_windows(*, include_edge: bool) -> list[str]:
    if not sys.platform.startswith("win"):
        return []
    import winreg  # type: ignore[import-not-found]

    targets = [_HKCU_CHROME]
    if include_edge:
        targets.append(_HKCU_EDGE)
    removed: list[str] = []
    for base in targets:
        full = rf"{base}\{NATIVE_HOST_NAME}"
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, full)
            removed.append(rf"HKCU\{full}")
        except FileNotFoundError:
            pass
        except OSError:
            pass
    return removed


def install(*, include_edge: bool = True) -> dict:
    python_exe = Path(sys.executable).resolve()
    native_host_py = (paths.PROBE_ROOT / "relay" / "native_host.py").resolve()
    if not native_host_py.exists():
        raise FileNotFoundError(
            f"native_host.py missing at {native_host_py}; "
            "this script must run from within the probe checkout"
        )

    wrapper = _write_wrapper(python_exe, native_host_py)
    manifest = _write_manifest(wrapper)
    registry = _register_windows(manifest, include_edge=include_edge)

    return {
        "extension_id": EXTENSION_ID,
        "native_host_name": NATIVE_HOST_NAME,
        "wrapper": str(wrapper),
        "manifest": str(manifest),
        "registry_paths": registry,
        "python_exe": str(python_exe),
    }


def uninstall(*, include_edge: bool = True) -> dict:
    removed_paths: list[str] = []
    for p in (paths.host_wrapper_path(), paths.native_host_manifest_path()):
        if p.exists():
            try:
                p.unlink()
                removed_paths.append(str(p))
            except OSError:
                pass
    registry = _unregister_windows(include_edge=include_edge)
    return {"files_removed": removed_paths, "registry_removed": registry}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Reverse the install: drop the wrapper/manifest + HKCU keys.",
    )
    parser.add_argument(
        "--no-edge",
        action="store_true",
        help="Skip the Edge HKCU registration (Windows only).",
    )
    args = parser.parse_args()

    if args.uninstall:
        result = uninstall(include_edge=not args.no_edge)
    else:
        result = install(include_edge=not args.no_edge)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
