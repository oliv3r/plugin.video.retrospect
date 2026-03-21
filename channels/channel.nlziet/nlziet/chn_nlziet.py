# SPDX-License-Identifier: GPL-3.0-or-later
"""NLZIET channel for Retrospect."""

import json
import threading
import time
import xbmc
from typing import List, Optional, final

from resources.lib import chn_class
from resources.lib.addonsettings import AddonSettings, LOCAL
from resources.lib.authentication.authenticator import Authenticator
from resources.lib.authentication.nlziethandler import (
    NLZIETHandler, AUTH_CLIENT_ID_KEY, DEVICE_CLIENT_ID,
)
from resources.lib.channelinfo import ChannelInfo
from resources.lib.deviceauthdialog import DeviceAuthDialog
from resources.lib.helpers.jsonhelper import JsonHelper
from resources.lib.helpers.languagehelper import LanguageHelper
from resources.lib.logger import Logger
from resources.lib.mediaitem import MediaItem
from resources.lib.urihandler import UriHandler
from resources.lib.vault import Vault
from resources.lib.xbmcwrapper import XbmcWrapper


APPCONFIG_CACHE_KEY = "nlziet_appconfig"
APPCONFIG_HEARTBEAT_DEFAULT = 90  # seconds

# App identification headers sent with every API request (auth + content).
# Values are faked to match the expected API client — not real app versions;
# these were current at implementation time (2025-03).
NLZIET_APP_NAME = "AndroidTv"
NLZIET_APP_VERSION = "5.65.5"
NLZIET_WEB_NAME = "WebApp"
NLZIET_WEB_VERSION = "5.65.9"

# Device identification headers — we don't run on real Android hardware,
# so brand/model/platform are "unknown".
NLZIET_BRAND_NAME = "unknown"
NLZIET_MODEL_NAME = "unknown"
NLZIET_PLATFORM_VERSION = "unknown"
NLZIET_DEVICE_CAPABILITIES = ""

# NLZIET API
API_CONTENT_URL = "https://api.nlziet.nl"

# V7 API
API_V7_APPCONFIG = "/v7/appconfig"

# V8 API
API_V8_PROFILE = "/v8/profile"


@final
class Channel(chn_class.Channel):
    """NLZIET channel for Retrospect."""

    service_interval: int = APPCONFIG_HEARTBEAT_DEFAULT
    """Appconfig heartbeat interval in seconds."""

    is_blocked: bool = False
    """Set to ``True`` when the server reports ``isAppBlocked`` in the appconfig."""

    blocked_reason: str = ""
    """Human-readable block reason from the API (``appBlockedReason``), or empty string."""

    is_update_required: bool = False
    """Set to ``True`` when the server reports ``isUpdateRequired`` in the appconfig."""

    update_reason: str = ""
    """Human-readable update reason from the API (``updateText``), or empty string."""


    def __init__(self, channel_info: ChannelInfo) -> None:
        """
        Initialization of the class.

        All class variables should be instantiated here and this method should
        not be overridden by any derived classes.

        :param channel_info: The channel info object to base this channel on.
        """

        chn_class.Channel.__init__(self, channel_info)

        self.baseUrl = API_CONTENT_URL
        self.mainListUri = "#mainlist"
        self.noImage = channel_info.icon

        self._http_headers: dict = dict(self.httpHeaders)
        self._handler: Optional[NLZIETHandler] = None
        self._authenticator: Optional[Authenticator] = None

        self._add_data_parser(
            "#mainlist",
            preprocessor=self.get_initial_folder_items,
            requires_logon=True,
        )


    @property
    def loggedOn(self) -> bool:
        """True when the handler holds a currently usable access token.

        Transparently refreshes a near-expiry token; returns ``False`` for
        expired tokens even if a token string is present in memory.
        """

        return (self._handler is not None and
                self._handler.get_authentication_token() is not None)

    @loggedOn.setter
    def loggedOn(self, value: bool) -> None:
        """No-op — loggedOn is read-only and derived from handler token state."""

        pass


    # -- Content parsers ---------------------------------------------------

    def get_initial_folder_items(self, data: str) -> tuple[str, list[MediaItem]]:
        """
        Populate the main entry list for this channel.

        :param str data: The retrieved data for the current item and URL.
        :return: A tuple of the data and a list of MediaItems.
        """

        items: list[MediaItem] = []

        if not self.loggedOn:
            return data, items

        return data, items


    # -- Authentication ----------------------------------------------------

    def _create_handler(self, device_flow: bool) -> Optional[NLZIETHandler]:
        """
        Create the authentication handler and authenticator for the given flow.

        Explicitly releases any previous handler before constructing the new
        one so that the flow switch is visible at every call site.

        :param device_flow: True for device flow, False for headless login.
        :return: The newly created handler,
                 None on failure.
        """

        self._authenticator = None
        self._handler = None
        self._http_headers = dict(self.httpHeaders)

        Logger.debug("NLZIET: Creating %s handler", "device flow" if device_flow else "headless")

        if device_flow:
            self._http_headers["Nlziet-AppName"] = NLZIET_APP_NAME
            self._http_headers["Nlziet-AppVersion"] = NLZIET_APP_VERSION
        else:
            self._http_headers["Nlziet-AppName"] = NLZIET_WEB_NAME
            self._http_headers["Nlziet-AppVersion"] = NLZIET_WEB_VERSION
        self._http_headers["Nlziet-BrandName"] = NLZIET_BRAND_NAME
        self._http_headers["Nlziet-ModelName"] = NLZIET_MODEL_NAME
        self._http_headers["Nlziet-PlatformVersion"] = NLZIET_PLATFORM_VERSION
        self._http_headers["Nlziet-DeviceCapabilities"] = NLZIET_DEVICE_CAPABILITIES
        self._http_headers["Accept"] = "application/json"

        self._handler = NLZIETHandler(use_device_flow=device_flow, http_headers=self._http_headers)
        self._authenticator = Authenticator(self._handler)
        return self._handler


    def _greet_user(self, handler: NLZIETHandler) -> None:
        """Show a welcome dialog addressed to the current user."""

        try:
            info = handler.get_user_info()
        except (PermissionError, RuntimeError, IOError):
            info = {}
        display_name = (info.get("name") or
                        info.get("email") or
                        LanguageHelper.get_localized_string(LanguageHelper.UnknownUser))

        welcome = LanguageHelper.get_localized_string(LanguageHelper.WelcomeUser)

        XbmcWrapper.show_dialog("NLZIET", welcome.replace("{0}", display_name))


    def _complete_login(self, handler: NLZIETHandler, greet: bool = True) -> bool:
        """
        Finalise a successful authentication: set session state and select a profile.

        :param handler: The active authentication handler.
        :param greet: Whether to greet the user.
        :return: True if profile selected,
                 False if profile selection failed (also calls log_off).
        """

        if greet:
            self._greet_user(handler)

        if self._select_profile(handler):
            return True

        self.log_off()
        return False


    def _headless_login(self, username: str, password: str) -> bool:
        """
        Attempt a headless login with the given credentials.

        :param username: The NLZIET account username.
        :param password: The NLZIET account password.
        :return: True on success,
                 False on any failure (a notification is shown to the user).
        """

        handler = self._create_handler(device_flow=False)
        if handler is None:
            return False
        result = handler.log_on(username, password)
        if result.logged_on:
            return True

        if result.error == "invalid_credentials":
            msg_id = LanguageHelper.LoginFailed
        elif result.error == "network_error":
            msg_id = LanguageHelper.ConnectionError
        else:
            msg_id = LanguageHelper.UnknownError
        XbmcWrapper.show_notification("NLZIET", msg_id,
                                      notification_type=XbmcWrapper.Warning,
                                      display_time=5000)

        return False


    def _manual_login(self) -> bool:
        """
        Prompt for username and password interactively, then attempt a headless login.

        Pre-fills both fields from stored credentials so the user can correct them.

        :return: True if authenticated; False if canceled or login failed.
        """

        username = self._get_setting("nlziet_username", value_for_none=None) or ""
        label = LanguageHelper.get_localized_string(LanguageHelper.Username)
        username = XbmcWrapper.show_key_board(default=username, heading="NLZIET - {}".format(label))
        if not username:
            return False
        AddonSettings.set_channel_setting(self, "nlziet_username", username)

        label = LanguageHelper.get_localized_string(LanguageHelper.Password)
        Vault().set_channel_setting(self.guid, "nlziet_password", setting_name="NLZIET - {}".format(label))
        password = Vault().get_channel_setting(self.guid, "nlziet_password")
        if not password:
            return False

        return self._headless_login(username, password)


    def _poll_with_progress(self, auth_data: dict, handler: NLZIETHandler) -> str:
        """
        Poll device flow with a progress dialog.

        :param auth_data: The device flow response from _device_authorization_request().
        :param handler: The active authentication handler.
        :return: ``"success"``, ``"timeout"``, ``"manual"``, ``"canceled"``
                 (see :class:`~resources.lib.deviceauthdialog.DeviceAuthDialog`),
                 or ``"error"`` on (unexpected) failures.
        """

        dialog = DeviceAuthDialog(
            title=LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupTitle),
            visit_text=LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupVisit),
            visit_url=auth_data["verification_uri"],
            code_text=LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupEnterCode),
            code=auth_data["user_code"],
            timeout=auth_data["expires_in"],
            manual_label=LanguageHelper.get_localized_string(LanguageHelper.ManualLogin),
            qr_url=auth_data["qr_url"],
            logo_path=self.icon)

        monitor = xbmc.Monitor()
        def _poll_worker() -> None:
            try:
                while not dialog.stop_event.wait(0.5):
                    if monitor.abortRequested():
                        dialog.close_with("canceled")
                        return

                    dialog.update_progress()

                    result = handler.poll_device_authorization(auth_data["device_code"])
                    if result != "pending":
                        dialog.close_with(result)
                        return
            except Exception:
                Logger.error("Device flow poll worker failed", exc_info=True)
                dialog.close_with("error")

        poll_thread = threading.Thread(target=_poll_worker, daemon=True)
        poll_thread.start()
        dialog.doModal()

        # When the user chose manual login, don't block waiting for the
        # in-flight HTTP poll to complete — the stop_event is already set so
        # the daemon thread will exit as soon as the current request returns.
        if dialog.result != "manual":
            poll_thread.join(timeout=2.0)

        return dialog.result or "error"


    def _run_device_flow(self) -> Optional[bool]:
        """
        Run device flow authentication with progress dialog and retry logic.

        :return: True if authentication succeeded,
                 False on error,
                 None if canceled.
        """

        Logger.debug("NLZIET: Starting device flow")
        monitor = xbmc.Monitor()
        while not monitor.abortRequested():
            device_name = xbmc.getInfoLabel("System.FriendlyName") or "Kodi Retrospect"

            handler = self._create_handler(device_flow=True)
            if handler is None:
                return False
            try:
                device_auth = handler.start_nlziet_device_flow(device_name)
            except OSError:
                XbmcWrapper.show_dialog("NLZIET",
                    LanguageHelper.get_localized_string(LanguageHelper.ConnectionError))
                return False
            if not device_auth:
                XbmcWrapper.show_dialog("NLZIET",
                    LanguageHelper.get_localized_string(LanguageHelper.DeviceSetupFailed))
                return False

            result = self._poll_with_progress(device_auth, handler)
            Logger.debug("NLZIET: Device flow poll result: %s", result)
            if result == "success":
                return True
            if result == "canceled":
                return None
            if result == "manual":
                if self._manual_login():
                    return True
                continue  # failed or canceled → restart with fresh device flow
            if result == "error":
                XbmcWrapper.show_dialog("NLZIET",
                    LanguageHelper.get_localized_string(LanguageHelper.ConnectionError))
                return False

            XbmcWrapper.show_notification("NLZIET", LanguageHelper.DeviceCodeExpired,
                                          notification_type=XbmcWrapper.Warning,
                                          display_time=30000)

        return None  # canceled via abortRequested()


    def _auto_login(self) -> Optional[bool]:
        """
        Attempt authentication using stored credentials.

        :return: True on success,
                 False on any failure (missing one credential, network error, ...),
                 None if no credentials are configured at all.
        """

        username = self._get_setting("nlziet_username", value_for_none=None)
        password = Vault().get_channel_setting(self.guid, "nlziet_password")

        Logger.debug("NLZIET: Attempting credential login for: %s", username)

        if not username and not password:
            return None

        if not username:
            XbmcWrapper.show_notification("NLZIET", LanguageHelper.MissingUsername,
                                          notification_type=XbmcWrapper.Warning,
                                          display_time=5000)
            return False

        if not password:
            XbmcWrapper.show_notification("NLZIET", LanguageHelper.MissingPassword,
                                          notification_type=XbmcWrapper.Warning,
                                          display_time=5000)
            return False

        return self._headless_login(username, password)


    def _resume_session(self) -> Optional[bool]:
        """
        Attempt authentication using a cached token.

        :return: True if a valid cached session was found,
                 False if the network is unreachable (token state unknown),
                 None if there is no session to resume (no token or expired).
        """

        client_id = (AddonSettings.get_setting(AUTH_CLIENT_ID_KEY, store=LOCAL) or
                     DEVICE_CLIENT_ID)
        handler = self._create_handler(device_flow=(client_id == DEVICE_CLIENT_ID))
        if handler is None:
            return False

        token_result = handler.active_authentication()
        Logger.debug("NLZIET: Cached token present: %s", token_result.logged_on)
        if not token_result.logged_on:
            return None

        # active_authentication() refreshes when near expiry, token is guaranteed fresh.
        return True


    # -- Profile handling -------------------------------------------------

    def _get_profile_id(self) -> str:
        """
        Return the stored profile ID, or empty string if none selected.

        :return: Profile UUID string, or ``""`` if no profile is selected.
        """

        profile_id = AddonSettings.get_setting("nlziet_profile_id", store=LOCAL) or ""
        Logger.debug("NLZIET: Getting profile ID: %s", profile_id or "<none>")
        return profile_id


    def _set_profile_id(self, profile_id: str) -> None:
        """
        Store the selected profile ID.

        :param profile_id: Profile UUID to store.
        """

        Logger.debug("NLZIET: Setting profile ID: %s", profile_id)
        AddonSettings.set_setting("nlziet_profile_id", profile_id, store=LOCAL)


    def _clear_profile_id(self) -> None:
        """Clear the stored profile ID."""

        Logger.debug("NLZIET: Clearing profile ID")
        AddonSettings.set_setting("nlziet_profile_id", "", store=LOCAL)


    def _profile_type(self, handler: NLZIETHandler) -> str:
        """
        Return the profile type from the current access token's JWT claims.

        :param handler: The active authentication handler.
        :return: Profile type string, or ``""`` if not available.
        """

        profile_type = handler.token_profile_type
        Logger.debug("NLZIET: Profile type: %s", profile_type or "<none>")
        return profile_type


    def _list_profiles(self, handler: NLZIETHandler) -> List[dict]:
        """
        Fetch the list of available profiles from the NLZIET API.

        :param handler: The active authentication handler.
        :return: List of profile dicts (id, displayName, type, color), or [] on error.
        """

        try:
            response = UriHandler.open(
                self._prefix_urls(API_V8_PROFILE),
                additional_headers=handler.get_headers(),
                no_cache=True)
            if not response:
                Logger.error("NLZIET: Empty response from profile API")
                return []
            profiles = JsonHelper(response).get_value()
            return profiles if isinstance(profiles, list) else []
        except Exception:
            Logger.error("NLZIET: Failed to list profiles", exc_info=True)
            return []


    def _select_profile(self, handler: NLZIETHandler) -> bool:
        """
        Let the user select a valid profile and set a token claim for it.

        :param handler: The active authentication handler.
        :return: True if a profile claim was successfully set, False otherwise.
        """

        profile_id = self._get_profile_id()
        if profile_id:
            # Fast path: if the current token already carries this profile's
            # claim (token was not refreshed since last run), skip the extra
            # grant round-trip entirely.
            if handler.token_profile_id == profile_id:
                Logger.debug("NLZIET: Token already scoped to profile %s, skipping claim", profile_id)
                return True
            if handler.set_profile_claim(profile_id):
                return True
            Logger.warning("NLZIET: Stored profile %r is no longer valid; re-selecting", profile_id)
            self._clear_profile_id()

        profiles = self._list_profiles(handler)
        if not profiles:
            Logger.warning("NLZIET: No profiles available")
            XbmcWrapper.show_dialog("NLZIET",
                LanguageHelper.get_localized_string(LanguageHelper.NoProfilesAvailable))
            return False

        if len(profiles) == 1:
            profile_id = profiles[0]["id"]
            result = handler.set_profile_claim(profile_id)
            if result:
                self._set_profile_id(profile_id)
                Logger.info("NLZIET: Auto-selected only available profile: %s", profiles[0]["displayName"])
            else:
                Logger.error("NLZIET: Failed to set profile claim for auto-selected profile: %s", profiles[0]["displayName"])
            return result

        options = [p["displayName"] for p in profiles]
        label = LanguageHelper.get_localized_string(LanguageHelper.SelectProfile)
        selected = XbmcWrapper.show_selection_dialog(label, options)
        if selected < 0:
            Logger.info("NLZIET: Profile selection canceled")
            return False

        profile_id = profiles[selected]["id"]
        if handler.set_profile_claim(profile_id):
            self._set_profile_id(profile_id)
            return True

        return False


    # -- Settings actions --------------------------------------------------

    def setup_device(self) -> Optional[bool]:
        """
        Run device flow authentication from settings.

        Note: If we are unable to select a profile (none available, canceled
        by user), we force a log_off. Using the channel without a profile is
        not supported.

        :return: True if authenticated and profile selected,
                 False on error,
                 None if the user canceled.
        """

        result = self._run_device_flow()
        if result is not True:
            return result

        handler = self._handler
        if handler is None:
            return False
        result = self._complete_login(handler)
        if result:
            xbmc.executebuiltin("Container.Refresh()")

        return result


    def switch_profile(self) -> None:
        """Re-trigger profile selection from settings."""

        if not self.loggedOn:
            XbmcWrapper.show_dialog("NLZIET",
                LanguageHelper.get_localized_string(LanguageHelper.LoginFirst))
            return

        handler = self._handler
        if handler is None:
            return
        self._clear_profile_id()
        if self._select_profile(handler):
            xbmc.executebuiltin("Container.Refresh()")
        else:
            self.log_off()


    def log_on(self) -> Optional[bool]:
        """
        Authenticate and set up the session.

        Tries to resume an existing session first (no network, fast-path).
        Falls through to fresh authentication (credentials or device flow)
        if there is no valid cached token.

        :return: True if authenticated,
                 False on failure,
                 None if canceled.
        """

        if Channel.is_blocked:
            reason = Channel.blocked_reason or LanguageHelper.get_localized_string(LanguageHelper.UnknownError)
            XbmcWrapper.show_dialog("NLZIET",
                f"{LanguageHelper.get_localized_string(LanguageHelper.AccountBlocked)}\n\n({reason})")
            return False

        result = self._resume_session()
        if result is False:
            return False
        if result is True:
            handler = self._handler
            if handler is None:
                return False
            return self._complete_login(handler, greet=False)

        result = self._auto_login()
        if result is False:
            return False
        if result is True:
            handler = self._handler
            if handler is None:
                return False
            return self._complete_login(handler)

        result = self._run_device_flow()
        if result is False:
            return False
        if result is True:
            handler = self._handler
            if handler is None:
                return False
            return self._complete_login(handler)

        Logger.debug("NLZIET: All authentication paths exhausted.")
        return result


    def log_off(self) -> None:
        """Force a logoff for the channel."""

        if self._authenticator is None:
            client_id = (AddonSettings.get_setting(AUTH_CLIENT_ID_KEY, store=LOCAL) or
                         DEVICE_CLIENT_ID)
            self._create_handler(device_flow=(client_id == DEVICE_CLIENT_ID))

        authenticator = self._authenticator
        if authenticator is None:
            return
        authenticator.log_off("", force=True)

        msg = LanguageHelper.get_localized_string(LanguageHelper.LoggedOutSuccessfully)
        xbmc.executebuiltin("Container.Refresh()")
        xbmc.executebuiltin("Action(ParentDir)")
        XbmcWrapper.show_dialog("NLZIET", msg)


    # -- Appconfig cache ---------------------------------------------------

    def _sync_appconfig(self) -> None:
        """Fetch and cache the appconfig from the API."""

        Logger.debug(f"NLZIET: syncing appconfig from {self.baseUrl}{API_V7_APPCONFIG}")

        raw = UriHandler.open(self._prefix_urls(API_V7_APPCONFIG + "?os=web&origin=app"))
        if not raw:
            Logger.warning("NLZIET: could not fetch appconfig")
            return
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            Logger.warning("NLZIET: could not parse appconfig response")
            return
        if not isinstance(data, dict):
            Logger.warning("NLZIET: appconfig response is not a JSON object")
            return

        cached_raw = AddonSettings.get_setting(APPCONFIG_CACHE_KEY, store=LOCAL)
        try:
            settings_cached = json.loads(cached_raw) if cached_raw else {}
        except (ValueError, TypeError):
            settings_cached = {}

        settings_cached.pop("_synced_at", None)
        settings_no_synced_at = {k: v for k, v in data.items() if k != "_synced_at"}
        if settings_no_synced_at != settings_cached:
            data["_synced_at"] = time.time()
            AddonSettings.set_setting(APPCONFIG_CACHE_KEY, json.dumps(data), store=LOCAL)

        Channel.service_interval = data.get("heartbeatInterval", APPCONFIG_HEARTBEAT_DEFAULT)
        Logger.debug(f"NLZIET: next heartbeat in {Channel.service_interval}s")

        Channel.is_blocked = data.get("isAppBlocked", False)
        Channel.blocked_reason = data.get("appBlockedReason") or ""
        if Channel.is_blocked:
            Logger.warning(f"NLZIET: App is blocked - '{Channel.blocked_reason}'")

        Channel.is_update_required = data.get("isUpdateRequired", False)
        Channel.update_reason = data.get("updateText") or ""
        if Channel.is_update_required:
            Logger.warning(f"NLZIET: API update required - '{Channel.update_reason}'")


    # -- Background service ------------------------------------------------

    def service_update(self) -> None:
        """Periodic background callback."""

        self._sync_appconfig()

        if self._handler is None:
            return

        self._handler.refresh_access_token()
