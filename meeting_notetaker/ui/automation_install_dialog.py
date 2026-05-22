"""Three-step install wizard for synthesis automation.

The user has chosen Path 3 (guided manual install) -- Chrome does not
permit silent installation of unpacked extensions, and we honor that.
The wizard's job is to make every step explicit and reversible:

  1. Extract the bundled extension to user space and offer to show it
     in Explorer.
  2. Open chrome://extensions with a copy-friendly Load-unpacked
     pointer.
  3. Verify the install -- write the native-host manifest + HKCU
     registry, then probe the extension by waiting for it to connect
     to the bridge.

Verify is the only step that does anything destructive (registry +
filesystem state changes). Re-running it is safe.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Callable

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..automation import installer
from ..utils.paths import extension_dir


log = logging.getLogger(__name__)


# How long to wait for an extension ping after Verify is clicked.
# The user has to actively switch focus to Chrome and (potentially)
# load the unpacked extension during this window if they haven't yet,
# so 60s is a reasonable upper bound -- shorter and a careful user
# loses their session; longer feels broken.
VERIFY_PROBE_SECONDS = 60


class AutomationInstallDialog(QDialog):
    """Modal install wizard. Created from the Settings dialog when the
    user toggles synthesis automation on.

    Dependencies are injected so the dialog stays test-friendly:
      * ``host_executable_provider`` returns the path Chrome will
        invoke for the native-messaging-host wrapper. In the running
        app this is the path to the frozen .exe (or the dev python
        binary).
      * ``ping_extension`` is a callable that fires a ``ping`` over
        the live Bridge and returns True if a pong arrives within
        ``timeout_sec``. The Settings dialog injects a function that
        wraps the controller's bridge; tests pass a fake.
    """

    def __init__(
        self,
        *,
        do_install: Callable[[], dict] | None = None,
        ping_extension: Callable[[float], bool] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Set up synthesis automation")
        self.setModal(True)
        self.resize(580, 480)
        # do_install runs all three steps of installer.install() but
        # lets the caller customize host_args (dev mode points the
        # wrapper at "python main.py --native-host"; frozen mode is
        # just "--native-host" on the running exe). None = default.
        self._do_install = do_install or (
            lambda: installer.install(host_executable=Path(sys.executable))
        )
        self._ping_extension = ping_extension

        layout = QVBoxLayout(self)

        header = QLabel(
            "<h2 style='margin: 0;'>Synthesis Automation</h2>"
            "<p style='color: #6b7280; margin: 4px 0 12px;'>"
            "Three quick steps. The browser stays the LLM intermediary; "
            "this extension automates the copy/paste you do today."
            "</p>",
            self,
        )
        header.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(header)

        # Step 1: extract + show in Explorer ---------------------------
        self._step1_box = _StepFrame(
            "1. Extract the extension files",
            "We'll copy the unpacked extension to a folder Chrome can read.",
            self,
        )
        self._extract_btn = QPushButton("Extract and open folder", self._step1_box)
        self._extract_btn.clicked.connect(self._on_extract_clicked)
        self._step1_box.add_action(self._extract_btn)
        self._step1_status = QLabel("Status: not extracted yet.", self._step1_box)
        self._step1_status.setStyleSheet("color: #6b7280;")
        self._step1_box.add_widget(self._step1_status)
        layout.addWidget(self._step1_box)

        # Step 2: chrome://extensions + load unpacked ------------------
        self._step2_box = _StepFrame(
            "2. Load the extension in Chrome",
            "We'll open chrome://extensions. Inside Chrome, enable "
            "Developer mode (top-right toggle), click Load unpacked, "
            "and select the folder from step 1.",
            self,
        )
        self._open_chrome_btn = QPushButton(
            "Open chrome://extensions", self._step2_box
        )
        self._open_chrome_btn.clicked.connect(self._on_open_chrome_clicked)
        self._open_chrome_btn.setEnabled(False)
        self._step2_box.add_action(self._open_chrome_btn)
        self._step2_help = QTextBrowser(self._step2_box)
        self._step2_help.setMaximumHeight(180)
        self._step2_help.setHtml(
            "<div style='font: 12px sans-serif; color: #4b5563;'>"
            "1. In Chrome, navigate to <code>chrome://extensions</code>.<br>"
            "2. Toggle <b>Developer mode</b> on (top right).<br>"
            "3. Click <b>Load unpacked</b>.<br>"
            "4. Pick the folder we opened for you in step 1, then come back here.<br><br>"
            "<b>One-time clipboard permission:</b> on your first Send, "
            "Chrome will ask <i>\"Allow claude.ai to see text and images "
            "copied to the clipboard?\"</i> -- click <b>Allow</b>. The "
            "extension uses Claude's own Copy button to read the response "
            "back, which requires this permission. If you don't grant it, "
            "the synthesis will fail with a clear error (no garbage data)."
            "</div>"
        )
        self._step2_box.add_widget(self._step2_help)
        layout.addWidget(self._step2_box)

        # Step 3: verify ---------------------------------------------------
        self._step3_box = _StepFrame(
            "3. Verify",
            "Registers the bridge so the extension can reach the app, "
            "then waits up to "
            f"{VERIFY_PROBE_SECONDS} seconds for the extension to connect.",
            self,
        )
        self._verify_btn = QPushButton("Verify", self._step3_box)
        self._verify_btn.clicked.connect(self._on_verify_clicked)
        self._verify_btn.setEnabled(False)
        self._step3_box.add_action(self._verify_btn)
        self._step3_status = QLabel("Status: pending steps 1-2.", self._step3_box)
        self._step3_status.setStyleSheet("color: #6b7280;")
        self._step3_status.setWordWrap(True)
        self._step3_box.add_widget(self._step3_status)
        layout.addWidget(self._step3_box)

        layout.addStretch(1)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Close, self
        )
        self._buttons.rejected.connect(self.reject)
        self._buttons.accepted.connect(self.accept)
        layout.addWidget(self._buttons)

        self._installed_ok = False
        self._refresh_state()

    # ------------------------------------------------------------------
    # Properties

    @property
    def installed_ok(self) -> bool:
        """True if Verify succeeded before the user closed the wizard."""
        return self._installed_ok

    # ------------------------------------------------------------------
    # Step handlers

    def _on_extract_clicked(self) -> None:
        try:
            path = installer.extract_extension()
        except (OSError, ValueError) as exc:
            self._step1_status.setText(
                f"<span style='color: #b91c1c;'>Extract failed: {exc}</span>"
            )
            self._step1_status.setTextFormat(Qt.TextFormat.RichText)
            return
        self._step1_status.setText(
            f"Status: extracted to <code>{path}</code>"
        )
        self._step1_status.setTextFormat(Qt.TextFormat.RichText)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        self._open_chrome_btn.setEnabled(True)
        self._verify_btn.setEnabled(True)

    def _on_open_chrome_clicked(self) -> None:
        # The chrome:// scheme isn't a registered URL handler in Windows
        # (or most OSes), so QDesktopServices.openUrl("chrome://...")
        # surfaces the OS "We can't open this 'chrome' link" dialog.
        # Locate chrome.exe directly and launch it with the URL as an
        # argument -- the in-app Chrome instance accepts chrome:// URLs
        # on the command line.
        chrome_exe = _locate_chrome_exe()
        if chrome_exe is not None:
            try:
                import subprocess  # noqa: PLC0415

                subprocess.Popen(  # noqa: S603 -- argv is a list, no shell
                    [str(chrome_exe), "chrome://extensions"],
                    close_fds=True,
                )
                return
            except OSError as exc:
                log.warning("chrome launch failed: %s", exc)
        # Fallback: copy the URL to the clipboard and surface a hint.
        try:
            from PyQt6.QtWidgets import QApplication  # noqa: PLC0415

            QApplication.clipboard().setText("chrome://extensions")
        except Exception:  # noqa: BLE001 -- defensive
            pass
        self._step2_help.setHtml(
            "<div style='font: 12px sans-serif; color: #b91c1c;'>"
            "Couldn't launch Chrome automatically. The URL has been "
            "copied to your clipboard -- open Chrome and paste into "
            "the address bar:<br><code>chrome://extensions</code>"
            "</div>"
        )

    def _on_verify_clicked(self) -> None:
        self._verify_btn.setEnabled(False)
        self._step3_status.setText("Verifying...")
        try:
            state = self._do_install()
        except (OSError, ValueError) as exc:
            self._step3_status.setText(
                f"<span style='color: #b91c1c;'>Install failed: {exc}</span>"
            )
            self._step3_status.setTextFormat(Qt.TextFormat.RichText)
            self._verify_btn.setEnabled(True)
            return

        # Registry write done. Tell the user, then probe the extension.
        registry_note = ""
        if sys.platform.startswith("win"):
            if state["registry_chrome"]:
                registry_note = " (Chrome registry: ok)"
            else:
                registry_note = " (Chrome registry: not present)"
        self._step3_status.setText(
            f"Bridge registered{registry_note}. "
            f"Waiting up to {VERIFY_PROBE_SECONDS}s for the extension to connect..."
        )
        QTimer.singleShot(50, self._do_probe)

    def _do_probe(self) -> None:
        ok = False
        if self._ping_extension is not None:
            try:
                ok = self._ping_extension(float(VERIFY_PROBE_SECONDS))
            except Exception:  # noqa: BLE001 -- surface as failure
                log.exception("ping_extension raised during verify")
                ok = False
        else:
            # Tests without a real bridge can skip probing; we still
            # report success if the install state is good off-Windows.
            ok = installer.is_fully_installed()
        if ok:
            self._installed_ok = True
            self._step3_status.setText(
                "<span style='color: #047857;'>Connected. You can close this dialog.</span>"
            )
            self._step3_status.setTextFormat(Qt.TextFormat.RichText)
            self._buttons.setStandardButtons(QDialogButtonBox.StandardButton.Ok)
        else:
            self._step3_status.setText(
                "<span style='color: #b91c1c;'>"
                "Didn't hear back from the extension within the time window. "
                "Make sure the extension is loaded at chrome://extensions and "
                "try Verify again. If you just loaded it, give Chrome a few "
                "seconds to spin up the service worker."
                "</span>"
            )
            self._step3_status.setTextFormat(Qt.TextFormat.RichText)
        self._verify_btn.setEnabled(True)

    # ------------------------------------------------------------------

    def _refresh_state(self) -> None:
        """Sync the wizard's idea of progress with what's actually on
        disk. Lets the user re-open the wizard mid-way through."""
        state = installer.installation_state()
        if state["extension_extracted"]:
            self._step1_status.setText(
                f"Status: extracted to <code>{state['extension_path']}</code>"
            )
            self._step1_status.setTextFormat(Qt.TextFormat.RichText)
            self._open_chrome_btn.setEnabled(True)
            self._verify_btn.setEnabled(True)


def _locate_chrome_exe() -> Path | None:
    """Best-effort lookup for the Chrome executable. Tries (in order):

      1. The App Paths registry key
         (HKLM\\Software\\Microsoft\\Windows\\CurrentVersion\\App Paths\\chrome.exe).
         This is the canonical Windows mechanism for finding installed
         apps -- if Chrome is on the box, the key is set even when the
         binary isn't on PATH.
      2. The default per-machine install path under Program Files.
      3. The per-user install path under %LOCALAPPDATA%.
      4. ``shutil.which("chrome")`` as a last-ditch PATH lookup.

    Returns None if none of the above resolves. On non-Windows the
    function still works against PATH-based ``chrome`` (or
    ``google-chrome``); dev hosts rarely need this code path.
    """
    if sys.platform.startswith("win"):
        try:
            import winreg  # type: ignore[import-not-found]  # noqa: PLC0415

            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(
                        hive,
                        r"Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
                    ) as k:
                        value, _ = winreg.QueryValueEx(k, None)
                        if value:
                            p = Path(value)
                            if p.exists():
                                return p
                except FileNotFoundError:
                    continue
        except ImportError:
            pass

        candidates = [
            Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
            Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        ]
        local_app = sys.platform.startswith("win") and __import__("os").environ.get(
            "LOCALAPPDATA"
        )
        if local_app:
            candidates.append(
                Path(local_app) / "Google" / "Chrome" / "Application" / "chrome.exe"
            )
        for c in candidates:
            if c.exists():
                return c

    import shutil  # noqa: PLC0415

    for name in ("chrome", "google-chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


class _StepFrame(QFrame):
    """Visual container for one step of the wizard."""

    def __init__(self, title: str, blurb: str, parent: QWidget) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        title_label = QLabel(f"<b>{title}</b>", self)
        title_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(title_label)
        blurb_label = QLabel(blurb, self)
        blurb_label.setWordWrap(True)
        blurb_label.setStyleSheet("color: #4b5563;")
        layout.addWidget(blurb_label)
        self._actions = QHBoxLayout()
        self._actions.setSpacing(8)
        layout.addLayout(self._actions)
        self._inner = layout

    def add_action(self, widget: QWidget) -> None:
        self._actions.addWidget(widget)
        self._actions.addStretch(1)

    def add_widget(self, widget: QWidget) -> None:
        self._inner.addWidget(widget)
