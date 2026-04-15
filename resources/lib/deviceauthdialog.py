# SPDX-License-Identifier: GPL-3.0-or-later
"""Device flow authentication dialog backed by a Kodi XML skin."""

import os
import tempfile
import threading
import time
from typing import Any, Optional, Type

import xbmc
import xbmcgui

from resources.lib.authentication.authenticationhandler import DeviceAuthResult
from resources.lib.helpers.languagehelper import LanguageHelper
from resources.lib.logger import Logger
from resources.lib.retroconfig import Config
from resources.lib.authentication.authenticationhandler import DeviceAuthResult


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
"""Maximum value of a single 8-bit color channel (0-255)."""
_WARN_THRESHOLD = 25.0
"""Progress bar stays full color above this percentage remaining."""
_DANGER_THRESHOLD = 5.0
"""Progress bar goes full red below this percentage remaining."""


class DeviceAuthDialog:
    """
    Device flow authentication dialog: URL, user code, countdown,
    optional manual-login button.

    Construct via the normal constructor -- invalid required arguments cause
    __new__ to return None so callers can check without exceptions:

        dialog = DeviceAuthDialog(visit_url=..., code=..., timeout=...)
        if dialog is None:
            # handle error

    All user-visible text is supplied via constructor parameters.  After
    doModal() returns, read result to discover how the dialog was closed.
    """

    class _WindowXMLDialogWrapper(xbmcgui.WindowXMLDialog):
        """
        Kodi XML skin wrapper -- private to DeviceAuthDialog.

        This class exists solely to satisfy the WindowXMLDialog contract: it
        allocates the Kodi XML window and routes the four Kodi callbacks back
        to the owning DeviceAuthDialog instance. All state and logic live there.
        """

        def __new__(cls: Type["DeviceAuthDialog._WindowXMLDialogWrapper"],
                    *args: Any, **kwargs: Any) -> "DeviceAuthDialog._WindowXMLDialogWrapper":
            addon_path = Config.rootDir.rstrip("/\\")
            return super().__new__(cls, XML_FILENAME, addon_path)


        def __init__(self, owner: "DeviceAuthDialog") -> None:
            super().__init__()
            self._owner = owner


        def onInit(self) -> None:
            self._owner._on_init()


        def onClosed(self) -> None:
            self._owner._on_closed()


        def onAction(self, action: Any) -> None:
            self._owner._on_action(action)


        def onClick(self, control_id: int) -> None:
            self._owner._on_click(control_id)


    __dialog_wrapper: "DeviceAuthDialog._WindowXMLDialogWrapper"

    def __new__(cls,  # type: ignore[misc]
                visit_url: str,
                code: str,
                timeout: int,
                **kwargs: Any) -> Optional["DeviceAuthDialog"]:
        """
        Validate required arguments before allocating the instance.

        Returns None and logs an error when any required argument is invalid,
        so callers can handle the failure without exceptions.

        :param visit_url: URL to display -- must be a non-empty string.
        :param code:      Authorization code -- must be a non-empty string.
        :param timeout:   Seconds until expiry -- must be a positive integer.
        """

        if not isinstance(visit_url, str) or not visit_url:
            Logger.error(f"DeviceAuthDialog: visit_url must be a non-empty string, got {visit_url!r}")
            return None

        if not isinstance(code, str) or not code:
            Logger.error(f"DeviceAuthDialog: code must be a non-empty string, got {code!r}")
            return None

        if not isinstance(timeout, int) or timeout <= 0:
            Logger.error(f"DeviceAuthDialog: timeout must be a positive integer, got {timeout!r}")
            return None

        return super().__new__(cls)


    def __init__(self, visit_url: str, code: str, timeout: int,
                 title: Optional[str] = None,
                 visit_text: Optional[str] = None,
                 code_text: Optional[str] = None,
                 qr_url: Optional[str] = None,
                 logo_path: Optional[str] = None) -> None:
        """
        Initialize the dialog state and create the Kodi XML window.

        Only called when __new__ succeeds (i.e. all required arguments are
        valid).

        :param visit_url:  URL to display.
        :param code:       The authorization code.
        :param timeout:    Seconds until the code/dialog expires.
        :param title:      Dialog title (defaults to DeviceSetupTitle).
        :param visit_text: Instruction text above the URL (defaults to DeviceSetupVisit).
        :param code_text:  Text above the code (defaults to DeviceSetupEnterCode).
        :param qr_url:     URL to encode as a QR code (omit for text-only).
        :param logo_path:  Path to a logo image (defaults to the Retrospect addon icon).
        """

        self._title = title or LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupTitle)
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
        self._visit_text = visit_text or LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupVisit)
        self._visit_url = visit_url
        self._code_text = code_text or LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupEnterCode)
        self._code = code
        self._timeout = timeout
        self._manual_label = LanguageHelper.get_localized_string(LanguageHelper.ManualLogin)
        self._canceled = False
        self._manual = False
        self._poll_result: Optional[DeviceAuthResult] = None
        self._start_time: Optional[float] = None
        self._stop_event = threading.Event()

        # Kodi XML window -- __new__ validated args so this always succeeds.
        self.__dialog_wrapper = DeviceAuthDialog._WindowXMLDialogWrapper(self)


    def __del__(self) -> None:
        """ Remove any temporary QR code image created during initialisation."""

        if self._qr_path:
            try:
                os.remove(self._qr_path)
            except OSError as e:
                Logger.error(f"Failed to remove cached QR code '{self._qr_path}': {e}")


    # -- Public interface --------------------------------------------------

    def doModal(self) -> None:
        """ Show the dialog and block until it is closed."""

        self.__dialog_wrapper.doModal()


    def getControl(self, control_id: int) -> Any:
        """ Return the Kodi control with the given XML ID."""

        return self.__dialog_wrapper.getControl(control_id)


    @property
    def _closed(self) -> bool:
        """ True if the underlying Kodi dialog has been closed (test helper)."""

        return self.__dialog_wrapper._closed


    @property
    def stop_event(self) -> threading.Event:
        """
        Threading event set when the dialog closes for any reason.

        Background threads can block on ``stop_event.wait(timeout)`` instead of
        busy-polling ``result``.
        """

        return self._stop_event


    @property
    def result(self) -> Optional[DeviceAuthResult]:
        """
        The closing action after ``doModal()`` returns.

        :return: - ``DeviceAuthResult.SUCCESS``   poll thread confirmed authentication
                 - ``DeviceAuthResult.TIMEOUT``   ``timeout`` elapsed with no auth
                 - ``DeviceAuthResult.MANUAL``    manual-login button pressed
                 - ``DeviceAuthResult.CANCELED``  cancel button, back key, or safety net
                 - None                           dialog not yet closed
        """

        if self._canceled:
            return DeviceAuthResult.CANCELED

        if self._manual:
            return DeviceAuthResult.MANUAL

        return self._poll_result


    def close_with(self, result: DeviceAuthResult) -> None:
        """
        Close the dialog and record an explicit result.

        :param result: Terminal result; see the ``result`` property.
        """

        self._poll_result = result
        self.__dialog_wrapper.close()


    def update_progress(self) -> None:
        """
        Update the countdown display and close the dialog when the code expires.

        Must be called periodically by an external poller; the dialog does not
        self-tick. Is a no-op before ``onInit`` runs.
        """

        if self._start_time is None:
            return

        elapsed = time.time() - self._start_time
        if elapsed >= self._timeout:
            self.close_with(DeviceAuthResult.TIMEOUT)
            return

        remaining_seconds = max(0, self._timeout - int(elapsed))
        percent = max(0.0, 100.0 - (elapsed / self._timeout) * 100.0)
        bar_width = max(0, int(XML_PROGRESS_BAR_WIDTH * percent / 100))
        mins = remaining_seconds // _SECONDS_PER_MINUTE
        secs = remaining_seconds % _SECONDS_PER_MINUTE

        if percent >= _WARN_THRESHOLD:
            g = b = _COLOR_CHANNEL_MAX
        elif percent <= _DANGER_THRESHOLD:
            g = b = 0
        else:
            t = (percent - _DANGER_THRESHOLD) / (_WARN_THRESHOLD - _DANGER_THRESHOLD)
            g = b = int(_COLOR_CHANNEL_MAX * t)
        bar_color = "FF{:02X}{:02X}{:02X}".format(_COLOR_CHANNEL_MAX, g, b)

        progress = self.getControl(XML_ID_PROGRESS)
        progress.setWidth(bar_width)
        progress.setColorDiffuse(bar_color)
        self.getControl(XML_ID_TIME).setLabel("{:d}:{:02d}".format(mins, secs))


    # -- Kodi callbacks (routed from _WindowXMLDialogWrapper) ---------

    def _on_init(self) -> None:
        """ Populate all dialog controls when the window is first shown."""

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
        btn_manual.setLabel(self._manual_label)

        self.update_progress()


    def _on_closed(self) -> None:
        """ Record cancellation when closed without an explicit result."""

        self._stop_event.set()
        if (not self._canceled and
            not self._manual and
                self._poll_result is None):
            self._canceled = True


    def _on_action(self, action: Any) -> None:
        """ Handle navigation actions that dismiss the dialog."""

        if action.getId() in (ACTION_PREVIOUS_MENU, ACTION_NAV_BACK):
            self._canceled = True
            self.__dialog_wrapper.close()


    def _on_click(self, control_id: int) -> None:
        """ Handle button clicks to cancel or switch to manual login."""

        if control_id == XML_ID_BTN_CANCEL:
            self._canceled = True
            self.__dialog_wrapper.close()

        if control_id == XML_ID_BTN_MANUAL:
            self._manual = True
            self.__dialog_wrapper.close()
