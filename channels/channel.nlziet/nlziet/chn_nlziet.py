# SPDX-License-Identifier: GPL-3.0-or-later
"""NLZIET channel for Retrospect."""

import api
import json
import threading
import time
from http import HTTPStatus

import xbmc

from resources.lib import chn_class
from resources.lib import mediatype
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

_APPCONFIG_CACHE_KEY = "nlziet_appconfig"
_APPCONFIG_HEARTBEAT_DEFAULT = 90  # seconds; fallback when server value is absent


class Channel(chn_class.Channel):
    service_interval = _APPCONFIG_HEARTBEAT_DEFAULT
    """Refresh the appconfig every :data:`_APPCONFIG_HEARTBEAT_DEFAULT` seconds
    until the first successful fetch, after which it follows the server-provided
    ``heartbeatInterval``.
    """

    def __init__(self, channel_info):
        """Initialisation of the class.

        All class variables should be instantiated here and this method should not
        be overridden by any derived classes.

        :param ChannelInfo channel_info: The channel info object to base this channel on.

        """

        chn_class.Channel.__init__(self, channel_info)

        self.noImage = channel_info.icon
        self.baseUrl = "https://api.nlziet.nl"

        self.mainListUri = self._prefix_urls(api.API_V9_EPG_LIVE)
        self.requiresLogon = True

        self.__handler = NLZIETOAuth2Handler()
        self.__authenticator = Authenticator(self.__handler)

        self._add_data_parser(
            self._prefix_urls(api.API_V9_EPG_LIVE_PREFIX),
            name="Live TV channels", json=True,
            requires_logon=True,
            parser=["data"],
            creator=self.create_live_channel_item,
            updater=self.update_live_item)

    # -- Service callback --------------------------------------------------

    def on_service(self) -> None:
        """Periodic background callback: proactively refresh the appconfig cache.

        Called by the Retrospect background service every
        :attr:`~chn_class.Channel.service_interval` seconds so that the cached
        appconfig is always fresh when a user opens the channel.
        """

        self.__load_appconfig()

    # -- Authentication ----------------------------------------------------

    def log_on(self, username=None, password=None, interactive=True) -> bool:
        """Authenticate and set up request headers.

        :param str|None username:    Optional username override (from tests / settings).
        :param str|None password:    Optional password override.
        :param bool interactive:     If False, never show UI (device flow is skipped).
        :return: True if authenticated successfully.
        :rtype: bool
        """

        if self.loggedOn:
            return True

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
                    LanguageHelper.get_localized_string(LanguageHelper.LoginFirst))
                return False
        else:
            if not interactive:
                return False
            if not self.__run_device_flow():
                return False

        self.loggedOn = True
        self.__set_auth_headers()
        self.__welcome_and_select_profile()
        self.__set_auth_headers()
        return True

    def __validate_token(self) -> bool:
        """Validate the current token against the NLZIET API.

        :return: True if the token is valid, False otherwise.
        :rtype: bool
        """

        token = self.__handler.get_valid_token()
        if not token:
            msg = LanguageHelper.get_localized_string(LanguageHelper.SessionExpired)
            XbmcWrapper.show_dialog("NLZIET", msg)
            return False

        headers = {
            "Authorization": "Bearer {}".format(token),
            "Accept": "application/json"
        }
        try:
            UriHandler.open(self._prefix_urls(api.API_V8_PROFILE),
                            additional_headers=headers, no_cache=True)
            status = UriHandler.instance().status
            if status.error:
                if status.code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                    msg = LanguageHelper.get_localized_string(LanguageHelper.SessionExpired)
                else:
                    msg = LanguageHelper.get_localized_string(LanguageHelper.ConnectionError)
                XbmcWrapper.show_dialog("NLZIET", msg)
                return False
        except Exception:
            Logger.error("NLZIET: Token validation failed", exc_info=True)
            msg = LanguageHelper.get_localized_string(LanguageHelper.ConnectionError)
            XbmcWrapper.show_dialog("NLZIET", msg)
            return False

        return True

    def __set_auth_headers(self):
        """Set the Bearer token and required app headers for API requests."""

        self.__handler.refresh_access_token()
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

        current = self.__handler.get_profile()
        if current:
            self.__handler.set_profile(current["id"])
            return

        profiles = self.__handler.list_profiles()
        if not profiles:
            Logger.warning("NLZIET: No profiles available")
            return

        if len(profiles) == 1:
            self.__handler.set_profile(profiles[0]["id"])
            Logger.info("NLZIET: Auto-selected only available profile: %s",
                        profiles[0]["displayName"])
            return

        options = [p["displayName"] for p in profiles]
        label = LanguageHelper.get_localized_string(LanguageHelper.SelectProfile)
        selected = XbmcWrapper.show_selection_dialog(label, options)
        if selected < 0:
            Logger.info("NLZIET: Profile selection cancelled")
            return

        self.__handler.set_profile(profiles[selected]["id"])

    # -- Device flow -------------------------------------------------------

    def __run_device_flow(self) -> bool:
        """Run device flow authentication with progress dialog and retry logic.

        :return: True if authentication succeeded, False otherwise.
        :rtype: bool
        """

        while True:
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

            from urllib.parse import quote
            qr_url = "{}?code={}&name={}".format(
                NLZIETOAuth2Handler.DEVICE_PORTAL_URL,
                quote(flow["user_code"]), quote(device_name))

            result = self.__poll_with_progress(flow, qr_url)
            if result == "success":
                return True
            if result == "cancelled":
                return False
            if result == "manual":
                return self.__manual_login()

            timeout_msg = LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupTimeout)
            if not XbmcWrapper.show_yes_no("NLZIET", timeout_msg):
                return False

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
        expires_in = flow.get("expires_in", 900)

        title = LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupTitle)
        visit_text = LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupVisit)
        enter_code = LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupEnterCode)
        cancel_lbl = LanguageHelper.get_localized_string(LanguageHelper.Cancel)
        manual_lbl = LanguageHelper.get_localized_string(LanguageHelper.ManualLogin)

        from resources.lib.deviceauthdialog import DeviceAuthDialog
        from resources.lib.retroconfig import Config
        addon_path = Config.rootDir.rstrip("/\\")
        dialog = DeviceAuthDialog("DeviceAuthDialog.xml", addon_path)
        dialog.set_content(
            title, visit_text, verification_uri, enter_code,
            user_code, expires_in, cancel_lbl, manual_label=manual_lbl,
            qr_url=qr_url, logo_path=self.icon)

        monitor = xbmc.Monitor()
        start_time = time.time()
        end_time = start_time + expires_in
        auth_result = []

        def _poll_worker():
            _interval = interval
            _time_since_poll = _interval
            _attempts = 0
            try:
                while time.time() < end_time:
                    if dialog.stop_event.wait(0.5):
                        return

                    if monitor.abortRequested():
                        auth_result.append("cancelled")
                        dialog.close()
                        return

                    elapsed = time.time() - start_time
                    pct = max(0.0, 100.0 - (elapsed / expires_in) * 100.0)
                    remaining = max(0, int(end_time - time.time()))
                    dialog.update_progress(pct, remaining)

                    _time_since_poll += 0.5
                    if _time_since_poll < _interval:
                        continue
                    _time_since_poll = 0.0
                    _attempts += 1

                    result = self.__handler.poll_device_flow_once(device_code)
                    if result == "success":
                        auth_result.append("success")
                        dialog.close()
                        return
                    elif result == "slow_down":
                        _interval += 1
                    elif result == "authorization_pending":
                        if _attempts > 10:
                            _interval = min(_interval + 1, 5)
                    elif result != "error":
                        auth_result.append("timeout")
                        dialog.close()
                        return

                auth_result.append("timeout")
                dialog.close()
            except Exception:
                Logger.error("Device flow poll worker failed", exc_info=True)
                try:
                    dialog.close()
                except Exception:
                    pass

        poll_thread = threading.Thread(target=_poll_worker, daemon=True)
        poll_thread.start()
        dialog.doModal()
        poll_thread.join(timeout=2.0)

        if dialog.manual_login:
            return "manual"
        if dialog.cancelled:
            return "cancelled"
        return auth_result[0] if auth_result else "timeout"

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
        msg = LanguageHelper.get_localized_string(LanguageHelper.LoggedOutSuccessfully)
        XbmcWrapper.show_dialog("NLZIET", msg)
        xbmc.executebuiltin("Container.Refresh()")

    # -- Live channel items ------------------------------------------------

    def create_live_channel_item(self, result_set):
        """Create a MediaItem for a live TV channel.

        :param dict result_set: A single entry from the ``data`` array of the
            ``/v9/epg/programlocations/live`` response.
        :return: A playable MediaItem or None.
        :rtype: MediaItem|None
        """

        channel = result_set.get("channel")
        if not channel:
            return None

        content = channel.get("content", {})
        channel_id = content.get("id")
        title = content.get("title", "")
        if not channel_id or not title:
            return None

        url = self._prefix_urls(api.API_V9_EPG_LIVE_CHANNEL.format(channel_id))

        item = MediaItem(title, url, media_type=mediatype.VIDEO)
        item.isLive = True
        item.isGeoLocked = True
        item.isDrmProtected = True
        item.complete = False

        logo = content.get("logo", {})
        logo_url = logo.get("normalUrl")
        if logo_url:
            item.thumb = logo_url
            item.icon = logo_url

        program_locations = result_set.get("programLocations", [])
        if program_locations:
            first_program = program_locations[0].get("content", {})
            asset_id = first_program.get("assetId")
            if asset_id:
                item.metaData["asset_id"] = asset_id

            program_title = first_program.get("title", "")
            if program_title:
                item.description = program_title

        if channel.get("missingSubscriptionFeature") is not None:
            item.isPaid = True

        item.HttpHeaders = self.httpHeaders
        return item

    def update_live_item(self, item):
        """Fetch the DASH stream URL for a live channel.

        :param MediaItem item: The item to update with stream info.
        :return: The updated item.
        :rtype: MediaItem
        """

        Logger.debug("Updating live stream for: %s", item.name)

        channel_id = item.url.rsplit("channel=", 1)[-1] if "channel=" in item.url else ""
        if not channel_id:
            Logger.error("No channel ID in URL for: %s", item.name)
            return item

        appconfig = self.__load_appconfig()
        start_offset = max(0, appconfig.get("liveStreamRestartStartPadding", 180))

        handshake_url = self._prefix_urls(api.API_V9_LIVE_HANDSHAKE.format(channel_id))
        if start_offset != 0:
            handshake_url = "{}&startOffsetInSeconds={}".format(handshake_url, start_offset)

        item = self.__handle_stream_handshake(item, handshake_url, manifest_update="full")
        if start_offset > 0 and item.streams:
            item.streams[-1].add_property(
                "inputstream.adaptive.manifest_config",
                json.dumps({"live_offset": start_offset}))
        return item

    # -- Stream helpers ----------------------------------------------------

    def __handle_stream_handshake(self, item, handshake_url, manifest_update=None):
        """Perform a v9 stream handshake and configure the item for playback.

        :param MediaItem item:           The item to update.
        :param str handshake_url:        The full handshake URL.
        :param str|None manifest_update: If set, passed as manifest_update_params.
        :return: The updated item.
        :rtype: MediaItem
        """

        data = UriHandler.open(handshake_url, additional_headers=self.httpHeaders,
                               no_cache=True)
        if not data:
            Logger.error("Empty handshake response for: %s", item.name)
            return item

        json_data = JsonHelper(data)

        errors = json_data.get_value("errors", fallback=None)
        if errors:
            self.__handle_handshake_error(item, errors)
            return item

        mpd_url = json_data.get_value("manifestUrl", fallback=None)
        if not mpd_url:
            Logger.error("No stream URI in handshake for: %s", item.name)
            return item

        stream = item.add_stream(mpd_url, 0)

        drm = json_data.get_value("drm", fallback={})
        license_url = drm.get("licenseUrl") if drm else None
        if license_url:
            license_headers = drm.get("headers", {})
            license_key = Mpd.get_license_key(
                license_url, key_type="R", key_headers=license_headers)
            kwargs = {"license_key": license_key}
            if manifest_update:
                kwargs["manifest_update_params"] = manifest_update
            Mpd.set_input_stream_addon_input(stream, **kwargs)
        else:
            Mpd.set_input_stream_addon_input(stream)

        item.complete = True
        return item

    @staticmethod
    def __handle_handshake_error(item, errors):
        """Log and handle errors from a stream handshake response.

        :param MediaItem item: The item that failed.
        :param errors: Error data from the API (list or dict).
        """

        if isinstance(errors, dict):
            errors = [e for v in errors.values()
                      for e in (v if isinstance(v, list) else [v])]
        if not errors:
            return

        first = errors[0]
        if isinstance(first, str):
            Logger.error("Handshake error for %s: %s", item.name, first)
            return

        error_type = first.get("type", "")
        error_msg = first.get("message", "")
        Logger.error("Handshake error for %s: %s - %s", item.name, error_type, error_msg)

        if error_type == "MaximumStreamsReached":
            error_data = first.get("data", {})
            max_streams = str(error_data.get("maximumNumberOfStreams", "?"))
            msg = LanguageHelper.get_localized_string(LanguageHelper.MaxStreamsReached)
            msg = msg.replace("{0}", max_streams).replace("{1}", max_streams)
            XbmcWrapper.show_dialog("NLZIET", msg)

    # -- Appconfig cache ---------------------------------------------------

    def __load_appconfig(self):
        """Fetch and cache the /v7/appconfig payload.

        The response is cached in LocalSettings for :data:`_APPCONFIG_HEARTBEAT_DEFAULT`
        seconds.  A fresh fetch is forced when the cache is absent or stale.

        :return: Appconfig payload dict, or empty dict on failure.
        :rtype: dict
        """

        raw_cached = AddonSettings.get_setting(_APPCONFIG_CACHE_KEY, store=LOCAL) or ""
        if raw_cached:
            try:
                cached = json.loads(raw_cached)
                age = time.time() - cached.get("_fetched_at", 0)
                if age < _APPCONFIG_HEARTBEAT_DEFAULT:
                    Logger.debug("NLZIET: appconfig serving from cache (age=%.0fs)", age)
                    return cached
            except (ValueError, TypeError):
                pass

        Logger.debug("NLZIET: appconfig fetching fresh")

        raw = UriHandler.open(
            self._prefix_urls(api.API_V7_APPCONFIG + "?os=web&origin=app"),
            additional_headers=self.httpHeaders)
        if not raw:
            Logger.warning("NLZIET: Could not fetch appconfig")
            return {}

        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            Logger.warning("NLZIET: Could not parse appconfig")
            return {}

        data["_fetched_at"] = time.time()
        AddonSettings.set_setting(_APPCONFIG_CACHE_KEY, json.dumps(data), store=LOCAL)

        Channel.service_interval = data.get("heartbeatInterval", _APPCONFIG_HEARTBEAT_DEFAULT)
        Logger.debug("NLZIET: next heartbeat in %ss", Channel.service_interval)

        is_blocked = data.get("isAppBlocked", False)
        Logger.info("NLZIET: isAppBlocked=%s", is_blocked)
        # TODO: set class variable when channel gating is implemented

        return data
