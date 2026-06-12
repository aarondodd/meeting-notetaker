"""Extract the bundled extension to user space, write the native-
messaging-host manifest, register the host in HKCU on Windows.

Per Path 3 (guided manual install): the user still loads the unpacked
extension from chrome://extensions themselves. The installer's job is
everything *around* that: putting the files where Chrome can find them
on disk, registering the native-messaging-host so the extension can
talk to the running app, and providing a verify-and-rollback path so
the install state is recoverable from the Settings UI.

The native-messaging-host manifest's ``allowed_origins`` field
constrains *which* extensions are allowed to call our host. We pin
that to a single deterministic extension ID derived from the manifest
``key`` field (see EXTENSION_ID below). A user could theoretically
load the same extension folder with a modified manifest and a
different ID -- in that case the host would reject the connection,
which is the right outcome.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from ..utils.paths import (
    automation_dir,
    extension_dir,
    native_host_manifest_path,
    resource_path,
)


log = logging.getLogger(__name__)


NATIVE_HOST_NAME = "com.meeting_notetaker.bridge"

# The deterministic extension ID derived from the SPKI public key
# embedded in resources/extension/manifest.json's ``key`` field. If
# that key changes, this constant must change too -- they're a pair.
# A mismatch produces a silent "host rejects extension" failure that
# is hard to debug remotely; the validator below catches it at install
# time.
EXTENSION_ID = "gmnecenhibfigbpldhacjhgmooopeelo"


# Windows HKCU path where Chrome looks for per-user native-messaging
# hosts. Edge would be HKCU\Software\Microsoft\Edge\NativeMessagingHosts
# -- we register both so the same extension works in either browser
# even if Aaron ever runs it in Edge for testing.
_HKCU_CHROME = r"Software\Google\Chrome\NativeMessagingHosts"
_HKCU_EDGE = r"Software\Microsoft\Edge\NativeMessagingHosts"


# ---------------------------------------------------------------------------
# Extension folder


def installed_extension_version() -> str:
    """Return the ``version`` string from the installed extension's
    ``manifest.json`` (the copy Chrome loads via 'Load unpacked').

    Empty string when the extension hasn't been extracted yet,
    when ``manifest.json`` is missing, or when the file isn't valid
    JSON. Used by the version-skew check (#102 bug 7) to compare
    against the value the running extension reports in its pong;
    a mismatch surfaces a one-shot QMessageBox asking the user to
    reload at chrome://extensions.
    """
    target = extension_dir() / "manifest.json"
    if not target.is_file():
        return ""
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("version") or "")


def extract_extension(*, source: Path | None = None, dest: Path | None = None) -> Path:
    """Copy the bundled extension folder to user space.

    The user opens chrome://extensions and points at the returned path.
    Re-extracting is safe; we delete any pre-existing contents first
    so a stale file from an older version doesn't shadow a new one.
    """
    source = source or resource_path("extension")
    dest = dest or extension_dir()
    if not source.exists():
        raise FileNotFoundError(
            f"bundled extension missing at {source} (build packaging issue?)"
        )
    if not (source / "manifest.json").exists():
        raise FileNotFoundError(
            f"source extension folder {source} has no manifest.json"
        )
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    # Sanity check: manifest reaches the destination.
    if not (dest / "manifest.json").exists():
        raise OSError(f"extension copy didn't land manifest.json at {dest}")
    # Validate the deterministic key/id pair so a future manifest edit
    # that forgot to bump EXTENSION_ID surfaces here instead of as a
    # silent runtime rejection.
    manifest = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    key_b64 = manifest.get("key", "")
    if key_b64:
        derived = _derive_extension_id_from_key(key_b64)
        if derived != EXTENSION_ID:
            raise ValueError(
                f"extension key/id mismatch: manifest key derives to "
                f"{derived!r} but installer.EXTENSION_ID is "
                f"{EXTENSION_ID!r}. Update one or the other."
            )
    return dest


def _derive_extension_id_from_key(key_b64: str) -> str:
    """Replicate Chrome's derivation: SHA256 of the raw SPKI bytes, take
    the first 16 bytes, map each nibble (0-15) to a letter (a-p)."""
    import base64

    raw = base64.b64decode(key_b64)
    digest = hashlib.sha256(raw).digest()[:16]
    return "".join(
        chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 0xF)) for b in digest
    )


# ---------------------------------------------------------------------------
# Native-messaging-host manifest JSON


def write_native_host_manifest(
    *,
    host_executable: Path,
    host_args: list[str] | None = None,
    manifest_path: Path | None = None,
) -> Path:
    """Write the JSON manifest Chrome reads when ``connectNative`` fires.

    Chrome's spec doesn't allow a host manifest to take CLI args
    directly -- the manifest's ``path`` must point at an executable
    that gets invoked with no args. Our solution: the manifest path
    points at a tiny wrapper batch file we generate alongside; the
    wrapper invokes the real exe with ``--native-host``.
    """
    manifest_path = manifest_path or native_host_manifest_path()
    host_args = host_args if host_args is not None else ["--native-host"]
    manifest_path.parent.mkdir(parents=True, exist_ok=True)

    # Generate the wrapper that gets pointed at by the manifest's path.
    # On Windows this is a .cmd; elsewhere it's a shell script (only
    # used during dev / Linux smoke testing).
    if sys.platform.startswith("win"):
        wrapper_path = automation_dir() / "native_host.cmd"
        wrapper_body = (
            "@echo off\r\n"
            "REM Wrapper invoked by Chrome's native-messaging host.\r\n"
            f'"{host_executable}" {" ".join(host_args)} %*\r\n'
        )
    else:
        wrapper_path = automation_dir() / "native_host.sh"
        wrapper_body = (
            "#!/usr/bin/env bash\n"
            f'exec "{host_executable}" {" ".join(host_args)} "$@"\n'
        )
    wrapper_path.write_text(wrapper_body, encoding="utf-8")
    if not sys.platform.startswith("win"):
        wrapper_path.chmod(0o755)

    manifest = {
        "name": NATIVE_HOST_NAME,
        "description": "Meeting Notetaker bridge: routes synthesis "
        "prompts between the desktop app and the web LLM tab.",
        "path": str(wrapper_path),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path


# ---------------------------------------------------------------------------
# Windows registry


def register_native_host(
    *, manifest_path: Path | None = None, include_edge: bool = True
) -> list[str]:
    """Write HKCU keys pointing Chrome (and optionally Edge) at the
    native-messaging manifest. Returns the list of registry paths
    that were updated, for the Settings UI to report. On non-Windows
    this is a no-op that returns ``[]`` (dev environments don't have
    Chrome talking to a registry)."""
    manifest_path = manifest_path or native_host_manifest_path()
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"native-host manifest missing at {manifest_path}; "
            f"call write_native_host_manifest() first"
        )
    if not sys.platform.startswith("win"):
        log.info(
            "register_native_host: non-Windows platform, skipping registry write"
        )
        return []

    import winreg  # type: ignore[import-not-found]  # noqa: PLC0415

    written: list[str] = []
    paths = [_HKCU_CHROME]
    if include_edge:
        paths.append(_HKCU_EDGE)
    for base in paths:
        full = rf"{base}\{NATIVE_HOST_NAME}"
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, full) as k:
            winreg.SetValue(k, "", winreg.REG_SZ, str(manifest_path))
        written.append(rf"HKCU\{full}")
    return written


def unregister_native_host(*, include_edge: bool = True) -> list[str]:
    """Remove the HKCU keys. Returns the list of paths actually
    removed (i.e. that existed before the call)."""
    if not sys.platform.startswith("win"):
        return []
    import winreg  # type: ignore[import-not-found]  # noqa: PLC0415

    removed: list[str] = []
    paths = [_HKCU_CHROME]
    if include_edge:
        paths.append(_HKCU_EDGE)
    for base in paths:
        full = rf"{base}\{NATIVE_HOST_NAME}"
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, full)
            removed.append(rf"HKCU\{full}")
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("DeleteKey %s failed: %s", full, exc)
    return removed


# ---------------------------------------------------------------------------
# State queries


def installation_state() -> dict:
    """Snapshot of the install for the Settings status indicator.

    Fields:
      * ``extension_extracted``: bool -- folder + manifest.json present
      * ``extension_path``: str
      * ``native_manifest_written``: bool
      * ``native_manifest_path``: str
      * ``registry_chrome``: bool -- HKCU key present (Windows only)
      * ``registry_edge``: bool
    """
    ext_path = extension_dir()
    manifest_path = native_host_manifest_path()
    state = {
        "extension_extracted": (ext_path / "manifest.json").exists(),
        "extension_path": str(ext_path),
        "native_manifest_written": manifest_path.exists(),
        "native_manifest_path": str(manifest_path),
        "extension_id": EXTENSION_ID,
        "registry_chrome": False,
        "registry_edge": False,
    }
    if sys.platform.startswith("win"):
        import winreg  # type: ignore[import-not-found]  # noqa: PLC0415

        for base, key in ((_HKCU_CHROME, "registry_chrome"), (_HKCU_EDGE, "registry_edge")):
            full = rf"{base}\{NATIVE_HOST_NAME}"
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, full):
                    state[key] = True
            except FileNotFoundError:
                state[key] = False
            except OSError:
                state[key] = False
    return state


def is_fully_installed() -> bool:
    s = installation_state()
    if not s["extension_extracted"] or not s["native_manifest_written"]:
        return False
    if sys.platform.startswith("win"):
        return s["registry_chrome"]
    # Off-Windows we don't gate on registry presence (dev environments).
    return True


# ---------------------------------------------------------------------------
# One-shot orchestration


def install(*, host_executable: Path) -> dict:
    """End-to-end install. Idempotent: safe to re-run.

    Returns the post-install state dict (same shape as
    ``installation_state()``)."""
    extract_extension()
    write_native_host_manifest(host_executable=host_executable)
    register_native_host()
    return installation_state()


def uninstall(*, keep_extension_files: bool = True) -> dict:
    """Tear down the install. By default the extension folder is left
    on disk -- Aaron chose Path 3 (manual install) so the user owns
    the chrome://extensions side; ripping the files out from under
    them would orphan their Chrome-side toggle."""
    unregister_native_host()
    try:
        native_host_manifest_path().unlink()
    except FileNotFoundError:
        pass
    if not keep_extension_files:
        try:
            shutil.rmtree(extension_dir())
        except FileNotFoundError:
            pass
    return installation_state()
