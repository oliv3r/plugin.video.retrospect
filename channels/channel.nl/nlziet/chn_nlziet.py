# SPDX-License-Identifier: GPL-3.0-or-later
"""NLZiet channel for Retrospect."""

import time

import xbmc

from resources.lib import chn_class
from resources.lib.authentication.authenticator import Authenticator
from resources.lib.authentication.nlzietoauth2handler import NLZIETOAuth2Handler
from resources.lib.deviceauthdialog import DeviceAuthDialog
from resources.lib.helpers.languagehelper import LanguageHelper
from resources.lib.logger import Logger
from resources.lib.xbmcwrapper import XbmcWrapper


class Channel(chn_class.Channel):
    def __init__(self, channel_info):
        """Initialisation of the class.

        All class variables should be instantiated here and this method should not
        be overridden by any derived classes.

        :param ChannelInfo channel_info: The channel info object to base this channel on.

        """

        chn_class.Channel.__init__(self, channel_info)

        self.noImage = channel_info.icon

        self._add_data_parser("", requires_logon=True)

        self.__handler = NLZIETOAuth2Handler()
        self.__authenticator = Authenticator(self.__handler)

    def log_on(self, username=None, password=None) -> bool:
        if self.loggedOn:
            return True

        result = self.__handler.active_authentication()
        if result.logged_on:
            self.loggedOn = True
            self.__select_profile_if_needed()
            self.__set_auth_headers()
            return True

        username = username or self._get_setting("nlziet_username", value_for_none=None)
        password = password or self._get_setting("nlziet_password", value_for_none=None)

        if username and password:
            if self.__handler.use_device_flow:
                self.__handler = NLZIETOAuth2Handler(use_device_flow=False)
                self.__authenticator = Authenticator(self.__handler)
            result = self.__authenticator.log_on(username=username, password=password,
                channel_guid=self.guid, setting_id="nlziet_password")
            if not result.logged_on:
                return False
        else:
            if not self.__run_device_flow():
                return False

        self.loggedOn = True
        self.__welcome_and_select_profile()
        self.__set_auth_headers()
        return True

    def __welcome_and_select_profile(self):
        """Show welcome dialog and prompt for profile selection if needed."""

        user_info = self.__handler.get_user_info()
        if user_info:
            display_name = user_info.get("name") or user_info.get("email", "NLZiet User")
        else:
            display_name = "NLZiet User"

        welcome = LanguageHelper.get_localized_string(LanguageHelper.WelcomeUser)
        XbmcWrapper.show_dialog("NLZIET", welcome.replace("{0}", display_name))
        self.__select_profile_if_needed()

    def __select_profile_if_needed(self):
        """Prompt for profile selection if no profile is currently selected.

        When a profile is already stored, performs a token exchange so the
        access token is scoped to that profile (required for server-side
        content filtering such as kids profiles).
        """

        current = self.__handler.get_profile()
        if current:
            # Re-exchange token for the stored profile so API filtering works.
            self.__handler.set_profile(current["id"])
            return

        profiles = self.__handler.list_profiles()
        if not profiles:
            Logger.warning("NLZIET: No profiles available")
            return

        if len(profiles) == 1:
            self.__handler.set_profile(profiles[0]["id"])
            Logger.info("NLZIET: Auto-selected only available profile: %s", profiles[0]["displayName"])
            return

        options = [p["displayName"] for p in profiles]
        label = LanguageHelper.get_localized_string(LanguageHelper.SelectProfile)
        selected = XbmcWrapper.show_selection_dialog(label, options)
        if selected < 0:
            Logger.info("NLZIET: Profile selection cancelled")
            return

        self.__handler.set_profile(profiles[selected]["id"])

    def __run_device_flow(self) -> bool:
        """Run device flow authentication with progress dialog and retry logic.

        :return: True if authentication succeeded, False otherwise.
        """

        while True:
            device_name = xbmc.getInfoLabel("System.FriendlyName") or "Kodi Retrospect"
            flow = self.__handler.start_device_flow(device_name)
            if not flow:
                msg = LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupFailed)
                XbmcWrapper.show_dialog("NLZIET", msg)
                return False

            result = self.__poll_with_progress(flow)
            if result == "success":
                return True
            if result == "cancelled":
                return False
            if result == "manual":
                return self.__manual_login()

            timeout_msg = LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupTimeout)
            if not XbmcWrapper.show_yes_no("NLZIET", timeout_msg):
                return False

    def __poll_with_progress(self, flow: dict) -> str:
        """Poll device flow with a progress dialog.

        :param flow: The device flow response from start_device_flow()
        :return: "success", "cancelled", "manual", or "timeout"
        """

        user_code = flow["user_code"]
        verification_uri = flow["verification_uri"]
        device_code = flow["device_code"]
        interval = max(flow.get("interval", 5), 1)
        expires_in = flow.get("expires_in", 900)

        title = LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupTitle)
        visit_text = LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupVisit)
        enter_code = LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupEnterCode)
        cancel_lbl = LanguageHelper.get_localized_string(LanguageHelper.Cancel)
        manual_lbl = LanguageHelper.get_localized_string(LanguageHelper.ManualLogin)

        dialog = DeviceAuthDialog(
            title, visit_text, verification_uri, enter_code,
            user_code, expires_in, cancel_lbl, manual_label=manual_lbl)
        dialog.show()

        monitor = xbmc.Monitor()
        start_time = time.time()
        end_time = start_time + expires_in
        current_interval = interval
        time_since_poll = 0.0
        attempts = 0

        try:
            while time.time() < end_time:
                if monitor.waitForAbort(0.5):
                    return "cancelled"

                if dialog.cancelled:
                    return "cancelled"
                if dialog.manual_login:
                    return "manual"

                elapsed = time.time() - start_time
                pct = max(0, 100 - int((elapsed / expires_in) * 100))
                remaining = max(0, int(end_time - time.time()))
                dialog.update_progress(pct, remaining)

                time_since_poll += 0.5
                if time_since_poll < current_interval:
                    continue
                time_since_poll = 0.0
                attempts += 1

                result = self.__handler.poll_device_flow_once(device_code)

                if result == "success":
                    return "success"
                if result == "slow_down":
                    current_interval += 1
                elif result == "authorization_pending":
                    if attempts > 10:
                        current_interval = min(current_interval + 1, 5)
                elif result != "error":
                    return "timeout"

            return "timeout"
        finally:
            dialog.close()

    def __manual_login(self) -> bool:
        """Prompt for username/password and attempt login."""

        import xbmcgui
        dialog = xbmcgui.Dialog()
        username_label = LanguageHelper.get_localized_string(30035)
        username = dialog.input("NLZIET - {}".format(username_label))
        if not username:
            return False
        pw_label = LanguageHelper.get_localized_string(30036)
        password = dialog.input("NLZIET - {}".format(pw_label), option=xbmcgui.ALPHANUM_HIDE_INPUT)
        if not password:
            return False

        self.__handler = NLZIETOAuth2Handler(use_device_flow=False)
        self.__authenticator = Authenticator(self.__handler)
        result = self.__handler.log_on(username, password)
        return result.logged_on

    def setup_device(self):
        """Device flow authentication triggered from settings."""

        if self.__run_device_flow():
            self.__welcome_and_select_profile()

    def select_profile(self):
        """Re-trigger profile selection from settings."""

        if not self.__handler.active_authentication().logged_on:
            XbmcWrapper.show_dialog(
                "NLZIET",
                LanguageHelper.get_localized_string(LanguageHelper.LoginFirst))
            return

        self.__handler.clear_profile()
        self.__select_profile_if_needed()
        self.__set_auth_headers()
        xbmc.executebuiltin("Container.Refresh()")

    def log_off(self):
        """Force a logoff for the channel."""

        self.__authenticator.log_off("", force=True)
        self.loggedOn = False
        xbmc.executebuiltin("Container.Refresh()")
