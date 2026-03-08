# SPDX-License-Identifier: GPL-3.0-or-later
"""NLZIET channel for Retrospect."""

import base64
import json
import threading
import time
from urllib.parse import parse_qs, urlparse, quote

import xbmc

from resources.lib import chn_class, mediatype
from resources.lib.addonsettings import AddonSettings, LOCAL
from resources.lib.authentication.authenticator import Authenticator
from resources.lib.authentication.nlzietoauth2handler import NLZIETOAuth2Handler
from resources.lib.helpers.jsonhelper import JsonHelper
from resources.lib.helpers.languagehelper import LanguageHelper
from resources.lib.logger import Logger
from resources.lib.mediaitem import MediaItem
from resources.lib.streams.mpd import Mpd
from resources.lib.urihandler import UriHandler
from resources.lib.xbmcwrapper import XbmcWrapper


class Channel(chn_class.Channel):
    APPCONFIG_CACHE_KEY = "nlziet_appconfig"
    APPCONFIG_HEARTBEAT_DEFAULT = 90  # seconds

    API_CONTENT_URL = "https://api.nlziet.nl"

    API_V7_APPCONFIG = "/v7/appconfig"

    API_V8_PROFILE = "/v8/profile"

    API_V9_EPG_LIVE = "/v9/epg/programlocations/live"
    API_V9_LIVE_HANDSHAKE = "/v9/stream/handshake"


    service_interval = APPCONFIG_HEARTBEAT_DEFAULT
    """Refresh the appconfig every :data:`APPCONFIG_HEARTBEAT_DEFAULT` seconds
    until the first successful fetch, after which it follows the server-provided
    ``heartbeatInterval``.
    """

    is_blocked = False
    """Set to ``True`` when the server reports ``isAppBlocked`` in the appconfig."""

    is_update_required = False
    """Set to ``True`` when the server reports ``isUpdateRequired`` in the appconfig."""


    def __init__(self, channel_info):
        """Initialisation of the class.

        All class variables should be instantiated here and this method should not
        be overridden by any derived classes.

        :param ChannelInfo channel_info: The channel info object to base this channel on.

        """

        chn_class.Channel.__init__(self, channel_info)

        self.baseUrl = Channel.API_CONTENT_URL
        self.mainListUri = "#mainlist"
        self.noImage = channel_info.icon

        self.requiresLogon = True

        self.__handler = NLZIETOAuth2Handler()
        self.__authenticator = Authenticator(self.__handler)

        self._add_data_parser(
            self._prefix_urls(self.API_V9_EPG_LIVE),
            name="Live TV channels", json=True,
            requires_logon=True,
            parser=["data"],
            creator=self.create_live_channel_item,
            updater=self.update_live_item)

    # -- Authentication ----------------------------------------------------

    def log_on(self, username=None, password=None, interactive=True):
        """Authenticate and set up request headers.

        :param str|None username:    Optional username override (from tests / settings).
        :param str|None password:    Optional password override.
        :param bool interactive:     If False, never show UI (device flow is skipped).
        :return: True if authenticated successfully, False on failure, None if cancelled.
        :rtype: bool|None
        """

        if self.loggedOn:
            return True

        if Channel.is_blocked:
            XbmcWrapper.show_dialog(
                "NLZIET",
                LanguageHelper.get_localized_string(LanguageHelper.AccountBlocked))
            return False

        result = self.__handler.active_authentication()
        if result.logged_on:
            if not self.__validate_token():
                return False
            self.loggedOn = True
            self.__set_auth_headers()
            self.__select_profile_if_needed()
            self.__set_auth_headers()
            return True
        username = username or self._get_setting("nlziet_username", value_for_none=None)
        password = password or self._get_setting("nlziet_password", value_for_none=None)

        if username and password:
            if self.__handler.use_device_flow:
                self.__handler = NLZIETOAuth2Handler(use_device_flow=False)
                self.__authenticator = Authenticator(self.__handler)
            result = self.__authenticator.log_on(
                username=username, password=password,
                channel_guid=self.guid, setting_id="nlziet_password")
            if not result.logged_on:
                XbmcWrapper.show_dialog(
                    "NLZIET",
                    LanguageHelper.get_localized_string(LanguageHelper.LoginFailed))
                return False
        else:
            if not interactive:
                return False
            result = self.__run_device_flow()
            if result is None:
                return None  # user cancelled — no error
            if not result:
                return False

        self.loggedOn = True
        self.__set_auth_headers()
        self.__welcome_and_select_profile()
        self.__set_auth_headers()
        return True

    def __validate_token(self) -> bool:
        """Validate the current token against the NLZIET identity server.

        :return: True if the token is valid, False otherwise.
        :rtype: bool
        """

        user_info = self.__handler.get_user_info()
        if not user_info:
            msg = LanguageHelper.get_localized_string(LanguageHelper.SessionExpired)
            XbmcWrapper.show_dialog("NLZIET", msg)
            return False
        return True

    def __set_auth_headers(self):
        """Set the Bearer token and required app headers for API requests."""

        token = self.__handler.get_valid_token()
        if token:
            self.httpHeaders["Authorization"] = "Bearer {}".format(token)
        self.httpHeaders["Nlziet-AppName"] = "WebApp"
        self.httpHeaders["Nlziet-AppVersion"] = "5.65.5"

    # -- Profile selection -------------------------------------------------

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

        profile_id = self.__get_stored_profile_id()
        if profile_id:
            self.__handler.set_profile_claim(profile_id)
            return

        profiles = self.__list_profiles()
        if not profiles:
            Logger.warning("NLZIET: No profiles available")
            return

        if len(profiles) == 1:
            profile_id = profiles[0]["id"]
            if self.__handler.set_profile_claim(profile_id):
                self.__set_stored_profile_id(profile_id)
            Logger.info(f"NLZIET: Auto-selected only available profile: {profiles[0]['displayName']}")
            return

        options = [p["displayName"] for p in profiles]
        label = LanguageHelper.get_localized_string(LanguageHelper.SelectProfile)
        selected = XbmcWrapper.show_selection_dialog(label, options)
        if selected < 0:
            Logger.info("NLZIET: Profile selection cancelled")
            return

        profile_id = profiles[selected]["id"]
        if self.__handler.set_profile_claim(profile_id):
            self.__set_stored_profile_id(profile_id)

    def __get_stored_profile_id(self) -> str:
        """Return the stored profile ID, or empty string if none selected.

        :return: Profile UUID string, or ``""`` if no profile is selected.
        """
        return AddonSettings.get_setting("nlziet_profile_id", store=LOCAL) or ""

    def __set_stored_profile_id(self, profile_id: str) -> None:
        """Store the selected profile ID, or clear it when given an empty string.

        :param profile_id: Profile UUID to store, or ``""`` to clear.
        """
        AddonSettings.set_setting("nlziet_profile_id", profile_id, store=LOCAL)

    def __profile_type(self) -> str:
        """Return the profile type from the current access token's JWT claims.

        Reads the ``profileType`` claim embedded in the current access token.
        Valid values are ``"Adult"`` and ``"ChildYoung"``. Used for routing
        between the standard home page and the kids home page.

        :return: Profile type string, or ``""`` if not available.
        """
        token = self.__handler.get_valid_token()
        if not token:
            return ""
        try:
            payload = token.split(".")[1]
            padding = 4 - len(payload) % 4
            if padding != 4:
                payload += "=" * padding
            claims = json.loads(base64.urlsafe_b64decode(payload))
            return claims.get("profileType", "")
        except Exception:
            return ""

    def __list_profiles(self) -> list:
        """Fetch the list of available profiles from the NLZIET API.

        :return: List of profile dicts (id, displayName, type, color), or [] on error.
        :rtype: list
        """

        token = self.__handler.get_valid_token()
        if not token:
            Logger.warning("NLZIET: No access token for profile list")
            return []

        headers = {
            "Authorization": "Bearer {}".format(token),
            "Accept": "application/json"
        }
        try:
            response = UriHandler.open(
                self._prefix_urls(self.API_V8_PROFILE),
                additional_headers=headers,
                no_cache=True)
            if not response:
                Logger.error("NLZIET: Empty response from profile API")
                return []
            profiles = JsonHelper(response).get_value()
            return profiles if isinstance(profiles, list) else []
        except Exception:
            Logger.error("NLZIET: Failed to list profiles", exc_info=True)
            return []

    # -- Device flow -------------------------------------------------------

    def __run_device_flow(self):
        """Run device flow authentication with progress dialog and retry logic.

        :return: True if authentication succeeded, False on error, None if cancelled.
        :rtype: bool|None
        """

        monitor = xbmc.Monitor()
        while not monitor.abortRequested():
            device_name = xbmc.getInfoLabel("System.FriendlyName") or "Kodi Retrospect"
            try:
                flow = self.__handler.start_device_flow(device_name)
            except OSError:
                msg = LanguageHelper.get_localized_string(LanguageHelper.ConnectionError)
                XbmcWrapper.show_dialog("NLZIET", msg)
                return False
            if not flow:
                msg = LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupFailed)
                XbmcWrapper.show_dialog("NLZIET", msg)
                return False

            qr_url = "{}?code={}&name={}".format(
                NLZIETOAuth2Handler.API_ID_DEVICE,
                quote(flow["user_code"]), quote(device_name))

            result = self.__poll_with_progress(flow, qr_url)
            if result == "success":
                self.__handler.save_device_session(device_name)
                return True
            if result == "cancelled":
                return None
            if result == "manual":
                return self.__manual_login()

            continue  # timeout: start a fresh device flow automatically

    def __poll_with_progress(self, flow, qr_url):
        """Poll device flow with a progress dialog.

        :param dict flow:   The device flow response from start_device_flow().
        :param str qr_url:  URL to encode as a QR code, passed to the dialog.
        :return: "success", "cancelled", "manual", or "timeout"
        :rtype: str
        """

        user_code = flow["user_code"]
        verification_uri = flow["verification_uri"]
        device_code = flow["device_code"]
        interval = max(flow.get("interval", 5), 1)
        timeout = flow.get("expires_in", 900)

        title = LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupTitle)
        visit_text = LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupVisit)
        enter_code = LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupEnterCode)
        manual_lbl = LanguageHelper.get_localized_string(LanguageHelper.ManualLogin)

        from resources.lib.deviceauthdialog import DeviceAuthDialog
        dialog = DeviceAuthDialog(
            title, visit_text, verification_uri, enter_code,
            user_code, timeout, manual_label=manual_lbl,
            qr_url=qr_url, logo_path=self.icon)

        monitor = xbmc.Monitor()

        def _poll_worker():
            _interval = interval
            _time_since_poll = _interval
            _attempts = 0
            try:
                while not dialog.stop_event.wait(0.5):
                    if monitor.abortRequested():
                        dialog.close_with("cancelled")
                        return

                    dialog.update_progress()

                    _time_since_poll += 0.5
                    if _time_since_poll < _interval:
                        continue
                    _time_since_poll = 0.0
                    _attempts += 1

                    result = self.__handler.poll_device_flow_once(device_code)
                    if result == "success":
                        dialog.close_with("success")
                        return
                    elif result == "slow_down":
                        _interval += 1
                    elif result == "authorization_pending":
                        if _attempts > 10:
                            _interval = min(_interval + 1, 5)
                    elif result != "error":
                        dialog.close_with("timeout")
                        return
            except Exception:
                Logger.error("Device flow poll worker failed", exc_info=True)
                try:
                    dialog.close_with("cancelled")
                except Exception:
                    pass

        poll_thread = threading.Thread(target=_poll_worker, daemon=True)
        poll_thread.start()
        dialog.doModal()
        poll_thread.join(timeout=2.0)

        return dialog.result

    def __manual_login(self) -> bool:
        """Prompt for username/password and attempt login.

        :return: True if authentication succeeded.
        :rtype: bool
        """

        import xbmcgui
        dialog = xbmcgui.Dialog()
        username_label = LanguageHelper.get_localized_string(30035)
        username = dialog.input("NLZIET - {}".format(username_label))
        if not username:
            return False
        pw_label = LanguageHelper.get_localized_string(30036)
        password = dialog.input("NLZIET - {}".format(pw_label),
                                option=xbmcgui.ALPHANUM_HIDE_INPUT)
        if not password:
            return False

        self.__handler = NLZIETOAuth2Handler(use_device_flow=False)
        self.__authenticator = Authenticator(self.__handler)
        result = self.__handler.log_on(username, password)
        return result.logged_on

    # -- Settings actions --------------------------------------------------

    def setup_device(self):
        """Device flow authentication triggered from settings."""

        if self.__run_device_flow():
            self.__welcome_and_select_profile()

    def switch_profile(self):
        """Re-trigger profile selection from settings."""

        if not self.__handler.active_authentication().logged_on:
            XbmcWrapper.show_dialog(
                "NLZIET",
                LanguageHelper.get_localized_string(LanguageHelper.LoginFirst))
            return

        self.__set_stored_profile_id("")
        self.__select_profile_if_needed()
        self.__set_auth_headers()
        xbmc.executebuiltin("Container.Refresh()")

    def log_off(self):
        """Force a logoff for the channel."""

        if self.__handler.use_device_flow:
            self.__handler.deregister_device()

        self.__authenticator.log_off("", force=True)
        self.loggedOn = False
        msg = LanguageHelper.get_localized_string(LanguageHelper.LoggedOutSuccessfully)
        XbmcWrapper.show_dialog("NLZIET", msg)
        xbmc.executebuiltin("Container.Refresh()")

    # -- Appconfig cache ---------------------------------------------------

    def __sync_appconfig(self) -> None:
        """Fetch ``/appconfig`` and store the result in the local settings cache.

        This is the write side of the appconfig cache: it always performs a
        network request and overwrites whatever was previously cached on change.
        """

        Logger.debug(f"NLZIET: syncing appconfig from {self.baseUrl}{self.API_V7_APPCONFIG}")

        raw = UriHandler.open(
            self._prefix_urls(self.API_V7_APPCONFIG + "?os=web&origin=app"),
            additional_headers=self.httpHeaders)
        if not raw:
            Logger.warning("NLZIET: could not fetch appconfig")
            return

        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            Logger.warning("NLZIET: could not parse appconfig response")
            return

        cached_raw = AddonSettings.get_setting(self.APPCONFIG_CACHE_KEY, store=LOCAL)
        try:
            cached = json.loads(cached_raw) if cached_raw else {}
        except (ValueError, TypeError):
            cached = {}

        cached.pop("_synced_at", None)
        data_no_ts = {k: v for k, v in data.items() if k != "_synced_at"}
        if data_no_ts != cached:
            data["_synced_at"] = time.time()
            AddonSettings.set_setting(self.APPCONFIG_CACHE_KEY, json.dumps(data), store=LOCAL)

        Channel.service_interval = data.get("heartbeatInterval", self.APPCONFIG_HEARTBEAT_DEFAULT)
        Logger.debug(f"NLZIET: next heartbeat in {Channel.service_interval}s")

        Channel.is_blocked = data.get("isAppBlocked", False)
        if Channel.is_blocked:
            Logger.warning("NLZIET: App is blocked!")

        Channel.is_update_required = data.get("isUpdateRequired", False)
        if Channel.is_update_required:
            Logger.warning(f"NLZIET: API update required — {data.get('updateText', 'no details provided')}")

    def on_service(self) -> None:
        """Periodic background callback: sync the appconfig cache with the server.

        Called by the Retrospect background service every
        :attr:`~chn_class.Channel.service_interval` seconds so that the cached
        appconfig is always fresh when a user opens the channel.
        """
        if xbmc.Monitor().abortRequested():
            return
        self.__sync_appconfig()
