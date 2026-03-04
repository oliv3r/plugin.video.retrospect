# SPDX-License-Identifier: GPL-3.0-or-later
"""Device flow authentication dialog backed by a Kodi XML skin."""

import os
import tempfile
import threading
import time
from typing import Any, Optional, Type

import xbmc
import xbmcgui

from resources.lib.helpers.languagehelper import LanguageHelper
from resources.lib.logger import Logger
from resources.lib.retroconfig import Config


ACTION_PREVIOUS_MENU = 10
ACTION_NAV_BACK = 92
KODI_STRING_CANCEL = 222

XML_ID_TITLE = 10
XML_ID_LOGO = 20
XML_ID_QR_TEXT = 30
XML_ID_QR_IMAGE = 40
XML_ID_VISIT_TEXT = 50
XML_ID_VISIT_URL = 60
XML_ID_CODE_TEXT = 70
XML_ID_CODE = 80
XML_ID_PROGRESS = 90
XML_ID_TIME = 100
XML_ID_BTN_CANCEL = 110
XML_ID_BTN_MANUAL = 120

XML_PROGRESS_BAR_WIDTH = 1000
"""Must match the blue fill image width in DeviceAuthDialog.xml."""

XML_FILENAME = "DeviceAuthDialog.xml"
"""Kodi skin XML file; passed to WindowXMLDialog via __new__."""

_SECONDS_PER_MINUTE = 60
_COLOR_CHANNEL_MAX = 255
"""Maximum value of a single 8-bit colour channel (0–255)."""
_WARN_THRESHOLD_PCT = 25.0
"""Progress bar stays full colour above this percentage remaining."""
_DANGER_THRESHOLD_PCT = 5.0
"""Progress bar goes full red below this percentage remaining."""


class DeviceAuthDialog(xbmcgui.WindowXMLDialog):
    """
    Device flow authentication dialog: URL, user code, countdown, optional manual-login button.

    All user-visible text is supplied via constructor parameters so callers
    can pass localized strings.  After ``doModal()`` returns, read ``result``
    to discover how the dialog was closed.
    """


    def __new__(cls: Type["DeviceAuthDialog"], *args: Any, **kwargs: Any) -> "DeviceAuthDialog":
        """Intercept Python constructor args so Kodi's C tp_new only sees xml+path."""

        addon_path = Config.rootDir.rstrip("/\\")
        return super().__new__(cls, XML_FILENAME, addon_path)


    def __init__(self, title: str, visit_text: str, visit_url: str, code_text: str, code: str,
                 timeout: int, manual_label: Optional[str] = None, qr_url: Optional[str] = None,
                 logo_path: Optional[str] = None) -> None:
        """
        Create the dialog.

        :param str title:             Dialog title.
        :param str visit_text:        Instruction text above the URL.
        :param str visit_url:         URL to display.
        :param str code_text:         Text above the authorization code.
        :param str code:              The authorization code.
        :param int timeout:           Seconds until the code/dialog expires.
        :param str|None manual_label: Label for the manual-login button (omit to hide).
        :param str|None qr_url:       URL to encode as a QR code (omit for text-only).
        :param str|None logo_path:    Path to a logo image for the header (defaults to
                                      the Retrospect addon icon when omitted).
        """

        super().__init__()

        self._title = title
        self._logo_path = logo_path or os.path.join(Config.rootDir, "resources", "media", "icon.png")
        self._qr_url = qr_url
        self._qr_path = None
        if qr_url:
            try:
                import qrcode
                os.makedirs(Config.cacheDir, exist_ok=True)
                fd, qr_path = tempfile.mkstemp(prefix="qr_", suffix=".png", dir=Config.cacheDir)
                os.close(fd)
                qrcode.make(qr_url).save(qr_path)
                self._qr_path = qr_path
            except Exception as e:
                Logger.warning(f"Unable to generate QR code: {e}")
        self._visit_text = visit_text
        self._visit_url = visit_url
        self._code_text = code_text
        self._code = code
        self._timeout = timeout
        self._manual_label = manual_label
        self._canceled = False
        self._manual = False
        self._poll_result: Optional[str] = None
        self._start_time: Optional[float] = None
        self._stop_event = threading.Event()


    def __del__(self) -> None:
        if self._qr_path:
            try:
                os.remove(self._qr_path)
            except OSError as e:
                Logger.error(f"Failed to remove cached QR code '{self._qr_path}': {e}")


    # -- Public interface --------------------------------------------------

    @property
    def stop_event(self) -> threading.Event:
        """
        Event set when the dialog closes for any reason.

        Background threads can block on ``stop_event.wait(timeout)`` instead of
        busy-polling ``result``.  The event is set in ``onClosed()``, which fires
        for every close path: button click, Escape/Back, programmatic ``close()``,
        Kodi shutdown.
        """

        return self._stop_event


    @property
    def result(self) -> Optional[str]:
        """
        The closing action after ``doModal()`` returns.

        :return: ``"success"`` — poll thread confirmed authentication;
                 ``"timeout"`` — ``timeout`` elapsed with no auth;
                 ``"manual"`` — manual-login button pressed;
                 ``"canceled"`` — cancel button, Back key, or safety net;
                 ``None`` — dialog not yet closed.
        :rtype: str | None
        """

        if self._canceled:
            return "canceled"

        if self._manual:
            return "manual"

        return self._poll_result


    def close_with(self, result: str) -> None:
        """
        Close the dialog and record an explicit result.

        Called by background workers or the internal timeout handler to set a
        result without triggering the canceled safety net in ``onClosed()``.
        RFC 8628 poll terminal values (``"success"``, ``"error"``) are valid
        here and map directly to ``result`` without translation.

        :param str result: Result string; see the ``result`` property for valid values.
        """

        self._poll_result = result
        self.close()


    def update_progress(self) -> None:
        """
        Update the progress bar and countdown.

        Computes elapsed time from ``_start_time`` set in ``onInit``.
        Closes the dialog automatically when the code expires.
        Is a no-op before ``onInit`` runs.

        Must be called periodically by the caller (e.g. every second) to drive
        the countdown. The dialog does not self-tick — it relies entirely on
        external polling to advance the progress bar and detect timeout.

        The progress bar fades from white (≥25% remaining) through orange to
        red (≤5% remaining) to signal imminent expiry.
        """

        if self._start_time is None:
            return

        elapsed = time.time() - self._start_time
        if elapsed >= self._timeout:
            self.close_with("timeout")
            return

        remaining_seconds = max(0, self._timeout - int(elapsed))
        percent = max(0.0, 100.0 - (elapsed / self._timeout) * 100.0)
        bar_width = max(0, int(XML_PROGRESS_BAR_WIDTH * percent / 100))
        mins = remaining_seconds // _SECONDS_PER_MINUTE
        secs = remaining_seconds % _SECONDS_PER_MINUTE

        if percent >= _WARN_THRESHOLD_PCT:
            g = b = _COLOR_CHANNEL_MAX
        elif percent <= _DANGER_THRESHOLD_PCT:
            g = b = 0
        else:
            t = (percent - _DANGER_THRESHOLD_PCT) / (_WARN_THRESHOLD_PCT - _DANGER_THRESHOLD_PCT)
            g = b = int(_COLOR_CHANNEL_MAX * t)
        bar_color = "FF{:02X}{:02X}{:02X}".format(_COLOR_CHANNEL_MAX, g, b)

        progress = self.getControl(XML_ID_PROGRESS)
        progress.setWidth(bar_width)
        progress.setColorDiffuse(bar_color)
        self.getControl(XML_ID_TIME).setLabel("{:d}:{:02d}".format(mins, secs))


    # -- Kodi callbacks ----------------------------------------------------

    def onInit(self) -> None:
        """Populate all dialog controls when the window is first shown."""

        self._start_time = time.time()

        self.getControl(XML_ID_TITLE).setLabel(self._title)
        self.getControl(XML_ID_LOGO).setImage(self._logo_path)
        self.getControl(XML_ID_VISIT_TEXT).setLabel(self._visit_text)
        self.getControl(XML_ID_VISIT_URL).setLabel(self._visit_url)
        self.getControl(XML_ID_CODE_TEXT).setLabel(self._code_text)
        self.getControl(XML_ID_CODE).setLabel(self._code)

        if self._qr_path:
            qr_ctrl = self.getControl(XML_ID_QR_IMAGE)
            qr_ctrl.setImage(self._qr_path)
            qr_ctrl.setVisible(True)
            self.getControl(XML_ID_QR_TEXT).setLabel(
                LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupQrInstruction))
        elif self._qr_url:
            self.getControl(XML_ID_QR_TEXT).setLabel(
                LanguageHelper.get_localized_string(LanguageHelper.QrAddonMissing))
            self.getControl(XML_ID_QR_IMAGE).setVisible(False)
        else:
            self.getControl(XML_ID_QR_TEXT).setVisible(False)
            self.getControl(XML_ID_QR_IMAGE).setVisible(False)

        self.getControl(XML_ID_BTN_CANCEL).setLabel(xbmc.getLocalizedString(KODI_STRING_CANCEL))

        btn_manual = self.getControl(XML_ID_BTN_MANUAL)
        if self._manual_label is None:
            btn_manual.setVisible(False)
        else:
            btn_manual.setLabel(self._manual_label)

        self.update_progress()


    def onClosed(self) -> None:
        """Signal the poll thread and record cancellation if the dialog was dismissed."""

        self._stop_event.set()
        if (not self._manual and
                not self._canceled and
                self._poll_result is None):
            self._canceled = True


    def onAction(self, action: Any) -> None:
        action_id = action.getId()
        if action_id in (ACTION_PREVIOUS_MENU, ACTION_NAV_BACK):
            self._canceled = True
            self.close()


    def onClick(self, controlId: int) -> None:
        if controlId == XML_ID_BTN_CANCEL:
            self._canceled = True
            self.close()
        elif controlId == XML_ID_BTN_MANUAL:
            self._manual = True
            self.close()
