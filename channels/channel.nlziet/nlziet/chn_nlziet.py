# SPDX-License-Identifier: GPL-3.0-or-later
"""NLZIET channel for Retrospect."""

import base64
import json
import threading
import time
import xbmc
from urllib.parse import parse_qs, quote, urlparse

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

    blocked_reason = ""
    """Human-readable reason from ``appBlockedReason`` in the appconfig, or empty string."""

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
        self.mainListUri = self._prefix_urls(self.API_V9_EPG_LIVE)
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
            msg = LanguageHelper.get_localized_string(LanguageHelper.AccountBlocked)
            if Channel.blocked_reason:
                msg += f"\n\n({Channel.blocked_reason})"
            XbmcWrapper.show_dialog("NLZIET", msg)
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
                msg = LanguageHelper.get_localized_string(LanguageHelper.LoginFailed)
                if Channel.is_blocked:
                    blocked = LanguageHelper.get_localized_string(LanguageHelper.AccountBlocked)
                    msg += f" — {blocked}\n\n({Channel.blocked_reason})"
                XbmcWrapper.show_dialog("NLZIET", msg)
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

        info = self.__handler.get_user_info() or {}
        unknown_user = LanguageHelper.get_localized_string(LanguageHelper.UnknownUser)
        display_name = info.get("name") or info.get("email") or unknown_user

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

        url = self._prefix_urls("{}?channel={}".format(self.API_V9_EPG_LIVE, channel_id))

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

        Logger.debug(f"Updating live stream for: {item.name}")

        channel_id = parse_qs(urlparse(item.url).query).get("channel", [""])[0]
        if not channel_id:
            Logger.error(f"No channel ID in URL for: {item.name}")
            return item

        padding_on = AddonSettings.get_channel_setting(
            self, "nlziet_restart_padding", "true") == "true"
        padding = self.__get_live_restart_padding() if padding_on else 0

        try:
            adjustment = int(float(
                AddonSettings.get_channel_setting(self, "nlziet_live_start_offset") or 0))
            adjustment = max(-120, min(320, adjustment))
        except (ValueError, TypeError):
            adjustment = 0

        total_offset = max(0, padding + adjustment)

        handshake_url = self._prefix_urls(
            "{}?context=Live&channel={}&drmType=Widevine&sourceType=Dash"
            "&playerName=BitmovinWeb&offsetType=Live".format(
                self.API_V9_LIVE_HANDSHAKE, channel_id)
        )
        if total_offset:
            handshake_url += f"&startOffsetInSeconds={total_offset}"

        item = self.__handle_stream_handshake(item, handshake_url, manifest_update="full")
        if total_offset and item.streams:
            item.streams[-1].add_property(
                "inputstream.adaptive.manifest_config",
                json.dumps({"live_offset": total_offset}))
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
            Logger.error(f"Empty handshake response for: {item.name}")
            return item

        try:
            json_data = JsonHelper(data)
        except (json.JSONDecodeError, ValueError) as e:
            Logger.error(f"NLZIET: Invalid JSON in handshake response for '{item.name}': {e}")
            return item

        errors = json_data.get_value("errors", fallback=None)
        if errors:
            self.__handle_handshake_error(item, errors)
            return item

        mpd_url = json_data.get_value("manifestUrl", fallback=None)
        if not mpd_url:
            Logger.error(f"No stream URI in handshake for: {item.name}")
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
            Logger.error(f"Handshake error for {item.name}: {first}")
            return

        error_type = first.get("type", "")
        error_msg = first.get("message", "")
        Logger.error(f"Handshake error for {item.name}: {error_type} - {error_msg}")

        if error_type == "MaximumStreamsReached":
            error_data = first.get("data", {})
            max_streams = str(error_data.get("maximumNumberOfStreams", "?"))
            msg = LanguageHelper.get_localized_string(LanguageHelper.MaxStreamsReached)
            msg = msg.replace("{0}", max_streams).replace("{1}", max_streams)
            XbmcWrapper.show_dialog("NLZIET", msg)

    # -- Appconfig cache ---------------------------------------------------

    def __get_live_restart_padding(self) -> int:
        """Return ``liveStreamRestartStartPadding`` from the cached appconfig (default 180)."""
        raw = AddonSettings.get_setting(self.APPCONFIG_CACHE_KEY, store=LOCAL) or "{}"
        try:
            return int(json.loads(raw).get("liveStreamRestartStartPadding", 180))
        except (ValueError, TypeError):
            return 180

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
        msg = LanguageHelper.get_localized_string(LanguageHelper.UnknownBlockReason)
        Channel.blocked_reason = data.get("appBlockedReason", msg)
        if Channel.is_blocked:
            blocked_reason = data.get("appBlockedReason", "Unknown block reason")
            Logger.warning(f"NLZIET: App is blocked — {blocked_reason}")

        Channel.is_update_required = data.get("isUpdateRequired", False)
        if Channel.is_update_required:
            update_reason = data.get("updateText", "Unknown update reason")
            Logger.warning(f"NLZIET: API update required — {update_reason}")

    def on_service(self) -> None:
        """Periodic background callback.

        Called by the Retrospect background service every
        :attr:`~chn_class.Channel.service_interval` seconds.
        """
        if xbmc.Monitor().abortRequested():
            return

        self.__sync_appconfig()
        self.__handler.refresh_access_token()
